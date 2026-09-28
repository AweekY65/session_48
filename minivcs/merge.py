"""基础三方合并（three-way merge）。

以合并基（merge base）为共同祖先，对两棵提交树逐路径合并：

* 仅一侧修改 → 采用该侧；
* 两侧改成相同结果 → 直接采用；
* 文本文件两侧在同一区域做了不同修改 → diff3 行级合并，
  自动合并非重叠改动，重叠区域生成 ``<<<<<<<`` / ``=======`` / ``>>>>>>>``
  冲突块并**保留双方全部内容**；
* 修改/删除冲突、双方不同的二进制改动 → 明确的冲突结果。

无冲突时直接生成两个父提交的 merge commit；存在冲突时把带冲突标记的文件
写入工作区，以 ours 版本暂存该路径，命令以非零状态退出，等待用户解决后提交。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from . import objects as obj
from .diffutil import looks_binary, merge3
from .objects import BLOB
from .repo import FlatTree, Repo, RepoError

CONFLICT_FILE_MODE = obj.FILE_MODE


def merge_base(repo: Repo, a: str, b: str) -> Optional[str]:
    """最近共同祖先：两侧祖先集合的交集中，到两侧距离和最小者。"""
    anc_a = repo.ancestors(a)
    anc_b = repo.ancestors(b)
    common = set(anc_a) & set(anc_b)
    if not common:
        return None
    return min(common, key=lambda c: (anc_a[c] + anc_b[c], anc_a[c], c))


def _blob_bytes(repo: Repo, entry: Optional[Tuple[str, str, str]]) -> Optional[bytes]:
    if entry is None:
        return None
    return repo.store.read(entry[2], BLOB)


def _merge_path(repo: Repo, rel: str,
                base: Optional[Tuple[str, str, str]],
                ours: Optional[Tuple[str, str, str]],
                theirs: Optional[Tuple[str, str, str]],
                ours_label: str, theirs_label: str
                ) -> Tuple[Optional[Tuple[str, str, str]], Optional[str]]:
    """合并单个路径。

    返回 (结果条目 或 None(表示删除), 冲突描述 或 None)。
    """
    # 两侧完全一致（含双方都不存在）
    if ours == theirs:
        return ours, None
    # 只有一侧相对 base 发生变化
    if ours == base:
        return theirs, None
    if theirs == base:
        return ours, None

    # 两侧都发生了变化且结果不同
    # 修改/删除冲突
    if ours is None or theirs is None:
        side = ours_label if theirs is None else theirs_label
        return (ours or theirs), f"{rel}: 修改/删除冲突（{side} 侧删除，另一侧修改）"

    if ours[2] == theirs[2]:
        # blob 内容一致但模式可能不同
        mode = ours[0] if ours[0] == theirs[0] else obj.FILE_MODE
        return (mode, BLOB, ours[2]), None

    base_bytes = _blob_bytes(repo, base) if base else b""
    ours_bytes = _blob_bytes(repo, ours)
    theirs_bytes = _blob_bytes(repo, theirs)

    if looks_binary(ours_bytes) or looks_binary(theirs_bytes) \
            or (base_bytes is not None and looks_binary(base_bytes)):
        # 二进制文件无法行级合并：明确冲突，写入 ours 内容并提示
        return (ours[0], BLOB, ours[2]), f"{rel}: 二进制文件双方修改冲突"

    merged, conflict = merge3(base_bytes or b"", ours_bytes, theirs_bytes,
                              ours_label, theirs_label)
    mode = ours[0] if ours[0] == theirs[0] else obj.FILE_MODE
    oid = repo.store.put_blob(merged)
    if conflict:
        return (mode, BLOB, oid), f"{rel}: 文本内容冲突（见冲突标记）"
    return (mode, BLOB, oid), None


def merge_trees(repo: Repo, base_flat: FlatTree,
                ours_flat: FlatTree, theirs_flat: FlatTree,
                ours_label: str = "ours", theirs_label: str = "theirs"
                ) -> Tuple[FlatTree, List[str]]:
    result: FlatTree = {}
    conflicts: List[str] = []
    for rel in sorted(set(base_flat) | set(ours_flat) | set(theirs_flat)):
        entry, conflict = _merge_path(
            repo, rel,
            base_flat.get(rel), ours_flat.get(rel), theirs_flat.get(rel),
            ours_label, theirs_label,
        )
        if conflict:
            conflicts.append(conflict)
        if entry is not None:
            result[rel] = entry
    return result, conflicts


def merge(repo: Repo, branch: str, message: Optional[str] = None
          ) -> Tuple[str, List[str], bool]:
    """把 branch 合并进当前 HEAD。

    返回 (结果提交ID 或 "", 冲突描述列表, 是否快进)。
    """
    theirs = repo.resolve_branch(branch) if repo.branch_exists(branch) else None
    if theirs is None:
        theirs = repo.resolve_target(branch)
    ours = repo.head_commit()
    if ours is None:
        raise RepoError("当前分支还没有提交，无法合并")
    if ours == theirs:
        return ours, [], False

    base = merge_base(repo, ours, theirs)

    # 快进：当前提交是对方祖先，直接前进
    if base == ours:
        target_flat = repo.flatten_tree(repo.commit_info(theirs)["tree"])
        from .checkout import switch_check
        problems = switch_check(repo, target_flat)
        if problems:
            raise RepoError(
                "快进合并会覆盖以下未提交的修改，请先 commit 或 stash：\n\t"
                + "\n\t".join(problems)
            )
        from .checkout import apply_switch
        apply_switch(repo, theirs)
        if repo.head_is_detached():
            repo.set_head_detached(theirs)
        else:
            repo.update_branch(repo.head_refname() or "main", theirs)
        return theirs, [], True

    # 已是同一内容（对方是我们祖先）：already up to date
    if base == theirs:
        return ours, [], False

    base_flat = repo.flatten_tree(repo.commit_info(base)["tree"])
    ours_flat = repo.flatten_tree(repo.commit_info(ours)["tree"])
    theirs_flat = repo.flatten_tree(repo.commit_info(theirs)["tree"])

    result_flat, conflicts = merge_trees(
        repo, base_flat, ours_flat, theirs_flat,
        ours_label=repo.head_refname() or "HEAD", theirs_label=branch,
    )

    # 把合并结果全部写入工作区（冲突文件含冲突标记），索引也同步
    import os
    for rel, (mode, _type, oid) in result_flat.items():
        full = os.path.join(repo.root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True) if os.path.dirname(rel) else None
        with open(full, "wb") as f:
            f.write(repo.store.read(oid, BLOB))
        from .checkout import _apply_mode
        _apply_mode(full, mode)
    old_paths = set(repo.index_flat())
    for rel in old_paths - set(result_flat):
        from .checkout import _remove_file
        _remove_file(repo, rel)
    repo.write_index(
        [{"path": p, "mode": m, "oid": o}
         for p, (m, _t, o) in result_flat.items()]
    )

    if conflicts:
        return "", conflicts, False

    msg = message or f"Merge branch '{branch}'"
    tree_id = repo.commit_tree(result_flat)
    name, email_addr = repo.author_identity()
    ts = repo.author_timestamp()
    payload = obj.build_commit_payload(
        tree_id, [ours, theirs], name, email_addr, ts, msg,
    )
    cid = repo.store.put(obj.COMMIT, payload)
    if repo.head_is_detached():
        repo.set_head_detached(cid)
    else:
        repo.update_branch(repo.head_refname() or "main", cid)
    return cid, [], False
