"""仓库核心：.minivcs 目录布局、索引、引用、配置与工作区操作。

目录布局::

    .minivcs/
      objects/          内容寻址对象存储（见 objects.py）
      index             JSON 索引：[{path, mode, oid}, ...]，按 path 排序
      HEAD              "ref: refs/heads/main" 或游离头提交 ID
      refs/heads/<名>    每个分支一个文件，内容是该分支指向的 commit ID
      config            JSON 配置（如 user.name / user.email）
"""

from __future__ import annotations

import json
import os
import stat
import time
from typing import Dict, List, Optional, Tuple

from . import objects as obj
from .objects import (
    BLOB,
    COMMIT,
    DIR_MODE,
    EXEC_MODE,
    FILE_MODE,
    TREE,
    ObjectStore,
    VCSObjectError,
)

DIR_NAME = ".minivcs"


class RepoError(Exception):
    """仓库逻辑错误（命令可预期的失败）。"""


# 路径 -> (mode, type, oid)
FlatTree = Dict[str, Tuple[str, str, str]]


class Repo:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.vcsdir = os.path.join(self.root, DIR_NAME)
        self.objects_dir = os.path.join(self.vcsdir, "objects")
        self.index_path = os.path.join(self.vcsdir, "index")
        self.head_path = os.path.join(self.vcsdir, "HEAD")
        self.refs_dir = os.path.join(self.vcsdir, "refs", "heads")
        self.config_path = os.path.join(self.vcsdir, "config")
        self.store = ObjectStore(self.objects_dir)

    # ------------------------------------------------------------ 查找/初始化

    @staticmethod
    def find(start: Optional[str] = None) -> "Repo":
        cur = os.path.abspath(start or os.getcwd())
        while True:
            if os.path.isdir(os.path.join(cur, DIR_NAME)):
                return Repo(cur)
            parent = os.path.dirname(cur)
            if parent == cur:
                raise RepoError(f"{DIR_NAME} 仓库不存在（先运行 init）")
            cur = parent

    @staticmethod
    def init(path: str = ".") -> "Repo":
        root = os.path.abspath(path)
        vcsdir = os.path.join(root, DIR_NAME)
        if os.path.isdir(vcsdir):
            raise RepoError(f"仓库已存在：{vcsdir}")
        os.makedirs(os.path.join(vcsdir, "objects"))
        os.makedirs(os.path.join(vcsdir, "refs", "heads"))
        repo = Repo(root)
        repo._write_atomic(repo.head_path, b"ref: refs/heads/main\n")
        repo._write_atomic(repo.index_path, b'{"version": 1, "entries": []}')
        repo._write_atomic(repo.config_path, b"{}")
        return repo

    # -------------------------------------------------------------- 原子写入

    @staticmethod
    def _write_atomic(path: str, data: bytes) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)

    # ----------------------------------------------------------------- 配置

    def config_get(self, key: str) -> Optional[str]:
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        return data.get(key)

    def config_set(self, key: str, value: str) -> None:
        data: dict = {}
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        data[key] = value
        self._write_atomic(
            self.config_path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()
        )

    def author_identity(self) -> Tuple[str, str]:
        name = os.environ.get("MVCS_AUTHOR_NAME") or self.config_get("user.name")
        email = os.environ.get("MVCS_AUTHOR_EMAIL") or self.config_get("user.email")
        if not name or not email:
            raise RepoError("缺少作者信息：请先 config user.name/user.email"
                            "（或设置 MVCS_AUTHOR_NAME / MVCS_AUTHOR_EMAIL）")
        return name, email

    @staticmethod
    def author_timestamp() -> int:
        # 测试可用 MVCS_AUTHOR_DATE 固定时间，得到稳定 commit ID
        env = os.environ.get("MVCS_AUTHOR_DATE")
        if env:
            return int(env)
        return int(time.time())

    # ----------------------------------------------------------------- 引用

    def head_refname(self) -> Optional[str]:
        """当前分支名；游离 HEAD 返回 None。未出生分支同样返回分支名。"""
        try:
            with open(self.head_path, "r", encoding="ascii") as f:
                content = f.read().strip()
        except OSError:
            return None
        if content.startswith("ref: "):
            ref = content[5:].strip()
            prefix = "refs/heads/"
            return ref[len(prefix):] if ref.startswith(prefix) else ref
        return None

    def head_is_detached(self) -> bool:
        try:
            with open(self.head_path, "r", encoding="ascii") as f:
                content = f.read().strip()
        except OSError:
            return False
        return not content.startswith("ref: ")

    def _ref_path(self, name: str) -> str:
        if not name or "/" in name and name.startswith("/") or ".." in name or "\\" in name:
            raise RepoError(f"非法分支名：{name!r}")
        return os.path.join(self.refs_dir, *name.split("/"))

    def branch_exists(self, name: str) -> bool:
        return os.path.isfile(self._ref_path(name))

    def resolve_branch(self, name: str) -> Optional[str]:
        path = self._ref_path(name)
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="ascii") as f:
            oid = f.read().strip()
        return oid if len(oid) == obj.ID_LEN else None

    def update_branch(self, name: str, oid: str) -> None:
        self._write_atomic(self._ref_path(name), (oid + "\n").encode("ascii"))

    def list_branches(self) -> List[str]:
        if not os.path.isdir(self.refs_dir):
            return []
        out = []
        for dirpath, _dirs, files in os.walk(self.refs_dir):
            for fn in files:
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, self.refs_dir).replace(os.sep, "/")
                out.append(rel)
        return sorted(out)

    def resolve_target(self, target: str) -> str:
        """把分支名或完整/前缀提交 ID 解析为 commit ID。"""
        if self.branch_exists(target):
            oid = self.resolve_branch(target)
            if oid:
                return oid
            raise RepoError(f"分支 {target!r} 引用无效")
        if len(target) == obj.ID_LEN and all(c in "0123456789abcdef" for c in target):
            if not self.store.has(target):
                raise RepoError(f"提交不存在：{target}")
            self.store.read(target, COMMIT)
            return target
        # 短 ID 前缀
        if 4 <= len(target) < obj.ID_LEN and all(c in "0123456789abcdef" for c in target):
            matches = [i for i in self.store.iter_ids() if i.startswith(target)]
            if len(matches) == 1:
                oid = matches[0]
                self.store.read(oid, COMMIT)
                return oid
            if not matches:
                raise RepoError(f"提交不存在：{target}")
            raise RepoError(f"短 ID {target!r} 不唯一")
        raise RepoError(f"无法解析的引用：{target!r}")

    def head_commit(self) -> Optional[str]:
        """HEAD 指向的 commit ID；未出生分支/无提交返回 None。"""
        if self.head_is_detached():
            with open(self.head_path, "r", encoding="ascii") as f:
                oid = f.read().strip()
            if self.store.has(oid):
                return oid
            return None
        branch = self.head_refname()
        if branch is None:
            return None
        return self.resolve_branch(branch)

    def set_head_to_branch(self, name: str) -> None:
        self._write_atomic(self.head_path,
                           (f"ref: refs/heads/{name}\n").encode("ascii"))

    def set_head_detached(self, oid: str) -> None:
        self._write_atomic(self.head_path, (oid + "\n").encode("ascii"))

    # ----------------------------------------------------------------- 索引

    def read_index(self) -> List[dict]:
        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            raise RepoError(f"索引无法读取：{exc}") from exc
        entries = data.get("entries", [])
        entries.sort(key=lambda e: e["path"])
        return entries

    def write_index(self, entries: List[dict]) -> None:
        norm = sorted(
            ({"path": e["path"], "mode": e["mode"], "oid": e["oid"]} for e in entries),
            key=lambda e: e["path"],
        )
        data = json.dumps({"version": 1, "entries": norm},
                          ensure_ascii=False, indent=2) + "\n"
        self._write_atomic(self.index_path, data.encode("utf-8"))

    def index_flat(self) -> FlatTree:
        return {e["path"]: (e["mode"], BLOB, e["oid"]) for e in self.read_index()}

    # ----------------------------------------------------------------- 树操作

    def flatten_tree(self, tree_id: str, prefix: str = "") -> FlatTree:
        """递归展开 tree 为平铺路径映射。"""
        out: FlatTree = {}
        payload = self.store.read(tree_id, TREE)
        for mode, otype, name, oid in obj.parse_tree(payload):
            path = f"{prefix}{name}"
            if otype == TREE:
                out.update(self.flatten_tree(oid, path + "/"))
            else:
                out[path] = (mode, BLOB, oid)
        return out

    def tree_from_index(self) -> Optional[str]:
        entries = self.read_index()
        if not entries:
            return None
        flat = {e["path"]: (e["mode"], BLOB, e["oid"]) for e in entries}
        return obj.write_tree(self.store, flat)

    def commit_tree(self, flat: FlatTree) -> str:
        return obj.write_tree(self.store, flat)

    # ---------------------------------------------------------------- 工作区

    def worktree_files(self) -> List[str]:
        """工作区内受版本管理的文件路径（相对 root），跳过 .minivcs 元数据目录。"""
        out = []
        for dirpath, dirs, files in os.walk(self.root):
            # 就地剪枝：从遍历时就排除 .minivcs
            dirs[:] = sorted(d for d in dirs
                             if not (os.path.abspath(os.path.join(dirpath, d))
                                     == os.path.abspath(self.vcsdir)))
            for fn in sorted(files):
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, self.root).replace(os.sep, "/")
                if rel.split("/")[0] == DIR_NAME:
                    continue
                out.append(rel)
        return sorted(set(out))

    def _file_mode(self, full: str) -> str:
        return EXEC_MODE if os.stat(full).st_mode & stat.S_IXUSR else FILE_MODE

    def hash_worktree_file(self, rel: str) -> Tuple[str, str]:
        full = os.path.join(self.root, rel)
        with open(full, "rb") as f:
            data = f.read()
        return self.store.put_blob(data), self._file_mode(full)

    def worktree_flat(self) -> FlatTree:
        """哈希所有工作区文件（副作用：写入 blob，实现去重）。"""
        flat: FlatTree = {}
        for rel in self.worktree_files():
            oid, mode = self.hash_worktree_file(rel)
            flat[rel] = (mode, BLOB, oid)
        return flat

    # ------------------------------------------------------------------- add

    def expand_paths(self, paths: List[str]) -> List[str]:
        result = set()
        all_files = set(self.worktree_files())
        for raw in paths:
            norm = os.path.normpath(raw).replace(os.sep, "/")
            if norm == ".":
                result.update(all_files)
                continue
            full = os.path.join(self.root, norm)
            if os.path.isdir(full):
                prefix = norm.rstrip("/") + "/"
                result.update(p for p in all_files
                              if p == norm or p.startswith(prefix))
            elif os.path.isfile(full):
                if norm.split("/")[0] == DIR_NAME:
                    raise RepoError(f"不能添加元数据目录中的文件：{norm}")
                result.add(norm)
            else:
                raise RepoError(f"路径不存在：{norm}")
        return sorted(result)

    def add(self, paths: List[str]) -> List[str]:
        entries = self.read_index()
        by_path = {e["path"]: e for e in entries}
        tracked = set(by_path)
        existing: List[str] = []
        deleted: List[str] = []
        for raw in paths:
            norm = os.path.normpath(raw).replace(os.sep, "/")
            if norm == ".":
                existing.extend(self.expand_paths(["."]))
                # add . 同时暂存工作区中已消失的被跟踪文件（Git 语义）
                deleted.extend(sorted(tracked - set(self.worktree_files())))
                continue
            full = os.path.join(self.root, norm)
            if os.path.isdir(full):
                existing.extend(self.expand_paths([norm]))
            elif os.path.isfile(full):
                if norm.split("/")[0] == DIR_NAME:
                    raise RepoError(f"不能添加元数据目录中的文件：{norm}")
                existing.append(norm)
            elif norm in tracked:
                # 已跟踪文件已从工作区删除 → 暂存该删除
                deleted.append(norm)
            else:
                raise RepoError(f"路径不存在：{norm}")

        targets = sorted(set(existing))
        changed: List[str] = []
        for rel in targets:
            full = os.path.join(self.root, rel)
            with open(full, "rb") as f:
                data = f.read()
            oid = self.store.put_blob(data)
            mode = self._file_mode(full)
            old = by_path.get(rel)
            if old is None or old["oid"] != oid or old["mode"] != mode:
                changed.append(rel)
            by_path[rel] = {"path": rel, "mode": mode, "oid": oid}
        for rel in set(deleted):
            by_path.pop(rel, None)
            changed.append(rel)
        self.write_index(list(by_path.values()))
        return sorted(set(changed))

    # ---------------------------------------------------------------- commit

    def commit(self, message: str) -> Tuple[str, bool]:
        """创建提交，返回 (commit ID, 是否为空提交)。允许空提交。"""
        message = message.strip()
        if not message:
            raise RepoError("提交消息不能为空")
        entries = self.read_index()
        flat = {e["path"]: (e["mode"], BLOB, e["oid"]) for e in entries}
        tree_id = self.commit_tree(flat)
        parent = self.head_commit()
        is_empty = False
        if parent is not None:
            parent_tree = self.store.read(parent, COMMIT)
            pinfo = obj.parse_commit(parent_tree)
            is_empty = pinfo["tree"] == tree_id
        name, email = self.author_identity()
        ts = self.author_timestamp()
        payload = obj.build_commit_payload(
            tree_id,
            [parent] if parent else [],
            name, email, ts, message,
        )
        cid = self.store.put(COMMIT, payload)
        if self.head_is_detached():
            self.set_head_detached(cid)
        else:
            branch = self.head_refname() or "main"
            self.update_branch(branch, cid)
            self.set_head_to_branch(branch)
        return cid, is_empty

    def commit_info(self, oid: str) -> dict:
        return obj.parse_commit(self.store.read(oid, COMMIT))

    def ancestors(self, start: str) -> Dict[str, int]:
        """BFS 所有祖先提交（含自身），值为代数距离。"""
        seen = {start: 0}
        queue = [(start, 0)]
        while queue:
            cur, depth = queue.pop(0)
            info = self.commit_info(cur)
            for p in info["parents"]:
                if p not in seen:
                    seen[p] = depth + 1
                    queue.append((p, depth + 1))
        return seen

    # ---------------------------------------------------------------- status

    def status(self) -> dict:
        """返回工作区状态。

        - staged:    相对 HEAD 的索引差异 [(状态, 路径)]，状态 A/M/D
        - unstaged:  相对索引的工作区差异 [(状态, 路径)]，状态 ?/? 之外 A/M/D，
                     其中未跟踪文件单独放 untracked
        - untracked: 工作区有、索引和 HEAD 都没有
        """
        head = self.head_commit()
        head_flat: FlatTree = {}
        if head:
            head_flat = self.flatten_tree(self.commit_info(head)["tree"])
        index_flat = self.index_flat()

        staged: List[Tuple[str, str]] = []
        for path in sorted(set(head_flat) | set(index_flat)):
            h = head_flat.get(path)
            i = index_flat.get(path)
            if h is None and i is not None:
                staged.append(("A", path))
            elif h is not None and i is None:
                staged.append(("D", path))
            elif h is not None and i is not None and (h[0], h[2]) != (i[0], i[2]):
                staged.append(("M", path))

        wt_flat = self.worktree_flat()
        unstaged: List[Tuple[str, str]] = []
        untracked: List[str] = []
        for path in sorted(set(wt_flat) | set(index_flat)):
            w = wt_flat.get(path)
            i = index_flat.get(path)
            h = head_flat.get(path)
            if i is None:
                if w is not None and h is None:
                    untracked.append(path)
                elif w is not None and h is not None and (w[0], w[2]) != (h[0], h[2]):
                    # 被跟踪文件已暂存删除后工作区仍有修改内容：报告为未跟踪不合适，
                    # 此处归入未暂段修改
                    unstaged.append(("M", path))
                continue
            if w is None:
                unstaged.append(("D", path))
            elif (w[0], w[2]) != (i[0], i[2]):
                unstaged.append(("M", path))
        return {"staged": staged, "unstaged": unstaged,
                "untracked": sorted(set(untracked)), "head": head}
