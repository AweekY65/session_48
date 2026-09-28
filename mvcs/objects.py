"""内容寻址对象存储。

对象类型：blob / tree / commit。
序列化格式：``<type> <size>\\0<payload>``，整体做 SHA-256 得到对象 ID，
以 zlib 压缩后写入 ``.mvcs/objects/<id[:2]>/<id[2:]>``。
相同内容的对象 ID 相同，因此天然去重，不会重复存储。
"""

import hashlib
import os
import zlib

OBJECT_TYPES = ("blob", "tree", "commit")


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def serialize(obj_type: str, payload: bytes) -> bytes:
    if obj_type not in OBJECT_TYPES:
        raise ValueError(f"unknown object type: {obj_type}")
    return b"%s %d\0" % (obj_type.encode(), len(payload)) + payload


def object_id(obj_type: str, payload: bytes) -> str:
    return hash_bytes(serialize(obj_type, payload))


class ObjectStore:
    def __init__(self, objects_dir: str):
        self.dir = objects_dir

    def _path(self, oid: str) -> str:
        return os.path.join(self.dir, oid[:2], oid[2:])

    def exists(self, oid: str) -> bool:
        return os.path.isfile(self._path(oid))

    def write(self, obj_type: str, payload: bytes) -> str:
        """写入对象并返回对象 ID；已存在的相同内容对象直接复用（去重）。"""
        raw = serialize(obj_type, payload)
        oid = hash_bytes(raw)
        path = self._path(oid)
        if not os.path.isfile(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(zlib.compress(raw))
            os.replace(tmp, path)
        return oid

    def read_raw(self, oid: str) -> bytes:
        with open(self._path(oid), "rb") as f:
            compressed = f.read()
        return zlib.decompress(compressed)

    def read(self, oid: str) -> tuple:
        """返回 (type, payload)。文件缺失或损坏时抛出异常。"""
        raw = self.read_raw(oid)
        header, _, payload = raw.partition(b"\0")
        parts = header.split(b" ")
        if len(parts) != 2:
            raise ValueError(f"corrupt object header: {oid}")
        obj_type = parts[0].decode()
        size = int(parts[1])
        if obj_type not in OBJECT_TYPES:
            raise ValueError(f"unknown object type in {oid}: {obj_type}")
        if size != len(payload):
            raise ValueError(f"size mismatch in object {oid}")
        return obj_type, payload

    def list_all(self):
        """列出磁盘上全部对象 ID。"""
        oids = []
        if not os.path.isdir(self.dir):
            return oids
        for sub in sorted(os.listdir(self.dir)):
            subdir = os.path.join(self.dir, sub)
            if not os.path.isdir(subdir) or len(sub) != 2:
                continue
            for name in sorted(os.listdir(subdir)):
                if name.endswith(".tmp"):
                    continue
                oids.append(sub + name)
        return oids

    def find_prefix(self, prefix: str):
        """按短哈希前缀查找唯一对象，返回 oid 或 None；歧义时抛出异常。"""
        if len(prefix) < 4:
            raise ValueError("prefix too short")
        matches = [o for o in self.list_all() if o.startswith(prefix)]
        if len(matches) > 1:
            raise ValueError(f"ambiguous object prefix: {prefix}")
        return matches[0] if matches else None
