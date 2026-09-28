"""内容寻址对象存储与 blob / tree / commit 三种对象的序列化。

磁盘格式（与 Git 思路一致，但完全独立实现）::

    <type> <十进制长度>\n<payload>      —— 存储前的规范化内容
    SHA-256(规范化内容)                —— 对象 ID（稳定、与内容一一对应）
    zlib.compress(规范化内容)          —— 实际落盘字节

对象存放于 ``.minivcs/objects/<id前2位>/<id后62位>``；
相同内容的对象 ID 必然相同，写入时只落盘一次，实现内容去重。
"""

from __future__ import annotations

import hashlib
import os
import zlib
from typing import Dict, Iterable, List, Optional, Tuple

BLOB = "blob"
TREE = "tree"
COMMIT = "commit"
TYPES = (BLOB, TREE, COMMIT)

ID_LEN = 64  # SHA-256 hex 长度


class VCSObjectError(Exception):
    """对象缺失、哈希不符、格式非法等底层错误。"""


def encode(obj_type: str, payload: bytes) -> Tuple[str, bytes]:
    """返回 (对象ID, 压缩字节)。"""
    if obj_type not in TYPES:
        raise VCSObjectError(f"unknown object type: {obj_type!r}")
    if not isinstance(payload, (bytes, bytearray)):
        raise VCSObjectError("payload must be bytes")
    content = f"{obj_type} {len(payload)}\n".encode("ascii") + bytes(payload)
    return hashlib.sha256(content).hexdigest(), zlib.compress(content)


def object_id(obj_type: str, payload: bytes) -> str:
    """仅计算对象 ID，不落盘。"""
    return encode(obj_type, payload)[0]


def blob_id(data: bytes) -> str:
    return object_id(BLOB, data)


# ---------------------------------------------------------------- tree 对象

# tree 条目：<模式> <类型字符> <名称>\t<对象ID>
# 模式采用八进制：普通文件 100644 / 可执行 100755；目录条目模式为 40000。
FILE_MODE = "100644"
EXEC_MODE = "100755"
DIR_MODE = "40000"


def build_tree(entries: Dict[str, Tuple[str, str, str]]) -> Tuple[str, bytes]:
    """由平铺的 ``{路径: (模式, 类型, 对象ID)}`` 递归构造 tree 对象。

    返回顶层 tree 的 (ID, payload)。
    """
    # 组织成嵌套字典：叶子是 (mode,type,id)，目录是 dict
    root: dict = {}
    for path in sorted(entries):
        parts = path.split("/")
        node = root
        for part in parts[:-1]:
            nxt = node.get(part)
            if nxt is None:
                nxt = {}
                node[part] = nxt
            if not isinstance(nxt, dict):
                raise VCSObjectError(f"path/file conflict at {part!r} in {path!r}")
            node = nxt
        name = parts[-1]
        if name in node:
            raise VCSObjectError(f"duplicate tree entry: {path!r}")
        node[name] = entries[path]

    def render(node: dict) -> Tuple[str, bytes]:
        children: List[Tuple[str, str, str, str]] = []  # (name, mode, type, id)
        for name in sorted(node):
            val = node[name]
            if isinstance(val, dict):
                tid, _ = render(val)  # 递归渲染，子 tree 已在下方收集
                children.append((name, DIR_MODE, TREE, tid))
            else:
                mode, otype, oid = val
                if otype not in (BLOB, TREE):
                    raise VCSObjectError(f"tree entry {name!r} has bad type {otype!r}")
                children.append((name, mode, otype, oid))
        lines = []
        for name, mode, otype, oid in children:
            type_char = "t" if otype == TREE else "b"
            lines.append(f"{mode} {type_char} {name}\t{oid}\n".encode("utf-8"))
        payload = b"".join(lines)
        return object_id(TREE, payload), payload

    tid, payload = render(root)
    return tid, payload


def write_tree(store: "ObjectStore",
               entries: Dict[str, Tuple[str, str, str]]) -> str:
    """构造整棵目录树并把**所有层级**的 tree 对象写入存储，返回顶层 tree ID。"""
    root: dict = {}
    for path in sorted(entries):
        parts = path.split("/")
        node = root
        for part in parts[:-1]:
            nxt = node.get(part)
            if nxt is None:
                nxt = {}
                node[part] = nxt
            if not isinstance(nxt, dict):
                raise VCSObjectError(f"path/file conflict at {part!r} in {path!r}")
            node = nxt
        name = parts[-1]
        if name in node:
            raise VCSObjectError(f"duplicate tree entry: {path!r}")
        node[name] = entries[path]

    def render(node: dict) -> str:
        lines = []
        for name in sorted(node):
            val = node[name]
            if isinstance(val, dict):
                tid = render(val)
                lines.append(f"{DIR_MODE} t {name}\t{tid}\n".encode("utf-8"))
            else:
                mode, otype, oid = val
                if otype not in (BLOB, TREE):
                    raise VCSObjectError(
                        f"tree entry {name!r} has bad type {otype!r}")
                type_char = "t" if otype == TREE else "b"
                lines.append(f"{mode} {type_char} {name}\t{oid}\n".encode("utf-8"))
        return store.put(TREE, b"".join(lines))

    return render(root)


def parse_tree(payload: bytes) -> List[Tuple[str, str, str, str]]:
    """解析 tree payload，返回 ``[(mode, 类型, 名称, 对象ID), ...]``。"""
    out: List[Tuple[str, str, str, str]] = []
    for raw in payload.splitlines():
        try:
            head, oid = raw.decode("utf-8").split("\t", 1)
            mode, type_char, name = head.split(" ", 2)
        except (UnicodeDecodeError, ValueError) as exc:
            raise VCSObjectError(f"malformed tree entry: {raw!r}") from exc
        if type_char not in ("b", "t") or len(oid) != ID_LEN:
            raise VCSObjectError(f"malformed tree entry: {raw!r}")
        out.append((mode, TREE if type_char == "t" else BLOB, name, oid))
    return out


# -------------------------------------------------------------- commit 对象


def build_commit_payload(
    tree: str,
    parents: Iterable[str],
    author_name: str,
    author_email: str,
    timestamp: int,
    message: str,
) -> bytes:
    """commit payload（UTF-8 文本）。

    头部行 ``tree`` / ``parent`` / ``author`` / ``committer`` 顺序固定，
    空行之后是提交消息（消息末尾规范化为单个换行）。
    """
    lines = [f"tree {tree}"]
    for p in parents:
        if len(p) != ID_LEN:
            raise VCSObjectError(f"bad parent id: {p!r}")
        lines.append(f"parent {p}")
    stamp = f"{author_name} <{author_email}> {int(timestamp)} +0000"
    lines.append(f"author {stamp}")
    lines.append(f"committer {stamp}")
    lines.append("")
    msg = message if message.endswith("\n") else message + "\n"
    return ("\n".join(lines) + "\n" + msg).encode("utf-8")


def parse_commit(payload: bytes) -> dict:
    """解析 commit payload，返回字典。

    keys: tree, parents(list), author(name/email/timestamp), message
    """
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VCSObjectError("commit payload is not valid utf-8") from exc
    if "\n\n" not in text:
        raise VCSObjectError("commit missing header/message separator")
    header, message = text.split("\n\n", 1)
    result = {
        "tree": None,
        "parents": [],
        "author": None,
        "committer": None,
        "message": message,
    }
    for line in header.split("\n"):
        key, _, val = line.partition(" ")
        if key == "tree":
            if result["tree"] is not None or len(val) != ID_LEN:
                raise VCSObjectError("bad tree line in commit")
            result["tree"] = val
        elif key == "parent":
            if len(val) != ID_LEN:
                raise VCSObjectError("bad parent line in commit")
            result["parents"].append(val)
        elif key in ("author", "committer"):
            m = val.rpartition(">")
            # val 形如: Name <email> 123 +0000
            left, _, tail = m[0], m[1], m[2]
            if "<" not in left or not tail.strip().split():
                raise VCSObjectError(f"bad {key} line in commit")
            name, _, email = left.partition(" <")
            ts = int(tail.strip().split()[0])
            result[key] = {"name": name, "email": email, "timestamp": ts}
        else:
            raise VCSObjectError(f"unknown commit header: {key!r}")
    if result["tree"] is None:
        raise VCSObjectError("commit missing tree")
    return result


# ----------------------------------------------------------------- 存储后端


class ObjectStore:
    """``.minivcs/objects`` 下的压缩内容寻址存储。"""

    def __init__(self, objects_dir: str):
        self.objects_dir = objects_dir

    def _path(self, oid: str) -> str:
        return os.path.join(self.objects_dir, oid[:2], oid[2:])

    def has(self, oid: str) -> bool:
        return os.path.isfile(self._path(oid))

    def put(self, obj_type: str, payload: bytes) -> str:
        """写入对象；已存在（相同内容）则直接复用，绝不重复存储。"""
        oid, compressed = encode(obj_type, payload)
        path = self._path(oid)
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(compressed)
            os.replace(tmp, path)  # 原子改名
        return oid

    def put_blob(self, data: bytes) -> str:
        return self.put(BLOB, data)

    def read_raw(self, oid: str) -> Tuple[str, bytes]:
        """读取并校验，返回 (type, payload)。缺失或损坏抛 VCSObjectError。"""
        path = self._path(oid)
        if not os.path.isfile(path):
            raise VCSObjectError(f"missing object {oid}")
        try:
            with open(path, "rb") as f:
                content = zlib.decompress(f.read())
        except (zlib.error, OSError) as exc:
            raise VCSObjectError(f"corrupt object {oid}: cannot decompress") from exc
        try:
            header, payload = content.split(b"\n", 1)
            otype_b, size_b = header.split(b" ", 1)
            otype = otype_b.decode("ascii")
            size = int(size_b)
        except (ValueError, UnicodeDecodeError) as exc:
            raise VCSObjectError(f"corrupt object {oid}: bad header") from exc
        if otype not in TYPES:
            raise VCSObjectError(f"corrupt object {oid}: unknown type {otype!r}")
        if size != len(payload):
            raise VCSObjectError(f"corrupt object {oid}: size mismatch")
        actual = hashlib.sha256(content).hexdigest()
        if actual != oid:
            raise VCSObjectError(f"corrupt object {oid}: hash mismatch (content is {actual})")
        return otype, payload

    def read(self, oid: str, expected_type: Optional[str] = None) -> bytes:
        otype, payload = self.read_raw(oid)
        if expected_type is not None and otype != expected_type:
            raise VCSObjectError(
                f"object {oid} is {otype}, expected {expected_type}"
            )
        return payload

    def iter_ids(self) -> Iterable[str]:
        if not os.path.isdir(self.objects_dir):
            return
        for part in sorted(os.listdir(self.objects_dir)):
            sub = os.path.join(self.objects_dir, part)
            if len(part) != 2 or not os.path.isdir(sub):
                continue
            for rest in sorted(os.listdir(sub)):
                oid = part + rest
                if len(oid) == ID_LEN:
                    yield oid
