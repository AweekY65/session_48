"""仓库核心：init / add / commit / status / checkout / branch / merge / fsck。"""

import json
import os
import time

from .diffutil import file_diff, unified_diff, split_lines
from .merge import merge_file_maps
from .objects import ObjectStore, hash_bytes

MVCS_DIR = ".mvcs"
DEFAULT_BRANCH = "main"


class MvcsError(Exception):
    pass


class CheckoutConflict(MvcsError):
    def __init__(self, paths):
        self.paths = sorted(paths)
        super().__init__(
            "checkout 会覆盖未提交的修改，已中止: " + ", ".join(self.paths)
        )


class MergeConflict(MvcsError):
    def __init__(self, conflicts):
        self.conflicts = conflicts
        paths = [c["path"] for c in conflicts]
        super().__init__("合并冲突: " + ", ".join(paths))


def find_repo(start="."):
    """从 start 向上查找包含 .mvcs 的目录，返回仓库根路径。"""
    path = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(path, MVCS_DIR)):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            raise MvcsError("当前目录及父目录都不是 mvcs 仓库（缺少 .mvcs）")
        path = parent


class Repository:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.mvcs_dir = os.path.join(self.root, MVCS_DIR)
        self.store = ObjectStore(os.path.join(self.mvcs_dir, "objects"))

    # ---------- 初始化 ----------

    @classmethod
    def init(cls, path="."):
        root = os.path.abspath(path)
        mvcs_dir = os.path.join(root, MVCS_DIR)
        if os.path.isdir(mvcs_dir):
            raise MvcsError(f"仓库已存在: {mvcs_dir}")
        os.makedirs(os.path.join(mvcs_dir, "objects"))
        os.makedirs(os.path.join(mvcs_dir, "refs", "heads"))
        with open(os.path.join(mvcs_dir, "HEAD"), "w") as f:
            f.write(f"ref: refs/heads/{DEFAULT_BRANCH}\n")
        with open(os.path.join(mvcs_dir, "index.json"), "w") as f:
            json.dump({}, f)
        with open(os.path.join(mvcs_dir, "config.json"), "w") as f:
            json.dump({"user.name": "unknown", "user.email": "unknown@localhost"}, f)
        return cls(root)

    # ---------- 配置 ----------

    def _config_path(self):
        return os.path.join(self.mvcs_dir, "config.json")

    def get_config(self, key, default=None):
        with open(self._config_path()) as f:
            return json.load(f).get(key, default)

    def set_config(self, key, value):
        with open(self._config_path()) as f:
            cfg = json.load(f)
        cfg[key] = value
        with open(self._config_path(), "w") as f:
            json.dump(cfg, f, indent=2)

    # ---------- 索引 ----------

    def _index_path(self):
        return os.path.join(self.mvcs_dir, "index.json")

    def read_index(self) -> dict:
        with open(self._index_path()) as f:
            return json.load(f)

    def write_index(self, index: dict):
        with open(self._index_path(), "w") as f:
            json.dump(index, f, indent=1, sort_keys=True)

    # ---------- 引用 ----------

    def _ref_path(self, name):
        return os.path.join(self.mvcs_dir, "refs", "heads", name)

    def list_branches(self):
        heads = os.path.join(self.mvcs_dir, "refs", "heads")
        return sorted(os.listdir(heads)) if os.path.isdir(heads) else []

    def read_ref(self, name):
        path = self._ref_path(name)
        if not os.path.isfile(path):
            return None
        with open(path) as f:
            return f.read().strip()

    def write_ref(self, name, commit_id):
        with open(self._ref_path(name), "w") as f:
            f.write(commit_id + "\n")

    def head_ref(self):
        """返回 ('branch', name) 或 ('detached', commit_id)。"""
        with open(os.path.join(self.mvcs_dir, "HEAD")) as f:
            content = f.read().strip()
        if content.startswith("ref: "):
            return ("branch", content[5:].split("/")[-1])
        return ("detached", content)

    def head_commit(self):
        kind, value = self.head_ref()
        if kind == "detached":
            return value
        return self.read_ref(value)

    def _set_head_branch(self, name):
        with open(os.path.join(self.mvcs_dir, "HEAD"), "w") as f:
            f.write(f"ref: refs/heads/{name}\n")

    def _set_head_detached(self, commit_id):
        with open(os.path.join(self.mvcs_dir, "HEAD"), "w") as f:
            f.write(commit_id + "\n")

    # ---------- 对象读写 ----------

    def hash_workdir_file(self, path):
        full = os.path.join(self.root, path)
        if not os.path.isfile(full):
            return None
        with open(full, "rb") as f:
            data = f.read()
        return hash_bytes(b"blob %d\0" % len(data) + data)

    def read_blob(self, oid) -> bytes:
        obj_type, payload = self.store.read(oid)
        if obj_type != "blob":
            raise MvcsError(f"对象 {oid} 不是 blob")
        return payload

    def write_blob(self, data: bytes) -> str:
        return self.store.write("blob", data)

    # ---------- tree ----------

    def write_tree(self, path_hash_map: dict) -> str:
        """把 路径->blobID 扁平映射写成嵌套 tree 对象，返回根 tree ID。"""
        dirs = {}   # dir_path -> {name: ("blob"|"tree", oid)}
        for path, oid in sorted(path_hash_map.items()):
            parts = path.split("/")
            for depth in range(len(parts)):
                dir_path = "/".join(parts[:depth])
                name = parts[depth]
                if depth == len(parts) - 1:
                    entry = ("blob", oid)
                else:
                    entry = None  # 占位，子树哈希后填
                dirs.setdefault(dir_path, {})
                if entry is not None:
                    dirs[dir_path][name] = entry
                else:
                    dirs[dir_path].setdefault(name, None)
        # 自底向上计算 tree 哈希
        tree_ids = {}

        def build(dir_path):
            entries = dirs.get(dir_path, {})
            lines = []
            for name in sorted(entries):
                entry = entries[name]
                if entry is None:
                    child = "/".join([dir_path, name]) if dir_path else name
                    entry = ("tree", build(child))
                lines.append(f"{entry[0]} {entry[1]} {name}")
            payload = "\n".join(lines).encode()
            oid = self.store.write("tree", payload)
            tree_ids[dir_path] = oid
            return oid

        return build("")

    def read_tree_flat(self, tree_id, prefix="") -> dict:
        """把 tree 对象展开为 路径->blobID 扁平映射。"""
        result = {}
        if tree_id is None:
            return result
        obj_type, payload = self.store.read(tree_id)
        if obj_type != "tree":
            raise MvcsError(f"对象 {tree_id} 不是 tree")
        text = payload.decode()
        for line in text.split("\n"):
            if not line:
                continue
            typ, oid, name = line.split(" ", 2)
            path = f"{prefix}/{name}" if prefix else name
            if typ == "blob":
                result[path] = oid
            else:
                result.update(self.read_tree_flat(oid, path))
        return result

    # ---------- commit ----------

    def read_commit(self, commit_id) -> dict:
        obj_type, payload = self.store.read(commit_id)
        if obj_type != "commit":
            raise MvcsError(f"对象 {commit_id} 不是 commit")
        header, _, message = payload.partition(b"\n\n")
        commit = {"tree": None, "parents": [], "author": None,
                  "timestamp": None, "message": message.decode("utf-8", "replace")}
        for line in header.decode().split("\n"):
            if line.startswith("tree "):
                commit["tree"] = line[5:]
            elif line.startswith("parent "):
                commit["parents"].append(line[7:])
            elif line.startswith("author "):
                # author Name <email> timestamp
                meta, _, ts = line[7:].rpartition(" ")
                commit["author"] = meta
                commit["timestamp"] = int(ts)
        return commit

    def _write_commit(self, tree_id, parents, author, timestamp, message) -> str:
        lines = [f"tree {tree_id}"]
        lines += [f"parent {p}" for p in parents]
        lines.append(f"author {author} {timestamp}")
        payload = ("\n".join(lines) + "\n\n" + message).encode()
        return self.store.write("commit", payload)

    def _author_string(self, author=None):
        if author:
            return author
        name = self.get_config("user.name", "unknown")
        email = self.get_config("user.email", "unknown@localhost")
        return f"{name} <{email}>"

    def commit(self, message, author=None, timestamp=None, parents=None) -> str:
        """根据当前索引创建提交，返回 commit ID。

        commit ID 由 tree、父提交、作者、时间戳和消息共同决定，内容稳定。
        """
        index = self.read_index()
        tree_id = self.write_tree({p: e["hash"] for p, e in index.items()})
        if parents is None:
            head = self.head_commit()
            parents = [head] if head else []
        author = self._author_string(author)
        if timestamp is None:
            timestamp = int(time.time())
        commit_id = self._write_commit(tree_id, parents, author, timestamp, message)
        kind, value = self.head_ref()
        if kind == "branch":
            self.write_ref(value, commit_id)
        else:
            self._set_head_detached(commit_id)
        return commit_id

    # ---------- add / status ----------

    def _workdir_files(self):
        """遍历工作区（排除 .mvcs），返回相对路径列表。"""
        files = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            rel = os.path.relpath(dirpath, self.root)
            if rel == ".":
                dirnames[:] = [d for d in dirnames if d != MVCS_DIR]
            elif rel.split(os.sep)[0] == MVCS_DIR:
                dirnames[:] = []
                continue
            for name in filenames:
                full = os.path.join(dirpath, name)
                files.append(os.path.relpath(full, self.root))
        return sorted(files)

    def add(self, paths):
        """把文件加入索引（计算 blob 并写入对象库，相同内容自动去重）。"""
        index = self.read_index()
        added = []
        targets = []
        for p in paths:
            full = os.path.join(self.root, p)
            if os.path.isdir(full):
                for dirpath, dirnames, filenames in os.walk(full):
                    rel = os.path.relpath(dirpath, self.root)
                    if rel.split(os.sep)[0] == MVCS_DIR:
                        dirnames[:] = []
                        continue
                    dirnames[:] = [d for d in dirnames if d != MVCS_DIR]
                    for name in filenames:
                        targets.append(os.path.relpath(
                            os.path.join(dirpath, name), self.root))
            elif os.path.isfile(full):
                targets.append(os.path.relpath(full, self.root))
            else:
                raise MvcsError(f"路径不存在: {p}")
        for rel in sorted(set(targets)):
            full = os.path.join(self.root, rel)
            with open(full, "rb") as f:
                data = f.read()
            oid = self.write_blob(data)
            st = os.stat(full)
            index[rel] = {"hash": oid, "size": st.st_size,
                          "mtime_ns": st.st_mtime_ns}
            added.append(rel)
        self.write_index(index)
        return added

    def status(self) -> dict:
        """返回 {"staged": {...}, "unstaged": [...], "untracked": [...]}。"""
        index = self.read_index()
        head = self.head_commit()
        head_map = {}
        if head:
            head_map = self.read_tree_flat(self.read_commit(head)["tree"])
        index_map = {p: e["hash"] for p, e in index.items()}
        staged = file_diff(head_map, index_map)

        unstaged = []
        for path, entry in index.items():
            full = os.path.join(self.root, path)
            if not os.path.isfile(full):
                unstaged.append(f"deleted: {path}")
            elif self.hash_workdir_file(path) != entry["hash"]:
                unstaged.append(f"modified: {path}")
        untracked = [p for p in self._workdir_files() if p not in index]
        return {"staged": staged, "unstaged": sorted(unstaged),
                "untracked": untracked}

    # ---------- checkout / branch ----------

    def resolve_revision(self, rev) -> str:
        """把分支名或（短）commit ID 解析为完整 commit ID。"""
        if rev in self.list_branches():
            return self.read_ref(rev)
        oid = self.store.find_prefix(rev)
        if oid is None:
            raise MvcsError(f"无法解析的引用: {rev}")
        obj_type, _ = self.store.read(oid)
        if obj_type != "commit":
            raise MvcsError(f"{rev} 不是 commit 对象")
        return oid

    def create_branch(self, name, start=None):
        if name in self.list_branches():
            raise MvcsError(f"分支已存在: {name}")
        commit_id = self.resolve_revision(start) if start else self.head_commit()
        if commit_id is None:
            raise MvcsError("还没有任何提交，无法创建分支")
        self.write_ref(name, commit_id)

    def _dirty_paths(self, head_map, target_map):
        """找出切换会被覆盖的本地未提交修改。"""
        index = self.read_index()
        dirty = []
        for path in set(head_map) | set(target_map):
            if head_map.get(path) == target_map.get(path):
                continue  # 两个版本一致，切换不影响该文件
            base = head_map.get(path)
            workdir_hash = self.hash_workdir_file(path)
            index_entry = index.get(path)
            index_hash = index_entry["hash"] if index_entry else None
            if base is None and index_hash is None and workdir_hash is not None:
                dirty.append(path)          # 未跟踪文件会被覆盖
            elif workdir_hash != base:      # 工作区相对 HEAD 有修改/删除
                dirty.append(path)
            elif index_hash is not None and index_hash != base:
                dirty.append(path)          # 已暂存但未提交的修改
        return dirty

    def _apply_tree(self, target_map, base_map):
        """把工作区和索引从 base_map 更新为 target_map（调用前必须已做冲突检查）。

        只处理两个版本间有差异的路径；内容一致的文件保持原样，
        本地未提交修改不会被覆盖。
        """
        index = self.read_index()
        changed = [p for p in set(base_map) | set(target_map)
                   if base_map.get(p) != target_map.get(p)]
        for path in sorted(changed):
            full = os.path.join(self.root, path)
            oid = target_map.get(path)
            if oid is None:
                # 目标中不存在 -> 删除
                if os.path.isfile(full):
                    os.remove(full)
                index.pop(path, None)
                continue
            os.makedirs(os.path.dirname(full) or self.root, exist_ok=True)
            with open(full, "wb") as f:
                f.write(self.read_blob(oid))
            st = os.stat(full)
            index[path] = {"hash": oid, "size": st.st_size,
                           "mtime_ns": st.st_mtime_ns}
        self.write_index(index)

    def checkout(self, target, create_branch=False):
        """切换分支或检出 commit。会覆盖未提交修改时中止并抛 CheckoutConflict。"""
        if create_branch:
            self.create_branch(target)
        branches = self.list_branches()
        if target in branches:
            commit_id = self.read_ref(target)
            move_to = ("branch", target)
        else:
            commit_id = self.resolve_revision(target)
            move_to = ("detached", commit_id)
        if commit_id is None:
            raise MvcsError(f"分支 {target} 还没有提交")
        target_map = self.read_tree_flat(self.read_commit(commit_id)["tree"])
        head = self.head_commit()
        head_map = {}
        if head:
            head_map = self.read_tree_flat(self.read_commit(head)["tree"])
        dirty = self._dirty_paths(head_map, target_map)
        if dirty:
            raise CheckoutConflict(dirty)
        self._apply_tree(target_map, head_map)
        if move_to[0] == "branch":
            self._set_head_branch(target)
        else:
            self._set_head_detached(commit_id)
        return commit_id

    # ---------- 历史 ----------

    def ancestors(self, start) -> dict:
        """BFS 返回 {commit_id: 最短深度}。"""
        dist = {start: 0}
        queue = [start]
        while queue:
            cid = queue.pop(0)
            commit = self.read_commit(cid)
            for p in commit["parents"]:
                if p not in dist:
                    dist[p] = dist[cid] + 1
                    queue.append(p)
        return dist

    def merge_base(self, a, b) -> str:
        """找两个 commit 的最近公共祖先，没有则返回 None。"""
        if a == b:
            return a
        dist_a = self.ancestors(a)
        # 从 b 逐层 BFS，第一个出现在 a 祖先集合中的即最近公共祖先
        visited = set()
        queue = [b]
        while queue:
            cid = queue.pop(0)
            if cid in visited:
                continue
            visited.add(cid)
            if cid in dist_a:
                return cid
            queue.extend(self.read_commit(cid)["parents"])
        return None

    def log(self, start=None):
        """从 start（默认 HEAD）遍历历史，按时间戳倒序返回 commit 列表。"""
        start = start or self.head_commit()
        if start is None:
            return []
        seen = set()
        commits = []
        stack = [start]
        while stack:
            cid = stack.pop()
            if cid in seen:
                continue
            seen.add(cid)
            commit = self.read_commit(cid)
            commit["id"] = cid
            commits.append(commit)
            stack.extend(commit["parents"])
        commits.sort(key=lambda c: (c["timestamp"] or 0), reverse=True)
        return commits

    # ---------- diff ----------

    def commit_map(self, commit_id) -> dict:
        return self.read_tree_flat(self.read_commit(commit_id)["tree"])

    def diff_commits(self, rev_a, rev_b) -> dict:
        """两个提交之间的文件级 + 行级 diff。"""
        a = self.resolve_revision(rev_a)
        b = self.resolve_revision(rev_b)
        map_a = self.commit_map(a)
        map_b = self.commit_map(b)
        files = file_diff(map_a, map_b)
        line_diffs = {}
        for path in files["modified"] + files["added"] + files["deleted"]:
            old = self.read_blob(map_a[path]) if path in map_a else b""
            new = self.read_blob(map_b[path]) if path in map_b else b""
            line_diffs[path] = unified_diff(
                old, new, f"a/{path}", f"b/{path}")
        return {"files": files, "lines": line_diffs, "a": a, "b": b}

    # ---------- merge ----------

    def merge(self, branch, author=None, timestamp=None):
        """把指定分支合并进当前分支。

        返回 (commit_id, conflicts)。无冲突时自动创建合并提交（两个父提交）；
        有冲突时不提交，冲突文件带标记写入工作区并抛 MergeConflict。
        """
        ours = self.head_commit()
        if ours is None:
            raise MvcsError("当前分支还没有提交")
        theirs = self.resolve_revision(branch)
        base = self.merge_base(ours, theirs)
        if base == theirs:
            return ours, []  # already up to date
        if base == ours:
            # fast-forward
            target_map = self.commit_map(theirs)
            head_map = self.commit_map(ours)
            dirty = self._dirty_paths(head_map, target_map)
            if dirty:
                raise CheckoutConflict(dirty)
            self._apply_tree(target_map, head_map)
            kind, value = self.head_ref()
            if kind == "branch":
                self.write_ref(value, theirs)
            return theirs, []

        base_map = self.commit_map(base) if base else {}
        our_map = self.commit_map(ours)
        their_map = self.commit_map(theirs)
        merged_map, conflicts = merge_file_maps(
            base_map, our_map, their_map, self.read_blob, self.write_blob)
        if conflicts:
            # 写入冲突文件（含双方内容），其余文件按合并结果更新
            self._apply_tree(merged_map, our_map)
            index = self.read_index()
            for c in conflicts:
                full = os.path.join(self.root, c["path"])
                os.makedirs(os.path.dirname(full) or self.root, exist_ok=True)
                with open(full, "wb") as f:
                    f.write(c["content"])
                index.pop(c["path"], None)  # 冲突文件不进入索引
            self.write_index(index)
            with open(os.path.join(self.mvcs_dir, "MERGE_HEAD"), "w") as f:
                f.write(theirs + "\n")
            raise MergeConflict(conflicts)

        tree_id = self.write_tree(merged_map)
        author = self._author_string(author)
        ts = timestamp if timestamp is not None else int(time.time())
        commit_id = self._write_commit(
            tree_id, [ours, theirs], author, ts,
            f"Merge branch '{branch}'")
        kind, value = self.head_ref()
        if kind == "branch":
            self.write_ref(value, commit_id)
        else:
            self._set_head_detached(commit_id)
        self._apply_tree(merged_map, our_map)
        return commit_id, []

    # ---------- fsck ----------

    def fsck(self) -> list:
        """完整性检查：缺失对象、损坏哈希、无效引用、不可达对象。

        返回问题描述字符串列表，空列表表示仓库健康。
        """
        problems = []
        disk_objects = set(self.store.list_all())
        valid_objects = set()

        # 1) 校验磁盘上每个对象：可解压、结构合法、哈希与内容一致
        for oid in sorted(disk_objects):
            try:
                raw = self.store.read_raw(oid)
            except Exception as e:
                problems.append(f"corrupt: 对象 {oid} 无法解压/读取 ({e})")
                continue
            actual = hash_bytes(raw)
            if actual != oid:
                problems.append(
                    f"corrupt: 对象 {oid} 哈希不匹配（内容哈希为 {actual}）")
                continue
            try:
                obj_type, payload = self.store.read(oid)
                self._validate_payload(obj_type, payload, oid)
                valid_objects.add(oid)
            except Exception as e:
                problems.append(f"corrupt: 对象 {oid} 结构非法 ({e})")

        # 2) 校验引用
        roots = []
        kind, value = self.head_ref()
        if kind == "branch" and value not in self.list_branches():
            # HEAD 指向的分支尚无任何提交是合法的（空仓库）
            head_path = self._ref_path(value)
            if os.path.isfile(head_path):
                problems.append(f"invalid-ref: HEAD 指向不存在的分支 {value}")
        for name in self.list_branches():
            target = self.read_ref(name)
            if not target or target not in valid_objects:
                problems.append(
                    f"invalid-ref: 分支 {name} 指向无效或缺失的对象 {target}")
                continue
            try:
                obj_type, _ = self.store.read(target)
                if obj_type != "commit":
                    problems.append(
                        f"invalid-ref: 分支 {name} 指向的对象不是 commit")
                    continue
            except Exception as e:
                problems.append(f"invalid-ref: 分支 {name} 无法解析 ({e})")
                continue
            roots.append(target)

        # 3) 从引用出发遍历可达对象，检查缺失
        reachable = set()
        stack = list(roots)
        while stack:
            oid = stack.pop()
            if oid in reachable:
                continue
            reachable.add(oid)
            if oid not in valid_objects:
                problems.append(f"missing: 引用的对象 {oid} 缺失或损坏")
                continue
            obj_type, payload = self.store.read(oid)
            for ref in self._object_refs(obj_type, payload):
                if ref not in reachable:
                    stack.append(ref)

        # 4) 不可达对象
        for oid in sorted(valid_objects - reachable):
            problems.append(f"unreachable: 对象 {oid} 不可达")

        return problems

    def _validate_payload(self, obj_type, payload, oid):
        if obj_type == "tree":
            for line in payload.decode().split("\n"):
                if not line:
                    continue
                parts = line.split(" ", 2)
                if len(parts) != 3 or parts[0] not in ("blob", "tree"):
                    raise ValueError(f"tree 条目非法: {line!r}")
        elif obj_type == "commit":
            header = payload.decode().split("\n\n", 1)[0]
            if not header.startswith("tree "):
                raise ValueError("commit 缺少 tree 字段")

    def _object_refs(self, obj_type, payload):
        """对象内部引用的其他对象 ID。"""
        refs = []
        if obj_type == "tree":
            for line in payload.decode().split("\n"):
                if line:
                    refs.append(line.split(" ", 2)[1])
        elif obj_type == "commit":
            header = payload.decode().split("\n\n", 1)[0]
            for line in header.split("\n"):
                if line.startswith("tree ") or line.startswith("parent "):
                    refs.append(line.split(" ", 1)[1])
        return refs
