"""minivcs 自动化测试。

覆盖场景（均不依赖 Git 本身或 GUI，纯终端可执行）：

1. 提交历史（init/add/commit/log、父子关系、稳定对象 ID）
2. 分支创建与切换
3. 内容去重（相同 blob 只存一份）
4. 文件级与文本行级 diff
5. 无冲突三方合并（含快进）
6. 冲突合并（冲突标记保留双方内容）
7. fsck：缺失对象、损坏哈希、无效引用、不可达对象
8. checkout 对未提交修改的保护
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from minivcs import objects as obj
from minivcs.checkout import checkout, switch_check
from minivcs.diffutil import merge3, opcodes, unified_diff
from minivcs.fsck import fsck
from minivcs.merge import merge, merge_base, merge_trees
from minivcs.repo import DIR_NAME, Repo, RepoError

# 让提交时间与作者固定 → commit ID 完全确定
os.environ["MVCS_AUTHOR_NAME"] = "Alice"
os.environ["MVCS_AUTHOR_EMAIL"] = "alice@example.com"
os.environ["MVCS_AUTHOR_DATE"] = "1700000000"


class WorkdirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="minivcs-test-")
        self.repo = Repo.init(self.tmp)
        self._clock = [1700000000]
        self._old_cwd = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self._old_cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------- 辅助方法

    def write(self, path, content, mode=None):
        full = os.path.join(self.tmp, path)
        os.makedirs(os.path.dirname(full), exist_ok=True) if os.path.dirname(path) else None
        if isinstance(content, str):
            content = content.encode()
        with open(full, "wb") as f:
            f.write(content)
        if mode is not None:
            os.chmod(full, mode)

    def read(self, path):
        with open(os.path.join(self.tmp, path), "rb") as f:
            return f.read()

    def add_commit(self, message, ts=None):
        self.repo.add(["."])
        if ts is not None:
            os.environ["MVCS_AUTHOR_DATE"] = str(ts)
        cid, _empty = self.repo.commit(message)
        return cid

    def run_cli(self, *argv):
        from minivcs.cli import main
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--help"] if False else list(argv))
        return code, out.getvalue(), err.getvalue()

    def modify_worktree_file(self, path, content):
        self.write(path, content)


# ----------------------------------------------------------- 1. 提交历史


class TestCommitHistory(WorkdirTest):
    def test_init_layout(self):
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, DIR_NAME, "objects")))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, DIR_NAME, "HEAD")))
        with open(os.path.join(self.tmp, DIR_NAME, "HEAD")) as f:
            self.assertIn("refs/heads/main", f.read())

    def test_commit_records_tree_parent_author_message(self):
        self.write("a.txt", "hello\n")
        self.write("dir/b.txt", "world\n")
        cid = self.add_commit("first", ts=1700000000)
        info = self.repo.commit_info(cid)
        self.assertEqual(info["parents"], [])
        self.assertEqual(info["author"]["name"], "Alice")
        self.assertEqual(info["author"]["email"], "alice@example.com")
        self.assertEqual(info["author"]["timestamp"], 1700000000)
        self.assertEqual(info["message"], "first\n")

        # 树快照包含两个文件，且嵌套目录正确
        flat = self.repo.flatten_tree(info["tree"])
        self.assertEqual(set(flat), {"a.txt", "dir/b.txt"})
        self.assertEqual(self.repo.store.read(flat["a.txt"][2], obj.BLOB), b"hello\n")

        # 第二次提交记录父提交
        self.write("a.txt", "hello v2\n")
        cid2 = self.add_commit("second", ts=1700000100)
        info2 = self.repo.commit_info(cid2)
        self.assertEqual(info2["parents"], [cid])
        self.assertIn("second", info2["message"])

        # 祖先链
        anc = self.repo.ancestors(cid2)
        self.assertEqual(set(anc), {cid, cid2})

    def test_stable_commit_id_from_content(self):
        # 同样的 tree/parent/作者/时间/消息 ⇒ 同样的 commit ID
        self.write("f.txt", "data\n")
        c1 = self.add_commit("msg", ts=1700000000)

        other = tempfile.mkdtemp(prefix="minivcs-stable-")
        try:
            r2 = Repo.init(other)
            with open(os.path.join(other, "f.txt"), "wb") as f:
                f.write(b"data\n")
            r2.add(["."])
            c2, _ = r2.commit("msg")
            self.assertEqual(c1, c2)
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_log_via_cli(self):
        self.write("a.txt", "1\n")
        self.add_commit("c1", ts=1700000000)
        self.write("a.txt", "2\n")
        self.add_commit("c2", ts=1700000100)
        code, out, _ = self.run_cli("log")
        self.assertEqual(code, 0)
        self.assertIn("c2", out)
        self.assertIn("c1", out)

    def test_requires_author_identity(self):
        del os.environ["MVCS_AUTHOR_NAME"]
        try:
            self.write("a.txt", "x\n")
            self.repo.add(["."])
            with self.assertRaises(RepoError):
                self.repo.commit("m")
        finally:
            os.environ["MVCS_AUTHOR_NAME"] = "Alice"


# ----------------------------------------------------------- 2. 分支切换


class TestBranches(WorkdirTest):
    def _base_commit(self):
        self.write("base.txt", "base\n")
        return self.add_commit("base", ts=1700000000)

    def test_create_and_switch_branch(self):
        base = self._base_commit()
        code, out, _ = self.run_cli("checkout", "-b", "feature")
        self.assertEqual(code, 0)
        self.assertEqual(self.repo.head_refname(), "feature")
        self.assertEqual(self.repo.resolve_branch("feature"), base)

        self.write("feature.txt", "feat\n")
        fc = self.add_commit("feature work", ts=1700000100)

        # main 不受影响
        code, _, _ = self.run_cli("checkout", "main")
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "feature.txt")))
        self.assertEqual(self.repo.head_commit(), base)

        # 切回 feature，文件回来
        code, _, _ = self.run_cli("checkout", "feature")
        self.assertEqual(code, 0)
        self.assertEqual(self.repo.head_commit(), fc)
        self.assertEqual(self.read("feature.txt"), b"feat\n")

        code, out, _ = self.run_cli("branch")
        self.assertIn("feature", out)
        self.assertIn("main", out)

    def test_switch_preserves_uncommitted_when_possible(self):
        # 目标分支与该文件一致时，未提交修改可以原样保留（不丢失）
        self._base_commit()
        self.run_cli("checkout", "-b", "other")
        self.write("only-other.txt", "x\n")
        self.add_commit("other", ts=1700000100)
        self.run_cli("checkout", "main")
        # main 上 base.txt 存在；对它做未提交修改
        self.write("base.txt", "uncommitted change\n")
        code, _, err = self.run_cli("checkout", "other")
        # other 分支 base.txt 内容与 main 相同（=base），工作区改动可保留
        self.assertEqual(code, 0)
        self.assertEqual(self.read("base.txt"), b"uncommitted change\n")
        self.assertEqual(err, "")


# ----------------------------------------------------------- 3. 内容去重


class TestDeduplication(WorkdirTest):
    def test_same_blob_stored_once(self):
        self.write("a.txt", "identical content\n")
        self.write("b.txt", "identical content\n")
        self.repo.add(["."])
        idx = self.repo.read_index()
        ids = {e["oid"] for e in idx}
        self.assertEqual(len(ids), 1)  # 两个路径同一 blob ID

        # 对象库中物理对象也只有一个 blob（另加 tree）
        all_objects = list(self.repo.store.iter_ids())
        blob_count = 0
        for oid in all_objects:
            t, _ = self.repo.store.read_raw(oid)
            if t == obj.BLOB:
                blob_count += 1
        self.assertEqual(blob_count, 1)

        cid = self.add_commit("c", ts=1700000000)
        # 提交后修改一个文件为另一个文件的同样内容，仍不新增 blob
        self.write("a.txt", "identical content\n")  # 无变化
        self.repo.add(["."])
        self.write("c.txt", "identical content\n")
        self.repo.add(["."])
        blob_ids = [o for o in self.repo.store.iter_ids()
                    if self.repo.store.read_raw(o)[0] == obj.BLOB]
        self.assertEqual(len(blob_ids), 1)
        self.assertTrue(cid)

    def test_put_existing_object_is_idempotent(self):
        data = os.urandom(256)
        o1 = self.repo.store.put_blob(data)
        path = self.repo.store._path(o1)
        mtime_before = os.stat(path).st_mtime_ns
        o2 = self.repo.store.put_blob(data)
        self.assertEqual(o1, o2)
        self.assertEqual(os.stat(path).st_mtime_ns, mtime_before)


# ----------------------------------------------------------- 4. diff


class TestDiff(WorkdirTest):
    def test_unified_diff_unit(self):
        a = b"l1\nl2\nl3\nl4\n"
        b = b"l1\nl2 changed\nl3\nl4\nl5\n"
        d = unified_diff(a, b, "a/f", "b/f")
        text = d.decode()
        self.assertIn("--- a/f", text)
        self.assertIn("+++ b/f", text)
        self.assertIn("@@", text)
        self.assertIn("-l2", text)
        self.assertIn("+l2 changed", text)
        self.assertIn("+l5", text)

        self.assertEqual(unified_diff(a, a), b"")

    def test_opcodes_basic(self):
        ops = opcodes([b"a", b"b"], [b"a", b"c"])
        tags = [o[0] for o in ops]
        self.assertIn("replace", tags)

    def test_commit_level_diff(self):
        self.write("f.txt", "line1\nline2\nline3\n")
        c1 = self.add_commit("c1", ts=1700000000)
        self.write("f.txt", "line1\nline2!\nline3\n")
        self.write("new.txt", "brand new\n")
        c2 = self.add_commit("c2", ts=1700000100)

        code, out, _ = self.run_cli("diff", c1, c2)
        self.assertEqual(code, 0)
        self.assertIn("修改文件: f.txt", out)
        self.assertIn("-line2", out)
        self.assertIn("+line2!", out)
        self.assertIn("新增文件: new.txt", out)

        # 反向 diff 包含删除
        code, out, _ = self.run_cli("diff", c2, c1)
        self.assertIn("删除文件: new.txt", out)
        self.assertIn("-line2!", out)


# ----------------------------------------------------------- 5. 无冲突合并


class TestCleanMerge(WorkdirTest):
    def _diverged_repo(self):
        # base: 三个文件
        self.write("a.txt", "A0\n")
        self.write("b.txt", "B0\n")
        self.write("c.txt", "C0\n")
        base = self.add_commit("base", ts=1700000000)
        # feature 从 base 分叉：改 a.txt
        self.run_cli("checkout", "-b", "feature")
        self.write("a.txt", "A1\n")
        feat = self.add_commit("feature", ts=1700000100)
        # main 从 base 分叉：改 b.txt
        self.run_cli("checkout", "main")
        self.write("b.txt", "B1\n")
        main_c = self.add_commit("main work", ts=1700000200)
        return base, feat, main_c

    def test_merge_base_detection(self):
        base, feat, main_c = self._diverged_repo()
        mb = merge_base(self.repo, feat, main_c)
        self.assertEqual(mb, base)

    def test_non_conflicting_merge_commit(self):
        _base, feat, main_c = self._diverged_repo()
        cid, conflicts, ff = merge(self.repo, "feature")
        self.assertEqual(conflicts, [])
        self.assertFalse(ff)
        info = self.repo.commit_info(cid)
        self.assertEqual(set(info["parents"]), {main_c, feat})

        # 双方修改都保留，未改文件不动
        self.assertEqual(self.read("a.txt"), b"A1\n")
        self.assertEqual(self.read("b.txt"), b"B1\n")
        self.assertEqual(self.read("c.txt"), b"C0\n")

        # 合并树再次 fsck 无错误
        errors, _warnings = fsck(self.repo)
        self.assertEqual(errors, [])

    def test_fast_forward_merge(self):
        self.write("a.txt", "x\n")
        base = self.add_commit("base", ts=1700000000)
        self.run_cli("checkout", "-b", "topic")
        self.write("a.txt", "y\n")
        tip = self.add_commit("tip", ts=1700000100)
        self.run_cli("checkout", "main")
        cid, conflicts, ff = merge(self.repo, "topic")
        self.assertTrue(ff)
        self.assertEqual(cid, tip)
        self.assertEqual(self.read("a.txt"), b"y\n")
        self.assertEqual(self.repo.head_commit(), tip)
        self.assertEqual(self.repo.resolve_branch("main"), tip)
        self.assertNotEqual(base, tip)

    def test_non_overlapping_textual_merge3(self):
        base = b"regionA v0\nmiddle\nregionB v0\n"
        a = b"regionA v1\nmiddle\nregionB v0\n"
        b = b"regionA v0\nmiddle\nregionB v1\n"
        merged, conflict = merge3(base, a, b)
        self.assertFalse(conflict)
        self.assertEqual(merged, b"regionA v1\nmiddle\nregionB v1\n")


# ----------------------------------------------------------- 6. 冲突合并


class TestConflictMerge(WorkdirTest):
    def test_merge3_conflict_keeps_both_sides(self):
        base = b"same line\nconflict zone v0\nlast\n"
        a = b"same line\nconflict zone ALICE\nlast\n"
        b = b"same line\nconflict zone BOB\nlast\n"
        merged, conflict = merge3(base, a, b, "main", "feature")
        self.assertTrue(conflict)
        text = merged.decode()
        self.assertIn("<<<<<<< main", text)
        self.assertIn("conflict zone ALICE", text)
        self.assertIn("=======", text)
        self.assertIn("conflict zone BOB", text)
        self.assertIn(">>>>>>> feature", text)
        # 未冲突区域保留
        self.assertIn("same line", text)
        self.assertIn("last", text)
        # 双方内容都保留，且顺序明确
        self.assertLess(text.index("ALICE"), text.index("======="))
        self.assertLess(text.index("======="), text.index("BOB"))

    def test_merge_command_conflict_exit_code_and_markers(self):
        self.write("f.txt", "shared base\n")
        self.add_commit("base", ts=1700000000)
        self.run_cli("checkout", "-b", "feature")
        self.write("f.txt", "shared FEATURE edit\n")
        self.add_commit("feature", ts=1700000100)
        self.run_cli("checkout", "main")
        self.write("f.txt", "shared MAIN edit\n")
        self.add_commit("main", ts=1700000200)

        cid, conflicts, ff = merge(self.repo, "feature")
        self.assertTrue(conflicts)
        self.assertEqual(cid, "")
        self.assertFalse(ff)
        text = self.read("f.txt").decode()
        self.assertIn("<<<<<<< main", text)
        self.assertIn("FEATURE edit", text)
        self.assertIn("MAIN edit", text)
        self.assertIn(">>>>>>> feature", text)

        # CLI 返回专门的冲突退出码 2
        code, _out, err = self.run_cli("merge", "feature")
        self.assertEqual(code, 2)
        self.assertIn("冲突", err)

    def test_modify_delete_conflict(self):
        self.write("f.txt", "v0\n")
        self.add_commit("base", ts=1700000000)
        self.run_cli("checkout", "-b", "feature")
        self.write("f.txt", "v1 on feature\n")
        self.add_commit("feature", ts=1700000100)
        self.run_cli("checkout", "main")
        os.remove(os.path.join(self.tmp, "f.txt"))
        self.repo.add(["f.txt"])
        self.add_commit("delete on main", ts=1700000200)
        _cid, conflicts, _ff = merge(self.repo, "feature")
        self.assertTrue(any("修改/删除冲突" in c for c in conflicts))

    def test_binary_conflict(self):
        base_flat = {}
        payload_a = bytes([0x00, 0x01, 0x02, 0xAA])
        payload_b = bytes([0x00, 0x01, 0x03, 0xBB])
        ob = self.repo.store.put_blob(payload_a)
        oa = self.repo.store.put_blob(bytes([1]))
        # 直接测 merge_trees：两侧二进制不同
        oid_base = self.repo.store.put_blob(bytes([0x00, 0x01]))
        oid_a = self.repo.store.put_blob(payload_a)
        oid_b = self.repo.store.put_blob(payload_b)
        flat_base = {"bin.dat": (obj.FILE_MODE, obj.BLOB, oid_base)}
        flat_a = {"bin.dat": (obj.FILE_MODE, obj.BLOB, oid_a)}
        flat_b = {"bin.dat": (obj.FILE_MODE, obj.BLOB, oid_b)}
        _res, conflicts = merge_trees(self.repo, flat_base, flat_a, flat_b)
        self.assertTrue(any("二进制" in c for c in conflicts))


# ----------------------------------------------------------- 7. fsck


class TestFsck(WorkdirTest):
    def _two_commits_repo(self):
        self.write("a.txt", "one\n")
        c1 = self.add_commit("c1", ts=1700000000)
        self.write("b.txt", "two\n")
        c2 = self.add_commit("c2", ts=1700000100)
        return c1, c2

    def test_clean_repo_passes(self):
        self._two_commits_repo()
        errors, warnings = fsck(self.repo)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_missing_object_detected(self):
        c1, c2 = self._two_commits_repo()
        # 删除 c1 引用链上的一个对象（c1 自身仍被 c2 的 parent 引用）
        info1 = self.repo.commit_info(c1)
        # 删除 c1 的 tree
        tree_oid = info1["tree"]
        os.remove(self.repo.store._path(tree_oid))
        errors, _ = fsck(self.repo)
        self.assertTrue(any("missing object" in e and tree_oid in e for e in errors),
                        f"未报告缺失对象: {errors}")

    def test_missing_commit_object(self):
        c1, c2 = self._two_commits_repo()
        os.remove(self.repo.store._path(c1))
        errors, _ = fsck(self.repo)
        self.assertTrue(any(c1 in e and "missing" in e for e in errors))

    def test_corrupt_hash_detected(self):
        c1, c2 = self._two_commits_repo()
        # 用一份合法压缩内容覆盖到错误的对象 ID 路径下 → 哈希不符
        path = self.repo.store._path(c1)
        import zlib
        evil = zlib.compress(b"blob 4\nevil")
        with open(path, "wb") as f:
            f.write(evil)
        errors, _ = fsck(self.repo)
        self.assertTrue(any("hash mismatch" in e or "corrupt" in e for e in errors),
                        f"未报告损坏对象: {errors}")

    def test_corrupt_compression_detected(self):
        c1, _ = self._two_commits_repo()
        with open(self.repo.store._path(c1), "wb") as f:
            f.write(b"this is not zlib at all")
        errors, _ = fsck(self.repo)
        self.assertTrue(any("corrupt" in e for e in errors))

    def test_invalid_ref_detected(self):
        self._two_commits_repo()
        ref = self.repo._ref_path("broken")
        os.makedirs(os.path.dirname(ref), exist_ok=True)
        with open(ref, "w") as f:
            f.write("deadbeef" * 8 + "\n")  # 不存在的 ID
        errors, _ = fsck(self.repo)
        self.assertTrue(any("invalid ref" in e and "broken" in e for e in errors),
                        f"未报告无效引用: {errors}")

    def test_unreachable_object_warned(self):
        self._two_commits_repo()
        dangling = self.repo.store.put_blob(b"nobody references me\n")
        _errors, warnings = fsck(self.repo)
        self.assertTrue(any(dangling in w and "unreachable" in w for w in warnings),
                        f"未报告不可达对象: {warnings}")

    def test_index_missing_blob_detected(self):
        self._two_commits_repo()
        entries = self.repo.read_index()
        entries[0]["oid"] = "a" * 64
        self.repo.write_index(entries)
        errors, _ = fsck(self.repo)
        self.assertTrue(any("index" in e for e in errors))


# ----------------------------------------------------------- 8. checkout 保护


class TestCheckoutProtection(WorkdirTest):
    def _branches_with_diverged_file(self):
        self.write("shared.txt", "original\n")
        self.write("only-main.txt", "main\n")
        self.add_commit("main c", ts=1700000000)
        self.run_cli("checkout", "-b", "feature")
        self.write("shared.txt", "feature version\n")
        self.write("only-feat.txt", "feat\n")
        os.remove(os.path.join(self.tmp, "only-main.txt"))
        self.add_commit("feature c", ts=1700000100)
        self.run_cli("checkout", "main")

    def test_refuse_when_uncommitted_change_would_be_overwritten(self):
        self._branches_with_diverged_file()
        # 在 main 上对 shared.txt 做未提交修改；feature 分支该文件不同
        self.write("shared.txt", "my dirty local edit\n")
        feat_flat = self.repo.flatten_tree(
            self.repo.commit_info(self.repo.resolve_branch("feature"))["tree"])
        problems = switch_check(self.repo, feat_flat)
        self.assertIn("shared.txt", problems)

        with self.assertRaises(RepoError) as ctx:
            checkout(self.repo, "feature")
        self.assertIn("shared.txt", str(ctx.exception))

        # 被拒绝后工作区原封不动、HEAD 仍是 main
        self.assertEqual(self.read("shared.txt"), b"my dirty local edit\n")
        self.assertEqual(self.repo.head_refname(), "main")
        # CLI 返回非零
        code, _, err = self.run_cli("checkout", "feature")
        self.assertNotEqual(code, 0)
        self.assertIn("shared.txt", err)

    def test_refuse_overwriting_untracked_file(self):
        self._branches_with_diverged_file()
        # only-feat.txt 在 feature 分支是受控文件；main 工作区里放一个同名未跟踪文件
        self.write("only-feat.txt", "local untracked payload\n")
        with self.assertRaises(RepoError):
            checkout(self.repo, "feature")
        # 未跟踪内容未被覆盖
        self.assertEqual(self.read("only-feat.txt"), b"local untracked payload\n")

    def test_staged_new_file_protected(self):
        self._branches_with_diverged_file()
        # 暂存一个新文件后切到不存在该路径的 feature：会丢失暂存内容 ⇒ 拒绝
        self.write("staged-new.txt", "staged but uncommitted\n")
        self.repo.add(["staged-new.txt"])
        with self.assertRaises(RepoError):
            checkout(self.repo, "feature")
        self.assertEqual(self.read("staged-new.txt"), b"staged but uncommitted\n")

    def test_safe_switch_when_no_conflict(self):
        self._branches_with_diverged_file()
        # 干净工作区切换成功
        mode, target = checkout(self.repo, "feature")
        self.assertEqual(mode, "branch")
        self.assertEqual(self.read("shared.txt"), b"feature version\n")
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "only-feat.txt")))
        # only-main.txt 在 feature 树中不存在、且无本地修改 → 随切换删除
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "only-main.txt")))


class TestStatusAndEndToEnd(WorkdirTest):
    def test_status_categories(self):
        self.write("tracked.txt", "v1\n")
        self.repo.add(["."])
        self.add_commit("c", ts=1700000000)
        self.write("tracked.txt", "v2\n")          # 未暂存修改
        self.write("added.txt", "staged\n")
        self.repo.add(["added.txt"])
        self.write("untracked.txt", "??\n")
        st = self.repo.status()
        paths_staged = {p for _s, p in st["staged"]}
        paths_unstaged = {p for _s, p in st["unstaged"]}
        self.assertIn("added.txt", paths_staged)
        self.assertIn("tracked.txt", paths_unstaged)
        self.assertIn("untracked.txt", st["untracked"])

    def test_metadata_dir_never_tracked(self):
        self.write("x.txt", "x\n")
        files = self.repo.worktree_files()
        self.assertIn("x.txt", files)
        self.assertFalse(any(p.startswith(DIR_NAME + "/") for p in files))


if __name__ == "__main__":
    unittest.main(verbosity=2)
