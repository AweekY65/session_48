"""mvcs 自动化测试：全部在终端通过 `python3 -m unittest` 运行，不依赖 Git。"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mvcs.merge import merge_lines
from mvcs.repo import (CheckoutConflict, MergeConflict, MvcsError, Repository)


class RepoTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="mvcs-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.repo = Repository.init(self.dir)

    # ---------- 辅助 ----------

    def write(self, path, content):
        full = os.path.join(self.dir, path)
        os.makedirs(os.path.dirname(full) or self.dir, exist_ok=True)
        with open(full, "w") as f:
            f.write(content)

    def read(self, path):
        with open(os.path.join(self.dir, path)) as f:
            return f.read()

    def exists(self, path):
        return os.path.isfile(os.path.join(self.dir, path))

    def commit_all(self, message, ts):
        self.repo.add(["."])
        return self.repo.commit(message, author="Tester <t@example.com>",
                                timestamp=ts)

    def object_count(self):
        return len(self.repo.store.list_all())


class TestInitAndCommit(RepoTestCase):
    def test_init_layout(self):
        self.assertTrue(os.path.isdir(os.path.join(self.dir, ".mvcs", "objects")))
        self.assertTrue(os.path.isdir(
            os.path.join(self.dir, ".mvcs", "refs", "heads")))
        with open(os.path.join(self.dir, ".mvcs", "HEAD")) as f:
            self.assertIn("refs/heads/main", f.read())

    def test_commit_history_and_parents(self):
        self.write("a.txt", "one\n")
        c1 = self.commit_all("first", 1000)
        self.write("a.txt", "one\ntwo\n")
        c2 = self.commit_all("second", 2000)
        self.write("b.txt", "b\n")
        c3 = self.commit_all("third", 3000)

        self.assertEqual(self.repo.head_commit(), c3)
        h = self.repo.log()
        self.assertEqual([c["id"] for c in h], [c3, c2, c1])
        self.assertEqual(self.repo.read_commit(c3)["parents"], [c2])
        self.assertEqual(self.repo.read_commit(c2)["parents"], [c1])
        self.assertEqual(self.repo.read_commit(c1)["parents"], [])
        self.assertEqual(self.repo.read_commit(c2)["author"],
                         "Tester <t@example.com>")
        self.assertEqual(self.repo.read_commit(c2)["message"], "second")
        # 树状态：c3 应包含两个文件
        tree = self.repo.commit_map(c3)
        self.assertEqual(sorted(tree), ["a.txt", "b.txt"])

    def test_commit_id_is_stable(self):
        """相同 tree/父提交/作者/时间戳/消息 -> 相同 commit ID。"""
        self.write("a.txt", "x\n")
        self.repo.add(["."])
        c1 = self.repo.commit("msg", author="A <a@b.c>", timestamp=42)
        # 内容完全相同时重新计算，ID 必须一致
        index = self.repo.read_index()
        tree_id = self.repo.write_tree({p: e["hash"] for p, e in index.items()})
        c2 = self.repo._write_commit(tree_id, [], "A <a@b.c>", 42, "msg")
        self.assertEqual(c1, c2)

    def test_content_dedup(self):
        """相同内容只存储一次。"""
        self.write("a.txt", "same content\n")
        self.write("dir/b.txt", "same content\n")
        self.repo.add(["."])
        blobs_after_first = self.object_count()
        # 两个文件内容相同 -> 只有 1 个 blob 对象
        index = self.repo.read_index()
        self.assertEqual(index["a.txt"]["hash"], index["dir/b.txt"]["hash"])
        # 再添加一个相同内容的新文件，对象总数不变
        self.write("c.txt", "same content\n")
        self.repo.add(["c.txt"])
        self.assertEqual(self.object_count(), blobs_after_first)
        # 未修改的文件重复 add 不产生新对象
        self.repo.add(["."])
        self.assertEqual(self.object_count(), blobs_after_first)

    def test_status(self):
        self.write("tracked.txt", "v1\n")
        self.commit_all("init", 100)
        self.write("tracked.txt", "v2\n")       # 未暂存修改
        self.write("new.txt", "new\n")          # 未跟踪
        st = self.repo.status()
        self.assertEqual(st["unstaged"], ["modified: tracked.txt"])
        self.assertEqual(st["untracked"], ["new.txt"])
        self.repo.add(["tracked.txt"])
        st = self.repo.status()
        self.assertEqual(st["staged"]["modified"], ["tracked.txt"])
        self.assertEqual(st["unstaged"], [])


class TestBranchAndCheckout(RepoTestCase):
    def _seed(self):
        self.write("f.txt", "base\n")
        return self.commit_all("base", 100)

    def test_branch_switch_restores_files(self):
        base = self._seed()
        self.repo.checkout("feature", create_branch=True)
        self.write("f.txt", "feature\n")
        self.write("only-feature.txt", "x\n")
        feat = self.commit_all("feature work", 200)

        self.repo.checkout("main")
        self.assertEqual(self.read("f.txt"), "base\n")
        self.assertFalse(self.exists("only-feature.txt"))
        self.assertEqual(self.repo.head_commit(), base)

        self.repo.checkout("feature")
        self.assertEqual(self.read("f.txt"), "feature\n")
        self.assertTrue(self.exists("only-feature.txt"))
        self.assertEqual(self.repo.head_commit(), feat)
        self.assertEqual(self.repo.head_ref(), ("branch", "feature"))

    def test_checkout_detached_commit(self):
        c1 = self._seed()
        self.write("f.txt", "v2\n")
        self.commit_all("v2", 200)
        self.repo.checkout(c1)
        self.assertEqual(self.read("f.txt"), "base\n")
        self.assertEqual(self.repo.head_ref(), ("detached", c1))

    def test_checkout_protects_uncommitted_changes(self):
        self._seed()
        self.repo.checkout("other", create_branch=True)
        self.write("f.txt", "other\n")
        self.commit_all("other", 200)
        self.repo.checkout("main")
        # 工作区未提交修改，目标分支该文件内容不同 -> 必须拒绝
        self.write("f.txt", "local edit\n")
        with self.assertRaises(CheckoutConflict):
            self.repo.checkout("other")
        # 工作区内容保持原样，HEAD 不变
        self.assertEqual(self.read("f.txt"), "local edit\n")
        self.assertEqual(self.repo.head_ref(), ("branch", "main"))

    def test_checkout_protects_staged_changes(self):
        self._seed()
        self.repo.checkout("other", create_branch=True)
        self.write("f.txt", "other\n")
        self.commit_all("other", 200)
        self.repo.checkout("main")
        self.write("f.txt", "staged edit\n")
        self.repo.add(["f.txt"])  # 已暂存未提交
        with self.assertRaises(CheckoutConflict):
            self.repo.checkout("other")
        self.assertEqual(self.read("f.txt"), "staged edit\n")

    def test_checkout_protects_untracked_file(self):
        self._seed()
        self.repo.checkout("other", create_branch=True)
        self.write("new.txt", "from branch\n")
        self.commit_all("add new", 200)
        self.repo.checkout("main")
        self.write("new.txt", "untracked local\n")  # 未跟踪同名文件
        with self.assertRaises(CheckoutConflict):
            self.repo.checkout("other")
        self.assertEqual(self.read("new.txt"), "untracked local\n")

    def test_checkout_keeps_dirty_file_when_target_identical(self):
        self.write("f.txt", "base\n")
        self.write("same.txt", "same\n")
        self.commit_all("base", 100)
        self.repo.checkout("other", create_branch=True)
        self.write("f.txt", "other\n")
        self.commit_all("other", 200)
        self.repo.checkout("main")
        # same.txt 在两个分支内容一致，本地修改它不阻止切换且修改被保留
        self.write("same.txt", "locally edited\n")
        self.repo.checkout("other")
        self.assertEqual(self.read("same.txt"), "locally edited\n")
        self.assertEqual(self.read("f.txt"), "other\n")


class TestDiff(RepoTestCase):
    def test_file_and_line_diff(self):
        self.write("mod.txt", "line1\nline2\nline3\n")
        self.write("del.txt", "gone\n")
        c1 = self.commit_all("v1", 100)
        self.write("mod.txt", "line1\nline2 changed\nline3\n")
        self.write("add.txt", "brand new\n")
        os.remove(os.path.join(self.dir, "del.txt"))
        self.repo.add(["."])
        # 删除的文件需要手动从索引移除（add 只处理存在的文件）
        index = self.repo.read_index()
        index.pop("del.txt")
        self.repo.write_index(index)
        c2 = self.repo.commit("v2", author="T <t@t>", timestamp=200)

        result = self.repo.diff_commits(c1, c2)
        files = result["files"]
        self.assertEqual(files["added"], ["add.txt"])
        self.assertEqual(files["deleted"], ["del.txt"])
        self.assertEqual(files["modified"], ["mod.txt"])
        text = result["lines"]["mod.txt"]
        self.assertIn("-line2\n", text)
        self.assertIn("+line2 changed\n", text)
        self.assertIn("--- a/mod.txt", text)
        self.assertIn("+++ b/mod.txt", text)


class TestMerge(RepoTestCase):
    def _diverge(self):
        """构造 base -> main/feature 分叉，返回 base commit。"""
        self.write("shared.txt", "a\nb\nc\nd\ne\n")
        self.write("main-only.txt", "m\n")
        base = self.commit_all("base", 100)
        self.repo.checkout("feature", create_branch=True)
        self.write("shared.txt", "a\nb\nc\nd\nE-feature\n")
        self.write("feature.txt", "f\n")
        self.commit_all("feature change", 200)
        self.repo.checkout("main")
        self.write("shared.txt", "A-main\nb\nc\nd\ne\n")
        self.commit_all("main change", 300)
        return base

    def test_merge_no_conflict(self):
        self._diverge()
        cid, conflicts = self.repo.merge("feature", author="T <t@t>")
        self.assertEqual(conflicts, [])
        # 双方不同区域的修改都被保留
        self.assertEqual(self.read("shared.txt"), "A-main\nb\nc\nd\nE-feature\n")
        self.assertTrue(self.exists("feature.txt"))
        # 合并提交有两个父提交
        commit = self.repo.read_commit(cid)
        self.assertEqual(len(commit["parents"]), 2)
        self.assertEqual(self.repo.head_commit(), cid)

    def test_merge_fast_forward(self):
        self.write("f.txt", "v1\n")
        self.commit_all("base", 100)
        self.repo.checkout("feature", create_branch=True)
        self.write("f.txt", "v2\n")
        c2 = self.commit_all("ahead", 200)
        self.repo.checkout("main")
        cid, _ = self.repo.merge("feature")
        self.assertEqual(cid, c2)
        self.assertEqual(self.read("f.txt"), "v2\n")

    def test_merge_conflict_keeps_both_sides(self):
        self.write("c.txt", "start\nmiddle\nend\n")
        self.commit_all("base", 100)
        self.repo.checkout("feature", create_branch=True)
        self.write("c.txt", "start\nfeature-version\nend\n")
        self.commit_all("feature edit", 200)
        self.repo.checkout("main")
        self.write("c.txt", "start\nmain-version\nend\n")
        main_head = self.commit_all("main edit", 300)

        with self.assertRaises(MergeConflict) as ctx:
            self.repo.merge("feature")
        self.assertEqual([c["path"] for c in ctx.exception.conflicts], ["c.txt"])
        # 不产生合并提交
        self.assertEqual(self.repo.head_commit(), main_head)
        # 冲突文件包含标记与双方内容
        content = self.read("c.txt")
        self.assertIn("<<<<<<< ours", content)
        self.assertIn("=======", content)
        self.assertIn(">>>>>>> theirs", content)
        self.assertIn("main-version", content)
        self.assertIn("feature-version", content)
        self.assertIn("start", content)
        self.assertIn("end", content)

    def test_merge_lines_unit(self):
        base = ["1\n", "2\n", "3\n", "4\n", "5\n"]
        ours = ["1\n", "two\n", "3\n", "4\n", "5\n"]
        theirs = ["1\n", "2\n", "3\n", "4\n", "five\n"]
        merged, nconf = merge_lines(base, ours, theirs)
        self.assertEqual(nconf, 0)
        self.assertEqual(merged, ["1\n", "two\n", "3\n", "4\n", "five\n"])
        # 同一行冲突
        ours2 = ["1\n", "A\n", "3\n", "4\n", "5\n"]
        theirs2 = ["1\n", "B\n", "3\n", "4\n", "5\n"]
        merged2, nconf2 = merge_lines(base, ours2, theirs2)
        self.assertEqual(nconf2, 1)
        text = "".join(merged2)
        self.assertIn("A\n", text)
        self.assertIn("B\n", text)


class TestFsck(RepoTestCase):
    def _seed(self):
        self.write("a.txt", "hello\n")
        self.write("sub/b.txt", "world\n")
        return self.commit_all("seed", 100)

    def test_healthy_repo(self):
        self._seed()
        self.assertEqual(self.repo.fsck(), [])

    def test_missing_object(self):
        self._seed()
        oid = self.repo.read_index()["a.txt"]["hash"]
        os.remove(self.repo.store._path(oid))
        problems = self.repo.fsck()
        self.assertTrue(any("missing" in p and oid in p for p in problems),
                        problems)

    def test_corrupt_object_hash(self):
        self._seed()
        oid = self.repo.read_index()["a.txt"]["hash"]
        path = self.repo.store._path(oid)
        with open(path, "wb") as f:
            f.write(b"garbage-not-zlib")
        problems = self.repo.fsck()
        self.assertTrue(any("corrupt" in p and oid in p for p in problems),
                        problems)

    def test_invalid_ref(self):
        self._seed()
        self.repo.write_ref("broken", "0" * 64)
        problems = self.repo.fsck()
        self.assertTrue(any("invalid-ref" in p and "broken" in p
                            for p in problems), problems)

    def test_unreachable_object(self):
        self._seed()
        orphan = self.repo.write_blob(b"never referenced\n")
        problems = self.repo.fsck()
        self.assertTrue(any("unreachable" in p and orphan in p
                            for p in problems), problems)


class TestCli(unittest.TestCase):
    """通过子进程在终端执行 CLI，验证端到端流程。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="mvcs-cli-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def run_mvcs(self, *args, expect=0):
        env = dict(os.environ)
        env["PYTHONPATH"] = self.root + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-m", "mvcs", *args],
            cwd=self.dir, env=env, capture_output=True, text=True)
        if expect is not None:
            self.assertEqual(proc.returncode, expect,
                             f"stdout={proc.stdout}\nstderr={proc.stderr}")
        return proc

    def test_full_workflow(self):
        self.run_mvcs("init")
        self.run_mvcs("config", "user.name", "CLI Tester")
        with open(os.path.join(self.dir, "x.txt"), "w") as f:
            f.write("v1\n")
        self.run_mvcs("add", "x.txt")
        self.run_mvcs("commit", "-m", "first")
        out = self.run_mvcs("status").stdout
        self.assertIn("工作区干净", out)
        self.run_mvcs("checkout", "-b", "dev")
        with open(os.path.join(self.dir, "x.txt"), "w") as f:
            f.write("v2\n")
        self.run_mvcs("add", ".")
        self.run_mvcs("commit", "-m", "second")
        log = self.run_mvcs("log").stdout
        self.assertIn("first", log)
        self.assertIn("second", log)
        self.run_mvcs("checkout", "main")
        with open(os.path.join(self.dir, "x.txt")) as f:
            self.assertEqual(f.read(), "v1\n")
        self.run_mvcs("fsck")
        # checkout 保护：未提交修改时切换被拒绝
        with open(os.path.join(self.dir, "x.txt"), "w") as f:
            f.write("dirty\n")
        proc = self.run_mvcs("checkout", "dev", expect=1)
        self.assertIn("未提交", proc.stderr)
        with open(os.path.join(self.dir, "x.txt")) as f:
            self.assertEqual(f.read(), "dirty\n")

    def test_cli_diff_and_merge_conflict(self):
        self.run_mvcs("init")
        p = os.path.join(self.dir, "f.txt")
        with open(p, "w") as f:
            f.write("base\n")
        self.run_mvcs("add", ".")
        self.run_mvcs("commit", "-m", "base")
        self.run_mvcs("checkout", "-b", "side")
        with open(p, "w") as f:
            f.write("side\n")
        self.run_mvcs("add", ".")
        self.run_mvcs("commit", "-m", "side")
        self.run_mvcs("checkout", "main")
        with open(p, "w") as f:
            f.write("main\n")
        self.run_mvcs("add", ".")
        self.run_mvcs("commit", "-m", "main")
        proc = self.run_mvcs("merge", "side", expect=1)
        self.assertIn("冲突", proc.stderr)
        with open(p) as f:
            content = f.read()
        self.assertIn("main", content)
        self.assertIn("side", content)


if __name__ == "__main__":
    unittest.main()
