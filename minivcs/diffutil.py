"""文本行级 diff 与三方合并（纯 Python，无外部依赖）。

- :func:`unified_diff`：基于 LCS 的标准 unified diff（带 @@ hunk 头与 3 行上下文）。
- :func:`merge3`：基于两侧各自与 base 的 LCS 对齐实现的 diff3 风格三方合并，
  互不冲突的修改自动合并；同一区域双方都改时产出
  ``<<<<<<<`` / ``=======`` / ``>>>>>>>`` 冲突块，双方内容都保留。
"""

from __future__ import annotations

from typing import List, Sequence, Tuple

CONFLICT_START = b"<<<<<<<\n"
CONFLICT_SEP = b"=======\n"
CONFLICT_END = b">>>>>>>\n"
NO_NEWLINE = b"\\ No newline at end of file\n"


def split_lines(data: bytes) -> List[bytes]:
    """按行切分且保留换行符；文件不以换行结尾时最后一行同样保留原样。"""
    return data.splitlines(keepends=True)


def _lcs_table(a: Sequence[bytes], b: Sequence[bytes]) -> List[List[int]]:
    n, m = len(a), len(b)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        ai = a[i]
        row = dp[i]
        nxt = dp[i + 1]
        for j in range(m - 1, -1, -1):
            if ai == b[j]:
                row[j] = nxt[j + 1] + 1
            else:
                row[j] = nxt[j] if nxt[j] >= nxt[j + 1] else nxt[j + 1]
    return dp


def matching_blocks(a: Sequence[bytes], b: Sequence[bytes]) -> List[Tuple[int, int, int]]:
    """返回 LCS 匹配块 ``[(a起点, b起点, 长度), ...]``，首尾含哨兵 (len(a),len(b),0)。"""
    dp = _lcs_table(a, b)
    blocks: List[Tuple[int, int, int]] = []
    i = j = 0
    n, m = len(a), len(b)
    while i < n and j < m:
        if a[i] == b[j]:
            start_i, start_j = i, j
            while i < n and j < m and a[i] == b[j]:
                i += 1
                j += 1
            blocks.append((start_i, start_j, i - start_i))
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    blocks.append((n, m, 0))
    return blocks


def opcodes(a: Sequence[bytes], b: Sequence[bytes]) -> List[Tuple[str, int, int, int, int]]:
    """与 difflib 风格一致的编辑脚本：(tag, i1, i2, j1, j2)。"""
    blocks = matching_blocks(a, b)
    ops = []
    i = j = 0
    for ai, bj, size in blocks:
        if ai > i and bj > j:
            ops.append(("replace", i, ai, j, bj))
        elif ai > i:
            ops.append(("delete", i, ai, j, j))
        elif bj > j:
            ops.append(("insert", i, i, j, bj))
        if size:
            ops.append(("equal", ai, ai + size, bj, bj + size))
        i, j = ai + size, bj + size
    return ops


# -------------------------------------------------------------- unified diff


def _hunk_range(start: int, count: int) -> str:
    # unified diff 行号从 1 开始；空范围指向起始行的前一行
    if count == 0:
        return f"{start} 0" if start > 0 else "0,0"
    return f"{start + 1}" if count == 1 else f"{start + 1},{count}"


def unified_diff(a: bytes, b: bytes, a_label: str = "a", b_label: str = "b",
                 context: int = 3) -> bytes:
    """生成标准 unified diff 文本；无差异时返回空 bytes。"""
    al = split_lines(a)
    bl = split_lines(b)
    ops = opcodes(al, bl)
    if not any(op[0] != "equal" for op in ops):
        return b""

    # 每个编辑块向两侧扩展 context 行；区间重叠则合并为同一个 hunk
    ranges: List[Tuple[int, int, int, int]] = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            continue
        ri1 = max(0, i1 - context)
        ri2 = min(len(al), i2 + context)
        rj1 = max(0, j1 - context)
        rj2 = min(len(bl), j2 + context)
        if ranges and (ri1 <= ranges[-1][1] or rj1 <= ranges[-1][3]):
            p = ranges[-1]
            ranges[-1] = (p[0], ri2, p[2], rj2)
        else:
            ranges.append((ri1, ri2, rj1, rj2))

    out = [f"--- {a_label}\n".encode(), f"+++ {b_label}\n".encode()]
    for i1, i2, j1, j2 in ranges:
        out.append(
            f"@@ -{_hunk_range(i1, i2 - i1)} +{_hunk_range(j1, j2 - j1)} @@\n".encode()
        )
        # 用 opcodes 在切片区间内精确地逐行输出
        for tag, x1, x2, y1, y2 in ops:
            # 裁剪到 hunk 窗口
            xx1 = max(x1, i1)
            xx2 = min(x2, i2)
            yy1 = max(y1, j1)
            yy2 = min(y2, j2)
            if tag == "equal":
                if xx2 <= xx1:
                    continue
                for line in al[xx1:xx2]:
                    out.append(b" " + line)
                    if not line.endswith(b"\n"):
                        out.append(NO_NEWLINE)
            else:
                if tag in ("delete", "replace"):
                    for line in al[xx1:xx2]:
                        out.append(b"-" + line)
                        if not line.endswith(b"\n"):
                            out.append(NO_NEWLINE)
                if tag in ("insert", "replace"):
                    for line in bl[yy1:yy2]:
                        out.append(b"+" + line)
                        if not line.endswith(b"\n"):
                            out.append(NO_NEWLINE)
    return b"".join(out)



# ---------------------------------------------------------------- 三方合并
#
# 基于坐标对齐的 diff3 合并。
#
# 把 base 看成一组有序坐标：第 k 个“空隙”坐标为 2k（位于第 k 行之前），
# 第 i 个“行”坐标为 2i+1。对 ours / theirs 分别求与 base 的 LCS：
#
#   * base 某行未出现在某侧的 LCS 中  → 该侧“改动”了这个行坐标（删除或替换）；
#   * 某侧在匹配块之间多出的行          → 该侧在对应空隙坐标插入的新内容。
#
# 于是每侧的修改是坐标轴上的一组触点。把连续触点归为一段：
#   * 段内没有“两侧同时触碰”的坐标      → 互不重叠的修改，自动合并；
#   * 段内存在双方共同触碰的坐标        → 同一区域双方都改：若两侧结果相同则
#     采用，否则输出 <<<<<<< / ======= / >>>>>>> 冲突块，双方内容完整保留；
#   * 双方都未触碰的坐标是稳定内容，原样输出。
#
# 这样相邻但作用于不同行的修改会被自动合并，只有真正作用于同一处的
# 修改才产生冲突。


def _side_layout(bl: Sequence[bytes], xl: Sequence[bytes]):
    """求一侧相对 base 的布局。

    返回 ``(matched, inserts)``：
      matched[i] = xl 中与 base 第 i 行匹配的下标，未匹配为 None；
      inserts[g] = xl 在“第 g 行之前”空隙插入的行列表（g 取 0..len(bl)）。
    """
    matched: List = [None] * len(bl)
    inserts: dict = {g: [] for g in range(len(bl) + 1)}
    prev_b = prev_x = 0
    for b0, x0, size in matching_blocks(bl, xl):
        if size == 0:
            continue
        # 匹配块之前：base 的 prev_b..b0-1 被删除/替换，xl 的 prev_x..x0-1
        # 是新内容，归到该匹配块之前的空隙（坐标 gap b0）
        if x0 > prev_x:
            inserts[b0].extend(xl[prev_x:x0])
        for k in range(size):
            matched[b0 + k] = x0 + k
        prev_b, prev_x = b0 + size, x0 + size
    # 尾部纯新增
    if prev_x < len(xl):
        inserts[len(bl)].extend(xl[prev_x:])
    return matched, inserts


def merge3(base: bytes, a: bytes, c: bytes,
           a_label: str = "ours", c_label: str = "theirs") -> Tuple[bytes, bool]:
    """三方文本合并，返回 ``(合并结果, 是否有冲突)``。"""
    bl = split_lines(base)
    al = split_lines(a)
    cl = split_lines(c)
    match_a, ins_a = _side_layout(bl, al)
    match_c, ins_c = _side_layout(bl, cl)
    n = len(bl)

    def contrib(xl, matched, ins, coord) -> List[bytes]:
        """该侧在某坐标贡献的行。"""
        if coord % 2 == 0:
            return list(ins[coord // 2])
        mi = matched[coord // 2]
        return [] if mi is None else [xl[mi]]

    def touched(matched, ins, coord) -> bool:
        if coord % 2 == 0:
            return bool(ins[coord // 2])
        return matched[coord // 2] is None

    def region_text(xl, matched, ins, lo, hi) -> bytes:
        return b"".join(b"".join(contrib(xl, matched, ins, k))
                        for k in range(lo, hi))

    out: List[bytes] = []
    conflict = False
    last = 2 * n  # 末尾空隙坐标
    coord = 0
    while coord <= last:
        ta = touched(match_a, ins_a, coord)
        tc = touched(match_c, ins_c, coord)
        if not ta and not tc:
            # 稳定坐标：只有行坐标才携带原始内容
            if coord % 2 == 1:
                out.append(bl[coord // 2])
            coord += 1
            continue

        # 吞掉整段连续触点，得到一个改动区间 [lo, hi)
        lo = coord
        while coord <= last and (
                touched(match_a, ins_a, coord)
                or touched(match_c, ins_c, coord)):
            coord += 1
        hi = coord

        both_touched = any(
            touched(match_a, ins_a, k) and touched(match_c, ins_c, k)
            for k in range(lo, hi)
        )
        text_a = region_text(al, match_a, ins_a, lo, hi)
        text_c = region_text(cl, match_c, ins_c, lo, hi)

        if both_touched and text_a != text_c:
            # 同一区域双方都改且结果不同 → 冲突，双方内容都保留
            conflict = True
            out.append(f"<<<<<<< {a_label}\n".encode())
            out.append(text_a)
            if not text_a.endswith(b"\n"):
                out.append(b"\n")
            out.append(CONFLICT_SEP)
            out.append(text_c)
            if not text_c.endswith(b"\n"):
                out.append(b"\n")
            out.append(f">>>>>>> {c_label}\n".encode())
        elif both_touched:
            # 双方都改但结果相同
            out.append(text_a)
        else:
            # 两侧作用于不同坐标：逐坐标拼接，把相邻的独立修改组合起来
            merged = []
            for k in range(lo, hi):
                if touched(match_a, ins_a, k):
                    merged.extend(contrib(al, match_a, ins_a, k))
                elif touched(match_c, ins_c, k):
                    merged.extend(contrib(cl, match_c, ins_c, k))
                elif k % 2 == 1:
                    merged.append(bl[k // 2])
            out.extend(merged)

    return b"".join(out), conflict


def looks_binary(data: bytes, sample_size: int = 8000) -> bool:
    """简单的二进制判定：采样区含 NUL 字节即视为二进制。"""
    return b"\x00" in data[:sample_size]
