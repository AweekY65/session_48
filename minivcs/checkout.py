"""checkout：创建/切换分支、游离头检出。

核心安全规则——切换前检查目标树是否会覆盖工作区里尚未提交的修改：

* 工作区中被跟踪的文件若相对索引有修改，且目标树该文件内容与工作区不一致，
  则拒绝切换（不会静默覆盖）。
* 索引中有、HEAD 树中没有的文件（新暂存/待提交文件），目标树若要写入不同内容，拒绝。
* 未跟踪文件若目标树中存在同路径文件，拒绝。
"""

from __future__ import annotations

import os
import stat
from typing import List, Tuple

from . import objects as obj
from .objects import BLOB
from .repo import FlatTree, Repo, RepoError


def _apply_mode(full: str, mode: str) -> None:
    if mode == obj.EXEC_MODE:
        os.chmod(full, 0o755)
    elif mode == obj.FILE_MODE:
        os.chmod(full, 0o644)


def _write_file(repo: Repo, path: str, mode: str, oid: str) -> None:
    full = os.path.join(repo.root, path)
    os.makedirs(os.path.dirname(full), exist_ok=True) if os.path.dirname(path) else None
    data = repo.store.read(oid, BLOB)
    with open(full, "wb") as f:
        f.write(data)
    _apply_mode(full, mode)


def _remove_file(repo: Repo, path: str) -> None:
    full = os.path.join(repo.root, path)
    try:
        os.chmod(full, 0o644)
        os.remove(full)
    except FileNotFoundError:
        pass


def _prune_empty_dirs(repo: Repo) -> None:
    for dirpath, dirs, files in os.walk(repo.root, topdown=False):
        if os.path.abspath(dirpath) == os.path.abspath(repo.root):
            continue
        if os.path.abspath(dirpath).startswith(os.path.abspath(repo.vcsdir)):
            continue
        if not os.listdir(dirpath):
            os.rmdir(dirpath)


def _current_blobs(repo: Repo):
    """返回 (工作区内容映射, 索引映射, HEAD树映射)，均为 path -> (mode,oid)。"""
    index_flat = repo.index_flat()
    head = repo.head_commit()
    head_flat: FlatTree = {}
    if head:
        head_flat = repo.flatten_tree(repo.commit_info(head)["tree"])
    wt: dict = {}
    for rel in repo.worktree_files():
        full = os.path.join(repo.root, rel)
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            continue
        mode = obj.EXEC_MODE if os.stat(full).st_mode & stat.S_IXUSR else obj.FILE_MODE
        wt[rel] = (mode, obj.blob_id(data))
    return wt, index_flat, head_flat


def switch_check(repo: Repo, target_flat: FlatTree) -> List[str]:
    """列出所有会导致未提交修改被覆盖的冲突路径；空列表表示可以安全切换。"""
    wt, index_flat, head_flat = _current_blobs(repo)
    problems: List[str] = []

    tracked = set(index_flat) | set(head_flat)
    for path, cur in wt.items():
        idx = index_flat.get(path)
        head = head_flat.get(path)
        dirty = False
        if idx is not None:
            # 工作区相对索引被修改（内容或模式）
            dirty = (cur[0], cur[1]) != (idx[0], idx[2])
        elif head is not None:
            # 文件在 HEAD 有、索引里已暂存删除，但工作区仍存在且有改动
            dirty = (cur[0], cur[1]) != (head[0], head[2])
        if not dirty:
            continue
        target = target_flat.get(path)
        # 目标内容与索引一致时，本地修改可以随切换原样携带，不构成冲突
        if idx is not None and target is not None \
                and (target[0], target[2]) == (idx[0], idx[2]):
            continue
        # 其余情况下目标树中该路径内容与当前工作区不同就会被覆盖
        if target is None or (target[0], target[2]) != cur:
            problems.append(path)

    # 暂存的新文件（索引有、HEAD 没有），工作区可能已删除或改动：
    for path, entry in index_flat.items():
        if path in head_flat:
            continue
        target = target_flat.get(path)
        cur = wt.get(path)
        if target is None:
            # 目标分支没有这个文件；已暂存的新增内容会丢失
            problems.append(path)
        elif cur is not None and (target[0], target[2]) != (cur[0], cur[1]):
            problems.append(path)

    # 未跟踪文件（工作区有、索引和 HEAD 都没有），目标树若占用同路径则冲突
    for path, cur in wt.items():
        if path in index_flat or path in head_flat:
            continue
        if path in target_flat:
            problems.append(path)

    # 本地删除了受控文件（工作区没有、索引有），而目标树仍需要它 → 恢复会覆盖“删除”
    for path, entry in index_flat.items():
        if path in wt or path in problems:
            continue
        target = target_flat.get(path)
        if target is not None and (target[0], target[2]) != (entry[0], entry[2]):
            # 目标内容不同于索引：需要写入，与本地删除冲突
            problems.append(path)

    return sorted(set(problems))


def apply_switch(repo: Repo, target_commit: str) -> None:
    """检出目标提交的树并同步工作区与索引（调用前应先通过 switch_check）。

    对「目标内容 == 索引内容」的路径保留工作区现状（随切换携带未提交修改，
    与 Git 行为一致）；其余路径严格写成目标内容。
    """
    info = repo.commit_info(target_commit)
    target_flat = repo.flatten_tree(info["tree"])
    index_flat = repo.index_flat()
    old_paths = set(index_flat)

    for path, (mode, _type, oid) in target_flat.items():
        idx = index_flat.get(path)
        full = os.path.join(repo.root, path)
        if idx is not None and idx[2] == oid and idx[0] == mode and os.path.exists(full):
            # 索引与目标一致：工作区可能带着可保留的本地修改，不要覆盖
            continue
        _write_file(repo, path, mode, oid)
    for path in old_paths - set(target_flat):
        idx = index_flat.get(path)
        full = os.path.join(repo.root, path)
        if idx is not None and os.path.exists(full):
            # 工作区相对索引有未提交改动时保留文件，避免丢失用户修改；
            # 未改动（内容仍等于索引）则按检出语义删除
            try:
                with open(full, "rb") as f:
                    cur = f.read()
                cur_mode = obj.EXEC_MODE if os.stat(full).st_mode & stat.S_IXUSR \
                    else obj.FILE_MODE
                if obj.blob_id(cur) != idx[2] or cur_mode != idx[0]:
                    continue
            except OSError:
                pass
        _remove_file(repo, path)
    _prune_empty_dirs(repo)
    repo.write_index(
        [{"path": p, "mode": m, "oid": o}
         for p, (m, _t, o) in target_flat.items()]
    )


def checkout(repo: Repo, target: str, create: bool = False) -> Tuple[str, str]:
    """切换到分支或提交。返回 (模式, 目标)。冲突时抛 RepoError，绝不改动工作区。"""
    if create:
        if repo.branch_exists(target):
            raise RepoError(f"分支已存在：{target}")
        start = repo.head_commit()
        if start is None:
            raise RepoError("当前没有任何提交，无法创建分支")
        target_commit = start
        target_flat = repo.flatten_tree(repo.commit_info(target_commit)["tree"])
        problems = switch_check(repo, target_flat)
        if problems:
            raise RepoError(
                "切换会覆盖以下未提交的修改，请先 commit 或 stash：\n\t"
                + "\n\t".join(problems)
            )
        repo.update_branch(target, target_commit)
        repo.set_head_to_branch(target)
        apply_switch(repo, target_commit)
        return "branch-created", target

    if repo.branch_exists(target):
        oid = repo.resolve_branch(target)
        if oid is None:
            raise RepoError(f"分支 {target!r} 引用无效")
        target_flat = repo.flatten_tree(repo.commit_info(oid)["tree"])
        problems = switch_check(repo, target_flat)
        if problems:
            raise RepoError(
                "切换会覆盖以下未提交的修改，请先 commit 或 stash：\n\t"
                + "\n\t".join(problems)
            )
        repo.set_head_to_branch(target)
        apply_switch(repo, oid)
        return "branch", target

    # 游离头：检出提交 ID
    oid = repo.resolve_target(target)
    target_flat = repo.flatten_tree(repo.commit_info(oid)["tree"])
    problems = switch_check(repo, target_flat)
    if problems:
        raise RepoError(
            "切换会覆盖以下未提交的修改，请先 commit 或 stash：\n\t"
            + "\n\t".join(problems)
        )
    repo.set_head_detached(oid)
    apply_switch(repo, oid)
    return "detached", oid
