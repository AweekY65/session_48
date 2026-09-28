"""fsck —— 仓库完整性检查。

检测四类问题：

1. **缺失对象**：被 tree / commit / 索引引用，但 objects 目录中不存在；
2. **损坏对象**：无法解压、头部非法、长度不符或内容 SHA-256 与对象 ID 不一致；
3. **无效引用**：分支文件指向不存在或类型错误的对象、HEAD 指向非法内容；
4. **不可达对象**：存在于对象库但从所有分支/HEAD 出发不可达（提示性问题）。
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from . import objects as obj
from .repo import Repo


def fsck(repo: Repo) -> Tuple[List[str], List[str]]:
    """返回 (errors, warnings)。errors 非空时调用方应以非零状态退出。"""
    errors: List[str] = []
    warnings: List[str] = []

    all_ids = set(repo.store.iter_ids())

    # 1) 逐个对象做哈希/结构校验
    valid_commits = set()
    valid_trees = set()
    valid_blobs = set()
    for oid in sorted(all_ids):
        try:
            otype, payload = repo.store.read_raw(oid)
        except obj.VCSObjectError as exc:
            errors.append(str(exc))
            continue
        if otype == obj.COMMIT:
            try:
                obj.parse_commit(payload)
            except obj.VCSObjectError as exc:
                errors.append(f"corrupt commit {oid}: {exc}")
                continue
            valid_commits.add(oid)
        elif otype == obj.TREE:
            try:
                obj.parse_tree(payload)
            except obj.VCSObjectError as exc:
                errors.append(f"corrupt tree {oid}: {exc}")
                continue
            valid_trees.add(oid)
        else:
            valid_blobs.add(oid)

    # 2) 引用检查，并收集可达根
    roots: List[str] = []
    # HEAD
    head_oid = None
    try:
        with open(repo.head_path, "r", encoding="ascii") as f:
            head_content = f.read().strip()
    except OSError:
        errors.append("invalid HEAD: 文件缺失")
        head_content = ""
    if head_content.startswith("ref: "):
        refname = head_content[5:].strip()
        if not refname.startswith("refs/heads/"):
            errors.append(f"invalid HEAD: 非法引用 {refname!r}")
    elif head_content:
        if len(head_content) != obj.ID_LEN:
            errors.append("invalid HEAD: 游离头不是合法对象 ID")
        elif head_content not in all_ids:
            errors.append(f"invalid HEAD: 提交 {head_content} 不存在")
        elif head_content not in valid_commits:
            errors.append(f"invalid HEAD: {head_content} 不是有效 commit")
        else:
            head_oid = head_content
            roots.append(head_oid)
    # 空内容表示未出生分支，合法

    for name in repo.list_branches():
        ref_oid = repo.resolve_branch(name)
        if ref_oid is None:
            errors.append(f"invalid ref refs/heads/{name}: 内容不是合法对象 ID")
            continue
        if ref_oid not in all_ids:
            errors.append(f"invalid ref refs/heads/{name}: 指向的提交 {ref_oid} 缺失")
            continue
        if ref_oid not in valid_commits:
            errors.append(f"invalid ref refs/heads/{name}: {ref_oid} 不是有效 commit")
            continue
        roots.append(ref_oid)

    # 3) 从可达根遍历，检查被引用对象是否存在/类型正确
    reachable: Dict[str, str] = {}  # oid -> 类型

    def walk_commit(cid: str, referrer: str) -> None:
        if cid in reachable:
            return
        reachable[cid] = obj.COMMIT
        if cid not in all_ids:
            errors.append(f"missing object {cid} (commit, referenced by {referrer})")
            return
        if cid not in valid_commits:
            # 前面已登记具体损坏原因
            return
        info = obj.parse_commit(repo.store.read(cid, obj.COMMIT))
        if info["tree"] not in all_ids:
            errors.append(
                f"missing object {info['tree']} (tree, referenced by commit {cid})"
            )
        elif info["tree"] not in valid_trees:
            errors.append(f"broken reference: commit {cid} 的 tree "
                          f"{info['tree']} 不是有效 tree")
        else:
            walk_tree(info["tree"], f"commit {cid}")
        for p in info["parents"]:
            if p not in all_ids:
                errors.append(
                    f"missing object {p} (parent commit, referenced by commit {cid})"
                )
            elif p not in valid_commits:
                errors.append(f"broken reference: commit {cid} 的父提交 "
                              f"{p} 不是有效 commit")
            else:
                walk_commit(p, f"commit {cid}")

    def walk_tree(tid: str, referrer: str) -> None:
        if tid in reachable:
            return
        reachable[tid] = obj.TREE
        for mode, otype, name, child in obj.parse_tree(repo.store.read(tid, obj.TREE)):
            if child not in all_ids:
                kind = "tree" if otype == obj.TREE else "blob"
                errors.append(
                    f"missing object {child} ({kind}, referenced by tree {tid} "
                    f"entry {name!r})"
                )
                continue
            if otype == obj.TREE:
                if child not in valid_trees:
                    errors.append(f"broken reference: tree {tid} 的条目 "
                                  f"{name!r} 应指向 tree，但 {child} 不是")
                else:
                    walk_tree(child, f"tree {tid}")
            else:
                if child not in valid_blobs:
                    errors.append(f"broken reference: tree {tid} 的条目 "
                                  f"{name!r} 应指向 blob，但 {child} 不是")
                reachable[child] = obj.BLOB

    for root in set(roots):
        walk_commit(root, "refs")

    # 4) 索引中引用的 blob 必须存在
    try:
        for e in repo.read_index():
            if e["oid"] not in all_ids:
                errors.append(
                    f"missing object {e['oid']} (blob, referenced by index entry "
                    f"{e['path']!r})"
                )
    except Exception as exc:  # 索引损坏
        errors.append(f"invalid index: {exc}")

    # 5) 不可达对象（含孤立 blob）——警告
    for oid in sorted(all_ids - set(reachable)):
        warnings.append(f"unreachable object {oid}")

    return errors, warnings
