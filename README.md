# mvcs — 本地轻量级版本控制系统

一个纯 Python 实现的本地版本控制系统。所有数据（仓库对象、索引、提交、分支、
配置）只保存在工程目录的 `.mvcs/` 隐藏目录中，**不调用 Git、不访问远程仓库、
不使用数据库或任何外部服务**。仅使用标准库 `hashlib`（SHA-256）与 `zlib`（压缩）。

## 快速开始

```bash
python3 -m mvcs init                     # 初始化仓库（创建 .mvcs/）
python3 -m mvcs config user.name "Alice" # 设置作者信息
python3 -m mvcs add .                    # 把文件加入索引
python3 -m mvcs commit -m "first commit" # 提交
python3 -m mvcs status                   # 查看工作区状态
python3 -m mvcs log                      # 查看提交历史
python3 -m mvcs checkout -b feature      # 创建并切换分支
python3 -m mvcs checkout main            # 切换分支（有未提交冲突修改时拒绝）
python3 -m mvcs branch                   # 列出分支
python3 -m mvcs diff <commitA> <commitB> # 文件级 + 行级 diff
python3 -m mvcs merge feature            # 三方合并
python3 -m mvcs fsck                     # 完整性检查
```

## 对象模型

对象采用**内容寻址存储**：序列化格式为 `<type> <size>\0<payload>`，对整体计算
SHA-256 得到对象 ID，zlib 压缩后写入 `.mvcs/objects/<id前2位>/<id后62位>`。
相同内容的对象 ID 相同，因此**相同内容绝不重复存储**。

三种对象类型：

| 类型   | 内容 |
|--------|------|
| blob   | 文件原始字节内容 |
| tree   | 目录快照，每行 `<blob|tree> <对象ID> <名称>`，按名称排序，嵌套组成目录树 |
| commit | `tree <ID>`、零或多个 `parent <ID>`、`author 名字 <邮箱> 时间戳`、空行后是提交消息 |

commit ID 由 tree、父提交、作者、时间戳和消息共同哈希决定，内容相同则 ID 稳定一致。

## 索引结构

索引是 `.mvcs/index.json`，JSON 对象，键为仓库相对路径，值为：

```json
{
  "src/main.py": {"hash": "<blob对象ID>", "size": 123, "mtime_ns": 1690000000000000000}
}
```

`add` 时计算文件 blob 哈希写入对象库并更新索引；`status` 通过比较
HEAD 树 ↔ 索引（已暂存变更）和索引 ↔ 工作区（未暂存变更、未跟踪文件）得出状态。

引用与配置：

- `.mvcs/refs/heads/<分支名>`：内容为 commit ID
- `.mvcs/HEAD`：`ref: refs/heads/<分支>` 或游离态的 commit ID
- `.mvcs/config.json`：`user.name`、`user.email` 等配置

## checkout 保护

切换分支/检出提交前，对每个两版本间内容有差异的路径检查本地是否有未提交修改
（工作区相对 HEAD 被修改或删除、有已暂存修改、或目标中存在同名的未跟踪文件）。
存在则**中止切换并报错**，工作区与 HEAD 保持不变；两分支内容一致的文件上的
本地修改不受影响，切换后保留。

## merge 规则

`merge <branch>` 执行三方合并：

1. 通过父提交链 BFS 找到两个分支的**最近公共祖先**（merge base）。
2. base 等于对方 → 已是最新；base 等于己方 → **fast-forward**。
3. 否则逐文件比较 base / ours / theirs 三个版本：
   - 只有一方修改 → 采用该方；双方修改相同 → 采用任一方；
   - 双方都不同修改 → 做**行级三方合并**（diff3 风格）：把 base→ours 与
     base→theirs 的变更区域对齐，互不重叠的修改自动合并；
   - 同一区域被双方不同修改 → 生成**冲突块**，同时保留双方内容：

     ```
     <<<<<<< ours
     本分支的内容
     =======
     对方分支的内容
     >>>>>>> theirs
     ```

4. 无冲突时自动创建带**两个父提交**的合并提交；有冲突时不提交，冲突文件
   （含标记）写入工作区，并记录 `.mvcs/MERGE_HEAD`。

## diff

`diff <A> <B>` 先给出文件级差异（新增 / 删除 / 修改），再对每个变化文件输出
基于 `difflib` 的 unified 行级 diff。

## fsck

`fsck` 检查四类问题，全部通过时退出码为 0，否则列出问题并返回非零：

- **缺失对象**：被提交/树/引用指向但对象库中不存在
- **损坏哈希**：对象无法解压，或文件内容的 SHA-256 与其对象 ID 不一致
- **无效引用**：分支指向不存在或非 commit 的对象
- **不可达对象**：从任何分支引用出发都无法到达的对象

## 运行测试

全部测试在终端执行，不依赖 Git 或 GUI：

```bash
python3 -m unittest discover -s tests -v
```

覆盖场景：提交历史与父提交链、commit ID 稳定性、内容去重、status、
分支创建/切换与文件恢复、checkout 未提交修改保护（工作区修改 / 已暂存 /
未跟踪文件）、文件级与行级 diff、fast-forward 与无冲突自动合并、
冲突合并（保留双方内容）、fsck（缺失对象 / 损坏哈希 / 无效引用 /
不可达对象），以及通过子进程端到端执行的 CLI 全流程。

## 目录结构

```
mvcs/
  objects.py   # 内容寻址对象存储（SHA-256 + zlib）
  repo.py      # 仓库核心：init/add/commit/status/checkout/branch/merge/fsck
  diffutil.py  # 文件级与行级 diff
  merge.py     # 行级 merge3 与树级三方合并
  cli.py       # 命令行入口（python3 -m mvcs）
tests/
  test_mvcs.py # 自动化测试（unittest）
```
