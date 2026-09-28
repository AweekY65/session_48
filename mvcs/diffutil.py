"""文件级与文本行级 diff，基于标准库 difflib，不依赖 Git。"""

import difflib


def file_diff(old_map: dict, new_map: dict) -> dict:
    """比较两个 路径->对象ID 的扁平映射，返回文件级差异。

    返回 {"added": [...], "deleted": [...], "modified": [...]}，各自排序。
    """
    old_paths = set(old_map)
    new_paths = set(new_map)
    added = sorted(new_paths - old_paths)
    deleted = sorted(old_paths - new_paths)
    modified = sorted(
        p for p in old_paths & new_paths if old_map[p] != new_map[p]
    )
    return {"added": added, "deleted": deleted, "modified": modified}


def split_lines(data: bytes):
    """字节内容按行切分（保留换行符），用于行级比较。"""
    return data.decode("utf-8", errors="replace").splitlines(keepends=True)


def unified_diff(old_data: bytes, new_data: bytes,
                 old_label: str = "a", new_label: str = "b") -> str:
    """生成两个文本之间的 unified diff（行级）。"""
    old_lines = split_lines(old_data)
    new_lines = split_lines(new_data)
    diff = difflib.unified_diff(
        old_lines, new_lines, fromfile=old_label, tofile=new_label, lineterm=""
    )
    out = []
    for line in diff:
        out.append(line if line.endswith("\n") else line + "\n")
    return "".join(out)


def changed_regions(base, other):
    """返回 base->other 的变更区域列表 [(start, end, replacement_lines), ...]。

    start/end 是 base 的行号区间 [start, end)，insert 时 start == end。
    """
    sm = difflib.SequenceMatcher(a=base, b=other, autojunk=False)
    edits = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        edits.append((i1, i2, other[j1:j2]))
    return edits
