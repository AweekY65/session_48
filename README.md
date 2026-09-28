# minivcs — 本地轻量级版本控制系统

一个**纯 Python 标准库**实现的本地版本控制系统。所有数据（仓库对象、索引、提交、
分支、配置）只保存在工程目录的 `.minivcs/` 隐藏目录中，**不调用 Git、不访问远程
仓库、不使用数据库或任何外部服务**。哈希用标准库 `hashlib`（SHA-256），压缩用
`zlib`，无任何第三方依赖。

## 快速开始

```bash
python3 -m minivcs init                      # 在当前目录初始化（创建 .minivcs/）
python3 -m minivcs config user.name "Alice"  # 设置作者
python3 -m minivcs config user.email "a@x.io"
python3 -m minivcs add .                     # 把工作区内容加入索引（自动暂存删除）
python3 -m minivcs commit -m "first commit"  # 提交
python3 -m minivcs status                    # 查看工作区状态
python3 -m minivcs log                       # 查看提交历史

python3 -m minivcs checkout -b feature       # 创建并切换分支
python3 -m minivcs checkout main             # 切换分支（会覆盖未提交修改时拒绝）
python3 -m minivcs branch                    # 列出分支（* 为当前分支）

python3 -m minivcs diff <提交A> <提交B>       # 文件级 + 文本行级 diff
python3 -m minivcs merge feature             # 三方合并 feature 到当前分支
python3 -m minivcs fsck                      # 完整性检查
```

提交参数可用分支名、完整 64 位提交 ID 或无歧义的短 ID 前缀。

## 对象模型（内容寻址存储）

所有对象用统一的规范化格式表示：

```
<类型> <十进制载荷长度>\n<载荷>
```

对这段规范化字节计算 **SHA-256** 得到 64 位十六进制**对象 ID**，再用 **zlib**
压缩后落盘到：

```
.minivcs/objects/<ID前2位>/<ID后62位>
```

对象 ID 完全由内容决定，因此**相同内容必然得到相同 ID，磁盘上只存一份**；
写入已存在的对象是空操作（天然去重，见测试 `test_same_blob_stored_once`）。
读取时重新解压、校验长度并重算哈希，任何不符都报“损坏对象”。

共三种对象类型：

| 类型   | 载荷 |
|--------|------|
| `blob`   | 文件的原始字节内容 |
| `tree`   | 目录快照。每行 `<模式> <b|t> <名称>\t<对象ID>`，按名称排序；`t` 指向子 tree、`b` 指向 blob，嵌套组成整棵目录树 |
| `commit` | 固定顺序头部：一行 `tree <ID>`、零或多行 `parent <ID>`、`author 名字 <邮箱> 时间戳 +0000`、`committer ...`，空行后是提交消息 |

- 文件模式：普通文件 `100644`、可执行文件 `100755`、目录 `40000`。
- **commit ID 由 tree、父提交、作者、时间戳、消息共同哈希得到**，内容相同 ID
  即稳定一致（测试 `test_stable_commit_id_from_content` 在两个独立仓库中验证）。
- 合并提交记录两个 `parent`。
- 所有 tree（含嵌套子目录）都在提交时完整写入对象库。

## 索引结构

索引文件为 `.minivcs/index`，UTF-8 JSON：

```json
{
  "version": 1,
  "entries": [
    {"path": "dir/file.txt", "mode": "100644", "oid": "<64位blob ID>"}
  ]
}
```

- 条目按 `path` 排序，路径相对仓库根、用 `/` 分隔；只保存 blob 引用，不复制内容。
- `add .` 会暂存新增/修改，**并自动暂存工作区中已删除的被跟踪文件**；
  显式 `add <已删除文件>` 同样暂存删除。
- `.minivcs/` 元数据目录永不被跟踪。
- 所有索引/引用/配置更新都用“写临时文件 + 原子改名”，避免写到一半的文件。

## 引用与配置

```
.minivcs/HEAD               "ref: refs/heads/main"（符号引用）或某 commit ID（游离头）
.minivcs/refs/heads/<分支>  文件内容为该分支指向的 commit ID
.minivcs/config             JSON，如 {"user.name": "Alice", "user.email": "..."}
```

`checkout -b` 创建分支；`checkout <提交ID>` 进入游离头（detached HEAD）。

作者信息可取环境变量 `MVCS_AUTHOR_NAME` / `MVCS_AUTHOR_EMAIL`；
`MVCS_AUTHOR_DATE` 可固定提交时间戳（测试用，保证 commit ID 可复现）。

## checkout 保护规则

切换分支/提交**前**先做安全检查（`switch_check`），凡是会导致未提交修改被
覆盖的路径都会列出并**拒绝切换、工作区与 HEAD 原样不动**：

1. 工作区相对索引被修改的文件，若目标树该文件内容与之不同 → 拒绝；
   若目标内容与索引一致，则本地修改随切换**原样携带**（Git 语义）。
2. 暂存的新文件在目标分支不存在（会丢失暂存内容）→ 拒绝。
3. 未跟踪文件若目标树占用同一路径（会被覆盖）→ 拒绝。

切换时：目标树文件写入工作区；目标树没有、本地也未改动的被跟踪文件会删除；
本地相对索引有改动的文件保留；索引最终同步为目标树。

## diff

`diff <A> <B>` 先做**文件级**比较（新增 / 删除 / 修改、按路径排序），再对每个
变化的文本文件输出基于 **LCS** 的标准 **unified diff**（`---`/`+++` 头、
`@@` hunk 头、3 行上下文、`-`/`+` 行、无换行尾标记）。相同 blob 不重复输出。

## merge 规则（基础三方合并）

以最近共同祖先 **merge base**（两侧祖先交集中距离和最小者）为基准，对
base / ours / theirs 三棵树逐路径合并：

| 情形 | 结果 |
|------|------|
| 两侧都未改 / 改成相同内容 | 采用该内容 |
| 仅相对 base 的一侧变化 | 采用变化侧（新增、修改、删除同理） |
| 当前提交已是对方祖先 | `Already up to date` |
| 对方是当前祖先（无冲突） | **快进（fast-forward）**，同样受 checkout 保护 |
| 一侧删除、另一侧修改同一文件 | 修改/删除冲突 |
| 两侧对同一二进制文件做不同修改 | 二进制冲突 |
| 文本文件双方在**不同坐标**修改 | 行级自动合并 |
| 文本文件双方在**同一坐标**修改且结果不同 | 冲突块，保留双方内容 |

文本行级合并使用坐标对齐的 diff3 算法：把 base 的每个“行”和“行间空隙”统一
编号，分别求 base↔ours、base↔theirs 的 LCS，把每侧修改落到坐标上：

- 连续触点归为一个改动区间；**没有双方共同触碰的坐标**时逐坐标拼接，
  相邻但互不重叠的修改自动合并；
- 区间内**存在双方共同触碰的坐标**且两侧结果不同 → 生成明确冲突：

  ```
  <<<<<<< <当前分支>
  （ours 的该区域内容）
  =======
  （theirs 的该区域内容）
  >>>>>>> <被合并分支>
  ```

  双方原始内容完整保留，非冲突区域照常合并。

无冲突时直接生成带两个父提交的 merge commit 并更新工作区/索引；有冲突时把
含冲突标记的文件写入工作区、以 ours 版本暂存，命令以退出码 **2** 结束，
等待用户解决后提交。

## fsck 完整性检查

`fsck` 检测四类问题（错误输出到 stderr，有错误时退出码非零）：

1. **缺失对象**：commit 的 tree/parent、tree 的子条目、索引条目引用了不存在的对象；
2. **损坏哈希 / 损坏对象**：无法 zlib 解压、头部非法、声明长度不符，
   或重算 SHA-256 与对象 ID 不一致；tree/commit 载荷无法解析也会报损坏；
3. **无效引用**：`refs/heads/*` 或游离 HEAD 指向不存在对象、指向非 commit 对象，
   或 HEAD 内容非法；
4. **不可达对象**：存在于对象库、但从所有分支与 HEAD 出发不可达（作为**警告**列出）。

## 测试命令

全部测试为纯终端自动化测试（`unittest`），**不依赖 Git 本身或任何 GUI**：

```bash
# 在工程根目录执行
python3 -m unittest discover -s tests -v
# 或
python3 -m unittest tests.test_minivcs -v
```

测试覆盖（34 个用例）：

- 提交历史：仓库布局、tree/parent/作者/消息记录、祖先链、稳定对象 ID、作者校验；
- 分支：创建/切换、双向切换文件内容正确、可安全携带的未提交修改不丢失；
- 内容去重：多路径共享同一 blob、对象库物理只存一份、重复写入幂等；
- diff：unified diff 头/hunk/增删行、提交间文件级与行级 diff（含反向）；
- 无冲突合并：merge base 检测、不同区域修改自动合并、双父 merge commit、快进；
- 冲突合并：同区域冲突标记保留双方内容、CLI 退出码 2、修改/删除冲突、二进制冲突；
- fsck：缺失对象/缺失提交、哈希损坏、压缩损坏、无效引用、不可达对象、索引悬空引用；
- checkout 保护：覆盖脏文件被拒且工作区不变、未跟踪文件不被覆盖、
  暂存新文件受保护、干净切换正确更新/删除文件。

## 目录结构

```
minivcs/
  __init__.py
  __main__.py     # python3 -m minivcs 入口
  cli.py          # 命令行（init/add/commit/status/log/branch/checkout/diff/merge/fsck 等）
  objects.py      # 内容寻址对象存储 + blob/tree/commit 序列化
  repo.py         # 仓库布局、索引、引用、配置、add/commit/status/树展开
  checkout.py     # 分支切换与未提交修改保护
  diffutil.py     # LCS、unified diff、diff3 三方文本合并
  merge.py        # 三方树合并、merge base、快进、merge commit
  fsck.py         # 完整性检查
tests/
  test_minivcs.py # 34 个终端自动化测试
```
