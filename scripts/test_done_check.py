"""Tests for workspace/done_check.py (the builder's done-gate, pitfalls.md 3)."""
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "workspace"))
import done_check as dc  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Repo:
    """A bare 'origin' and a clone, so push state can be checked without the network."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.origin = root / "origin.git"
        git(root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.work = root / "work"
        git(root, "clone", "-q", str(self.origin), str(self.work))
        for k, v in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
            git(self.work, "config", k, v)
        (self.work / "a.txt").write_text("a\n")
        git(self.work, "add", "a.txt")
        git(self.work, "commit", "-q", "-m", "a")
        git(self.work, "push", "-q", "-u", "origin", "main")

    def close(self):
        self.tmp.cleanup()


class DirtyFiles(unittest.TestCase):
    def test_lists_modified_untracked_and_renamed(self):
        status = " M app/x.py\n?? new.txt\nR  old.py -> new.py\n"
        self.assertEqual(dc.dirty_files(status), ["app/x.py", "new.txt", "new.py"])

    def test_ignores_the_packet_folder(self):
        self.assertEqual(dc.dirty_files("?? .context/unit.md\n"), [])


class CompareFiles(unittest.TestCase):
    def test_equal_lists_pass(self):
        self.assertEqual(dc.compare_files(["a", "b"], ["b", "a"]), [])

    def test_a_file_left_out_of_the_pull_request_fails(self):
        # The 2026-11-06 case: 4 of 6 changed files were never committed and pushed.
        problems = dc.compare_files(["fixtures.sh"], ["fixtures.sh", "boot-image.sh"])
        self.assertEqual(len(problems), 1)
        self.assertIn("boot-image.sh", problems[0])

    def test_an_extra_file_in_the_pull_request_fails(self):
        self.assertIn("stale.py", dc.compare_files(["stale.py"], [])[0])


class ParsePr(unittest.TestCase):
    def test_short_and_full_forms(self):
        self.assertEqual(dc.parse_pr("onepeek-infra#177"), ("OnePeek/onepeek-infra", "177"))
        self.assertEqual(dc.parse_pr("OnePeek/.github#55"), ("OnePeek/.github", "55"))

    def test_bad_forms_are_refused(self):
        for bad in ("onepeek-infra", "#3", "repo#x"):
            with self.assertRaises(ValueError):
                dc.parse_pr(bad)


class Check(unittest.TestCase):
    def setUp(self):
        self.r = Repo()

    def tearDown(self):
        self.r.close()

    def test_clean_and_pushed_is_ok(self):
        self.assertEqual(dc.check(self.r.work, None, None), [])

    def test_an_uncommitted_file_fails(self):
        (self.r.work / "a.txt").write_text("changed\n")
        self.assertTrue(any("uncommitted" in p for p in dc.check(self.r.work, None, None)))

    def test_an_untracked_file_fails(self):
        (self.r.work / "forgot.sh").write_text("x\n")
        self.assertTrue(any("forgot.sh" in p for p in dc.check(self.r.work, None, None)))

    def test_a_commit_not_pushed_fails(self):
        (self.r.work / "b.txt").write_text("b\n")
        git(self.r.work, "add", "b.txt")
        git(self.r.work, "commit", "-q", "-m", "b")
        self.assertTrue(any("push HEAD" in p for p in dc.check(self.r.work, None, None)))

    def test_a_named_branch_missing_on_origin_fails(self):
        self.assertTrue(any("not on origin" in p for p in dc.check(self.r.work, None, "feat/none")))

    def test_main_returns_1_and_fails_closed_on_errors(self):
        self.assertEqual(dc.main([str(self.r.work)]), 0)
        self.assertEqual(dc.main([str(self.r.work), "--pr", "not-a-pr"]), 1)


if __name__ == "__main__":
    unittest.main()
