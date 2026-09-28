"""minivcs 命令行入口。

用法： python3 -m minivcs <command> [args...]
所有数据仅落在工程目录的 .minivcs/ 隐藏目录中，不调用任何外部服务。
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from . import objects as obj
from .checkout import checkout
from .diffutil import unified_diff
from .fsck import fsck
from .merge import merge, merge_base
from .repo import Repo, RepoError

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFLICT = 2


def _print_status(repo: Repo, out) -> None:
    st = repo.status()
    cur = repo.head_refname()
    if cur is None and repo.head_commit() is not None:
        cur = f"HEAD detached at {repo.head_commit()[:8]}"
    elif cur is None:
        cur = f"{repo.head_refname() or 'main'} (无提交)"
    out.write(f"分支: {cur}\n")
    if not st["staged"] and not st["unstaged"] and not st["untracked"]:
        out.write("工作区干净\n")
        return
    if st["staged"]:
        out.write("\n已暂存（待提交）:\n")
        for tag, path in st["staged"]:
            label = {"A": "新增", "M": "修改", "D": "删除"}[tag]
            out.write(f"  {tag:1}  {label}  {path}\n")
    if st["unstaged"]:
        out.write("\n未暂存的修改:\n")
        for tag, path in st["unstaged"]:
            label = {"A": "新增", "M": "修改", "D": "删除"}[tag]
            out.write(f"  {tag:1}  {label}  {path}\n")
    if st["untracked"]:
        out.write("\n未跟踪文件:\n")
        for path in st["untracked"]:
            out.write(f"  ??  {path}\n")


def _read_blob(repo: Repo, oid: str) -> bytes:
    return repo.store.read(oid, obj.BLOB)


def cmd_diff(repo: Repo, args) -> int:
    a_oid = repo.resolve_target(args.commit_a)
    b_oid = repo.resolve_target(args.commit_b)
    a_tree = repo.flatten_tree(repo.commit_info(a_oid)["tree"])
    b_tree = repo.flatten_tree(repo.commit_info(b_oid)["tree"])
    paths = sorted(set(a_tree) | set(b_tree))
    chunks: List[str] = []
    any_diff = False
    for path in paths:
        ea, eb = a_tree.get(path), b_tree.get(path)
        if ea is not None and eb is not None and ea[2] == eb[2]:
            continue
        any_diff = True
        a_data = _read_blob(repo, ea[2]) if ea else b""
        b_data = _read_blob(repo, eb[2]) if eb else b""
        if ea is None:
            chunks.append(f"新增文件: {path}\n")
        elif eb is None:
            chunks.append(f"删除文件: {path}\n")
        else:
            chunks.append(f"修改文件: {path}\n")
        chunks.append(unified_diff(a_data, b_data,
                                   a_label=f"a/{path}", b_label=f"b/{path}").decode(
                                       "utf-8", errors="replace"))
        chunks.append("\n")
    if any_diff:
        sys.stdout.write("".join(chunks))
    else:
        sys.stdout.write("两个提交内容相同\n")
    return EXIT_OK


def cmd_log(repo: Repo, args) -> int:
    head = repo.head_commit()
    if head is None:
        sys.stdout.write("（当前分支还没有提交）\n")
        return EXIT_OK
    cur = head
    seen = set()
    while cur and cur not in seen:
        seen.add(cur)
        info = repo.commit_info(cur)
        author = info["author"]
        sys.stdout.write(f"commit {cur}\n")
        sys.stdout.write(f"Author: {author['name']} <{author['email']}>\n")
        sys.stdout.write(f"Date:   {author['timestamp']}\n\n")
        for line in info["message"].rstrip("\n").split("\n"):
            sys.stdout.write(f"    {line}\n")
        sys.stdout.write("\n")
        cur = info["parents"][0] if info["parents"] else None
    return EXIT_OK


def cmd_branch(repo: Repo, args) -> int:
    if args.name:
        if repo.branch_exists(args.name):
            raise RepoError(f"分支已存在：{args.name}")
        start = repo.head_commit()
        if start is None:
            raise RepoError("当前没有任何提交，无法创建分支")
        repo.update_branch(args.name, start)
        sys.stdout.write(f"已创建分支 {args.name}（{start[:8]}）\n")
        return EXIT_OK
    cur = repo.head_refname()
    for name in repo.list_branches():
        mark = "* " if name == cur else "  "
        sys.stdout.write(f"{mark}{name}\n")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="minivcs",
                                description="本地轻量级版本控制系统（不依赖 Git）")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("init", help="初始化仓库，创建 .minivcs/")
    sp.add_argument("path", nargs="?", default=".")

    sp = sub.add_parser("config", help="读取/设置配置项")
    sp.add_argument("key")
    sp.add_argument("value", nargs="?", default=None)

    sp = sub.add_parser("add", help="把文件/目录加入索引")
    sp.add_argument("paths", nargs="+")

    sp = sub.add_parser("commit", help="提交索引")
    sp.add_argument("-m", "--message", required=True)
    sp.add_argument("--allow-empty", action="store_true")

    sub.add_parser("status", help="查看工作区状态")
    sub.add_parser("log", help="查看提交历史")

    sp = sub.add_parser("branch", help="列出或创建分支")
    sp.add_argument("name", nargs="?", default=None)

    sp = sub.add_parser("checkout", help="切换分支或提交（-b 创建并切换）")
    sp.add_argument("-b", "--create", action="store_true")
    sp.add_argument("target")

    sp = sub.add_parser("diff", help="两个提交之间的文件级+行级 diff")
    sp.add_argument("commit_a")
    sp.add_argument("commit_b")

    sp = sub.add_parser("merge", help="三方合并指定分支到当前分支")
    sp.add_argument("branch")
    sp.add_argument("-m", "--message", default=None)

    sub.add_parser("fsck", help="完整性检查（缺失/损坏/无效引用/不可达）")

    sp = sub.add_parser("rev-parse", help="(辅助) 解析引用为对象 ID")
    sp.add_argument("target")

    sp = sub.add_parser("cat-file", help="(辅助) 打印对象")
    sp.add_argument("-t", "--type", action="store_true")
    sp.add_argument("oid")

    sp = sub.add_parser("merge-base", help="(辅助) 输出两个提交的合并基")
    sp.add_argument("commit_a")
    sp.add_argument("commit_b")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            repo = Repo.init(args.path)
            sys.stdout.write(f"已在 {repo.vcsdir} 初始化空仓库\n")
            return EXIT_OK

        repo = Repo.find()

        if args.command == "config":
            if args.value is None:
                val = repo.config_get(args.key)
                if val is None:
                    return EXIT_ERROR
                sys.stdout.write(val + "\n")
            else:
                repo.config_set(args.key, args.value)
            return EXIT_OK

        if args.command == "add":
            repo.add(args.paths)
            return EXIT_OK

        if args.command == "commit":
            _cid, is_empty = repo.commit(args.message)
            if is_empty and not args.allow_empty:
                sys.stdout.write("没有变化（空提交需 --allow-empty）\n")
                return EXIT_ERROR
            sys.stdout.write(f"[{repo.head_refname() or 'HEAD'} {_cid[:8]}] "
                             f"{args.message.strip()}\n")
            return EXIT_OK

        if args.command == "status":
            _print_status(repo, sys.stdout)
            return EXIT_OK

        if args.command == "log":
            return cmd_log(repo, args)

        if args.command == "branch":
            return cmd_branch(repo, args)

        if args.command == "checkout":
            mode, target = checkout(repo, args.target, create=args.create)
            if mode == "branch-created":
                sys.stdout.write(f"已创建并切换到分支 {target}\n")
            elif mode == "detached":
                sys.stdout.write(f"HEAD 现在游离于 {target[:8]}\n")
            else:
                sys.stdout.write(f"已切换到分支 {target}\n")
            return EXIT_OK

        if args.command == "diff":
            return cmd_diff(repo, args)

        if args.command == "merge":
            cid, conflicts, fast_forward = merge(repo, args.branch, args.message)
            if conflicts:
                sys.stderr.write("合并冲突：\n")
                for c in conflicts:
                    sys.stderr.write(f"  {c}\n")
                sys.stderr.write("请解决冲突标记后提交\n")
                return EXIT_CONFLICT
            if fast_forward:
                sys.stdout.write(f"快进合并到 {cid[:8]}\n")
            else:
                sys.stdout.write(f"合并完成，产生合并提交 {cid[:8]}\n")
            return EXIT_OK

        if args.command == "fsck":
            errors, warnings = fsck(repo)
            for w in warnings:
                sys.stdout.write(f"警告: {w}\n")
            for e in errors:
                sys.stderr.write(f"错误: {e}\n")
            if errors:
                sys.stderr.write(f"fsck 失败：{len(errors)} 个错误，"
                                 f"{len(warnings)} 个警告\n")
                return EXIT_ERROR
            sys.stdout.write(f"fsck 通过（{len(warnings)} 个不可达对象警告）\n")
            return EXIT_OK

        if args.command == "rev-parse":
            sys.stdout.write(repo.resolve_target(args.target) + "\n")
            return EXIT_OK

        if args.command == "cat-file":
            otype, payload = repo.store.read_raw(args.oid)
            if args.type:
                sys.stdout.write(otype + "\n")
            else:
                sys.stdout.buffer.write(payload)
            return EXIT_OK

        if args.command == "merge-base":
            a = repo.resolve_target(args.commit_a)
            b = repo.resolve_target(args.commit_b)
            base = merge_base(repo, a, b)
            if base:
                sys.stdout.write(base + "\n")
            return EXIT_OK if base else EXIT_ERROR

    except RepoError as exc:
        sys.stderr.write(f"错误: {exc}\n")
        return EXIT_ERROR
    except obj.VCSObjectError as exc:
        sys.stderr.write(f"错误: {exc}\n")
        return EXIT_ERROR

    parser.print_help()
    return EXIT_ERROR
