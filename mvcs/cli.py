"""命令行接口：python3 -m mvcs <command> ..."""

import argparse
import sys

from .repo import (CheckoutConflict, MergeConflict, MvcsError, Repository,
                   find_repo)


def _repo():
    return Repository(find_repo())


def cmd_init(args):
    repo = Repository.init(args.directory)
    print(f"已在 {repo.mvcs_dir} 初始化空仓库")


def cmd_config(args):
    repo = _repo()
    if args.value is None:
        value = repo.get_config(args.key)
        if value is None:
            print(f"未设置: {args.key}")
            return 1
        print(f"{args.key}={value}")
    else:
        repo.set_config(args.key, args.value)
        print(f"{args.key} = {args.value}")


def cmd_add(args):
    repo = _repo()
    added = repo.add(args.paths)
    for p in added:
        print(f"add {p}")


def cmd_commit(args):
    repo = _repo()
    cid = repo.commit(args.message, author=args.author)
    print(f"[{cid[:12]}] {args.message}")


def cmd_status(args):
    repo = _repo()
    st = repo.status()
    kind, value = repo.head_ref()
    print(f"当前分支: {value}" if kind == "branch" else f"游离 HEAD: {value[:12]}")
    staged = st["staged"]
    for p in staged["added"]:
        print(f"  新文件(已暂存):   {p}")
    for p in staged["modified"]:
        print(f"  修改(已暂存):     {p}")
    for p in staged["deleted"]:
        print(f"  删除(已暂存):     {p}")
    for line in st["unstaged"]:
        tag, p = line.split(": ", 1)
        print(f"  {'修改' if tag == 'modified' else '删除'}(未暂存):   {p}")
    for p in st["untracked"]:
        print(f"  未跟踪:           {p}")
    if not any(staged.values()) and not st["unstaged"] and not st["untracked"]:
        print("工作区干净")


def cmd_log(args):
    repo = _repo()
    for c in repo.log():
        parents = " ".join(p[:12] for p in c["parents"])
        print(f"commit {c['id']}")
        if parents:
            print(f"父提交: {parents}")
        print(f"作者:   {c['author']}")
        print(f"日期:   {c['timestamp']}")
        print(f"\n    {c['message']}\n")


def cmd_branch(args):
    repo = _repo()
    if args.name is None:
        kind, current = repo.head_ref()
        for name in repo.list_branches():
            mark = "*" if kind == "branch" and name == current else " "
            print(f"{mark} {name}")
    else:
        repo.create_branch(args.name, args.start)
        print(f"已创建分支 {args.name}")


def cmd_checkout(args):
    repo = _repo()
    cid = repo.checkout(args.target, create_branch=args.create)
    kind, value = repo.head_ref()
    where = value if kind == "branch" else cid[:12]
    print(f"已切换到 {where}")


def cmd_diff(args):
    repo = _repo()
    result = repo.diff_commits(args.rev_a, args.rev_b)
    files = result["files"]
    for p in files["added"]:
        print(f"新增文件: {p}")
    for p in files["deleted"]:
        print(f"删除文件: {p}")
    for p in files["modified"]:
        print(f"修改文件: {p}")
    for path, text in result["lines"].items():
        if text:
            print(text, end="" if text.endswith("\n") else "\n")


def cmd_merge(args):
    repo = _repo()
    cid, _ = repo.merge(args.branch, author=args.author)
    print(f"合并完成: {cid[:12]}")


def cmd_fsck(args):
    repo = _repo()
    problems = repo.fsck()
    if not problems:
        print("fsck: 仓库完整，未发现问题")
        return 0
    for p in problems:
        print(p)
    print(f"fsck: 发现 {len(problems)} 个问题")
    return 1


def build_parser():
    parser = argparse.ArgumentParser(
        prog="mvcs", description="本地轻量级版本控制系统（不依赖 Git）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="初始化仓库")
    p.add_argument("directory", nargs="?", default=".")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("config", help="读取/设置配置，如 user.name")
    p.add_argument("key")
    p.add_argument("value", nargs="?")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("add", help="把文件加入索引")
    p.add_argument("paths", nargs="+")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("commit", help="提交索引内容")
    p.add_argument("-m", "--message", required=True)
    p.add_argument("--author", default=None)
    p.set_defaults(func=cmd_commit)

    p = sub.add_parser("status", help="查看工作区状态")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("log", help="查看提交历史")
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("branch", help="列出或创建分支")
    p.add_argument("name", nargs="?")
    p.add_argument("start", nargs="?")
    p.set_defaults(func=cmd_branch)

    p = sub.add_parser("checkout", help="切换分支或检出提交")
    p.add_argument("target")
    p.add_argument("-b", "--create", action="store_true", help="创建并切换新分支")
    p.set_defaults(func=cmd_checkout)

    p = sub.add_parser("diff", help="两个提交之间的文件级和行级 diff")
    p.add_argument("rev_a")
    p.add_argument("rev_b")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("merge", help="把指定分支合并进当前分支")
    p.add_argument("branch")
    p.add_argument("--author", default=None)
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("fsck", help="仓库完整性检查")
    p.set_defaults(func=cmd_fsck)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except CheckoutConflict as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    except MergeConflict as e:
        print(f"合并冲突，冲突文件已写入工作区（含冲突标记）:", file=sys.stderr)
        for c in e.conflicts:
            print(f"  {c['path']}", file=sys.stderr)
        return 1
    except MvcsError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
