"""三方合并：文本行级 merge3 + 文件树级三方合并规则。"""

from .diffutil import changed_regions

CONFLICT_START = "<<<<<<< ours\n"
CONFLICT_SEP = "=======\n"
CONFLICT_END = ">>>>>>> theirs\n"


def _apply_edits(base, edits, start, end):
    """把某一侧在 [start, end) 区间内的编辑应用到 base，得到该侧在该区间的文本。"""
    out = []
    pos = start
    for a, b, lines in edits:
        out.extend(base[pos:a])
        out.extend(lines)
        pos = b
    out.extend(base[pos:end])
    return out


def merge_lines(base, ours, theirs):
    """对三份按行切分的文本做三方合并。

    返回 (merged_lines, conflict_count)。
    互不冲突的修改自动合并；同一区域被双方不同修改时生成冲突块，
    冲突块中同时保留双方内容（ours/theirs 标记之间）。
    """
    edits_o = changed_regions(base, ours)
    edits_t = changed_regions(base, theirs)
    out = []
    conflicts = 0
    i = 0
    n = len(base)

    while edits_o or edits_t:
        # 下一个变更起点
        starts = []
        if edits_o:
            starts.append(edits_o[0][0])
        if edits_t:
            starts.append(edits_t[0][0])
        s = min(starts)
        if s > i:
            out.extend(base[i:s])
            i = s
        # 收集相互重叠的变更簇（区间 [i, end)，插入点视为 a<=p<=b 重叠）
        cluster_o, cluster_t = [], []
        end = i
        grew = True
        while grew:
            grew = False
            for edits, cluster in ((edits_o, cluster_o), (edits_t, cluster_t)):
                while edits and edits[0][0] <= end and edits[0][1] >= i:
                    a, b, lines = edits.pop(0)
                    cluster.append((a, b, lines))
                    if b > end:
                        end = b
                    grew = True
        our_part = _apply_edits(base, cluster_o, i, end)
        their_part = _apply_edits(base, cluster_t, i, end)
        if not cluster_t:
            out.extend(our_part)          # 只有 ours 修改
        elif not cluster_o:
            out.extend(their_part)        # 只有 theirs 修改
        elif our_part == their_part:
            out.extend(our_part)          # 双方修改相同
        else:
            conflicts += 1                # 同一区域冲突：保留双方内容
            out.append(CONFLICT_START)
            out.extend(our_part)
            out.append(CONFLICT_SEP)
            out.extend(their_part)
            out.append(CONFLICT_END)
        i = end

    out.extend(base[i:n])
    return out, conflicts


def merge_file_maps(base_map, our_map, their_map, read_blob, write_blob):
    """树级三方合并。

    参数为三个 路径->对象ID 扁平映射；read_blob(oid)->bytes 读取内容，
    write_blob(bytes)->oid 写入合并结果。
    返回 (merged_map, conflicts)，conflicts 为冲突文件信息列表：
    [{"path": p, "content": bytes(带冲突标记)}, ...]
    """
    merged = {}
    conflicts = []
    paths = sorted(set(base_map) | set(our_map) | set(their_map))
    for path in paths:
        b = base_map.get(path)
        o = our_map.get(path)
        t = their_map.get(path)
        if o == t:
            # 双方一致（含双方都删除）
            if o is not None:
                merged[path] = o
        elif b == o:
            # 仅 theirs 修改/新增；theirs 删除则结果删除
            if t is not None:
                merged[path] = t
        elif b == t:
            # 仅 ours 修改/新增
            if o is not None:
                merged[path] = o
        else:
            # 双方都改了且不一致 -> 行级三方合并
            base_lines = _lines(read_blob, b)
            our_lines = _lines(read_blob, o)
            their_lines = _lines(read_blob, t)
            merged_lines, nconf = merge_lines(base_lines, our_lines, their_lines)
            content = "".join(merged_lines).encode("utf-8")
            if nconf:
                conflicts.append({"path": path, "content": content})
            else:
                merged[path] = write_blob(content)
    return merged, conflicts


def _lines(read_blob, oid):
    if oid is None:
        return []
    return read_blob(oid).decode("utf-8", errors="replace").splitlines(keepends=True)
