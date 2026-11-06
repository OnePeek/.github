"""Tests for workspace/done_check.py (the builder's done-gate, pitfalls.md 3; agent-operations.md §5.4 step 5)."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "done_check", pathlib.Path(__file__).resolve().parents[1] / "workspace" / "done_check.py")
dc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(dc)


def setUpModule() -> None:
    """Clear Git's repository-local variables (`git rev-parse --local-env-vars`: GIT_DIR, GIT_INDEX_FILE and the rest),
    as a hook passes them on, so the fixtures and the code under test touch only the fixture repos (CodeRabbit on #60:
    an inherited GIT_DIR had the fixture rewrite another repo's config)."""
    names = subprocess.run(["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True, check=True).stdout.split()
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    for name in names:
        os.environ.pop(name, None)


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Repo:
    """A bare 'origin', a clone of it, and a linked worktree on branch `feat`, so push state is checked offline."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.origin = root / "origin.git"
        git(root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.clone = root / "clone"
        git(root, "clone", "-q", str(self.origin), str(self.clone))
        for k, v in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
            git(self.clone, "config", k, v)
        (self.clone / "a.txt").write_text("a\n")
        git(self.clone, "add", "a.txt")
        git(self.clone, "commit", "-q", "-m", "a")
        git(self.clone, "push", "-q", "-u", "origin", "main")
        self.work = root / "wt"
        git(self.clone, "worktree", "add", "-q", "-b", "feat", str(self.work), "origin/main")
        git(self.work, "push", "-q", "-u", "origin", "feat")

    def commit(self, files: dict[str, str], push: bool = True) -> str:
        for name, text in files.items():
            p = self.work / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
            git(self.work, "add", "--", name)
        git(self.work, "commit", "-q", "-m", "change")
        if push:
            git(self.work, "push", "-q", "origin", "HEAD:feat")
        return self.head()

    def head(self) -> str:
        return git(self.work, "rev-parse", "HEAD")

    def close(self):
        self.tmp.cleanup()


class FakeGh:
    """Stands in for `gh` only; every git command runs for real. `files` are what the API's jq would print."""

    def __init__(self, heads, files, branch="feat", base="main", fail_fetch=False, fork=None):
        self.heads, self.files, self.branch, self.base, self.fail_fetch = list(heads), files, branch, base, fail_fetch
        self.fork = fork  # (owner, name, {branch: sha}) for a pull request from a fork
        self.calls = []
        self.real = dc.run

    def __call__(self, cmd, cwd):
        self.calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "view"]:
            head = self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]
            info = {"headRefName": self.branch, "headRefOid": head, "baseRefName": self.base,
                    "isCrossRepository": bool(self.fork)}
            if self.fork:
                info |= {"headRepositoryOwner": {"login": self.fork[0]}, "headRepository": {"name": self.fork[1]}}
            return json.dumps(info)
        if self.fork and cmd[:2] == ["git", "ls-remote"] and cmd[2] == f"https://github.com/{self.fork[0]}/{self.fork[1]}.git":
            ref = cmd[3].removeprefix("refs/heads/")
            return f"{self.fork[2][ref]}\t{cmd[3]}\n" if ref in self.fork[2] else ""
        if cmd[:2] == ["gh", "api"]:
            return "".join(json.dumps(f) + "\n" for f in self.files)
        if cmd[:2] == ["git", "fetch"] and self.fail_fetch:
            raise subprocess.CalledProcessError(128, cmd, "", "fatal: could not read from remote")
        return self.real(cmd, cwd)


class DirtyFiles(unittest.TestCase):
    def test_lists_modified_untracked_and_renamed_from_nul_separated_output(self):
        status = " M app/x.py\0?? new.txt\0R  new.py\0old.py\0?? after.txt\0"
        self.assertEqual(dc.dirty_files(status), ["app/x.py", "new.txt", "new.py", "after.txt"])

    def test_ignores_only_untracked_files_under_a_top_level_context_folder(self):
        self.assertEqual(dc.dirty_files("?? .context/unit.md\0"), [])
        self.assertEqual(dc.dirty_files(" M .context/tracked.md\0"), [".context/tracked.md"])
        self.assertEqual(dc.dirty_files("?? x -> .context/f\0"), ["x -> .context/f"])
        self.assertEqual(dc.dirty_files("?? sub/.context/f\0"), ["sub/.context/f"])
        self.assertEqual(dc.dirty_files("?? .contexts/f\0"), [".contexts/f"])


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


class ChangedLines(unittest.TestCase):
    def test_counts_added_and_deleted_and_skips_generated_and_binary(self):
        numstat = "10\t5\tapp/x.py\0" "900\t0\tuv.lock\0" "50\t50\tweb/pnpm-lock.yaml\0" "7\t0\tCargo.lock\0" \
                  "300\t0\trendered/page.html\0" "40\t0\tdocs/rendered/p.html\0" "-\t-\tlogo.png\0" "1\t1\tsp ace é.md\0"
        self.assertEqual(dc.changed_lines(numstat), 17)

    def test_a_name_that_only_looks_generated_counts(self):
        self.assertEqual(dc.changed_lines("3\t0\trendered.py\0" "2\t0\tuv.lock.md\0"), 5)


class Check(unittest.TestCase):
    def setUp(self):
        self.r = Repo()
        self.addCleanup(self.r.close)

    def check(self, *args, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            problems = dc.check(self.r.work, *args, **kw)
        return problems, out.getvalue()

    def with_gh(self, fake):
        return mock.patch.object(dc, "run", side_effect=fake)

    def test_clean_and_pushed_is_ok(self):
        self.assertEqual(self.check(None, None)[0], [])

    def test_a_plain_clone_is_not_a_linked_worktree(self):
        problems = dc.check(self.r.clone, None, None)
        self.assertTrue(any("not a linked worktree" in p for p in problems), problems)

    def test_an_uncommitted_file_fails(self):
        (self.r.work / "a.txt").write_text("changed\n")
        self.assertTrue(any("uncommitted" in p for p in self.check(None, None)[0]))

    def test_an_untracked_file_fails(self):
        (self.r.work / "forgot.sh").write_text("x\n")
        self.assertTrue(any("forgot.sh" in p for p in self.check(None, None)[0]))

    def test_untracked_context_files_are_ignored_but_a_changed_tracked_one_fails(self):
        (self.r.work / ".context").mkdir()
        (self.r.work / ".context" / "status.md").write_text("phase: done\n")
        self.assertEqual(self.check(None, None)[0], [])
        self.r.commit({".context/kept.md": "x\n"})
        (self.r.work / ".context" / "kept.md").write_text("y\n")
        problems = self.check(None, None)[0]
        self.assertTrue(any(".context/kept.md" in p for p in problems), problems)
        self.assertFalse(any("status.md" in p for p in problems), problems)

    def test_a_folder_named_like_an_arrow_to_context_is_reported(self):
        (self.r.work / "x -> .context").mkdir()
        (self.r.work / "x -> .context" / "f").write_text("x\n")
        self.assertTrue(any("x -> .context/f" in p for p in self.check(None, None)[0]))

    def test_spaces_and_non_ascii_names_are_reported_exactly(self):
        (self.r.work / "my file é.txt").write_text("x\n")
        problems = self.check(None, None)[0]
        self.assertTrue(any("my file é.txt" in p for p in problems), problems)

    def test_a_commit_not_pushed_fails(self):
        self.r.commit({"b.txt": "b\n"}, push=False)
        self.assertTrue(any("push HEAD" in p for p in self.check(None, None)[0]))

    def test_a_named_branch_missing_on_origin_fails(self):
        self.assertTrue(any("not on origin" in p for p in self.check(None, "feat/none")[0]))

    # --pr, end to end: every git command is real, only gh is stubbed.
    def test_pr_with_matching_head_and_files_is_ok_and_prints_the_files(self):
        head = self.r.commit({"b.txt": "b\n", "dir/c d é.txt": "c\n"})
        with self.with_gh(FakeGh([head], ["b.txt", "dir/c d é.txt"])):
            problems, out = self.check("x#1", None)
        self.assertEqual(problems, [])
        self.assertIn("pull request files (2):", out)
        self.assertIn("  dir/c d é.txt", out)

    def test_pr_head_that_never_arrives_fails(self):
        head = self.r.commit({"b.txt": "b\n"})
        fake = FakeGh(["0" * 40], ["b.txt"])
        with self.with_gh(fake), mock.patch.object(dc, "PR_READ_WAIT", 0):
            problems, _ = self.check("x#1", None)
        self.assertTrue(any("pull request's head is 0000000" in p and head[:7] in p for p in problems), problems)
        self.assertEqual(sum(c[:3] == ["gh", "pr", "view"] for c in fake.calls), dc.PR_READ_TRIES)

    def test_pr_missing_a_changed_file_fails(self):
        head = self.r.commit({"b.txt": "b\n", "boot-image.sh": "x\n"})
        with self.with_gh(FakeGh([head], ["b.txt"])):
            problems, _ = self.check("x#1", None)
        self.assertTrue(any("not in the pull request: boot-image.sh" in p for p in problems), problems)

    def test_pr_branch_not_pushed_fails(self):
        head = self.r.commit({"b.txt": "b\n"}, push=False)
        with self.with_gh(FakeGh([head], ["b.txt"])):
            problems, _ = self.check("x#1", None)
        self.assertTrue(any(p.startswith("origin/feat is") for p in problems), problems)

    def test_a_branch_other_than_the_pull_requests_head_branch_fails(self):
        head = self.r.commit({"b.txt": "b\n"})
        with self.with_gh(FakeGh([head], ["b.txt"])):
            problems, _ = self.check("x#1", "main")
        self.assertTrue(any("not the pull request's head branch feat" in p for p in problems), problems)

    def test_pr_base_fetch_failure_fails_closed(self):
        head = self.r.commit({"b.txt": "b\n"})
        with self.with_gh(FakeGh([head], ["b.txt"], fail_fetch=True)):
            problems, _ = self.check("x#1", None)
        self.assertTrue(any("could not fetch origin/main" in p for p in problems), problems)

    def test_pr_behind_its_base_fails(self):
        head = self.r.commit({"b.txt": "b\n"})
        (self.r.clone / "z.txt").write_text("z\n")
        git(self.r.clone, "add", "z.txt")
        git(self.r.clone, "commit", "-q", "-m", "z")
        git(self.r.clone, "push", "-q", "origin", "main")
        with self.with_gh(FakeGh([head], ["b.txt"])):
            problems, _ = self.check("x#1", None)
        self.assertTrue(any("does not contain the current origin/main" in p for p in problems), problems)

    def test_pr_with_over_100_files_reads_every_page(self):
        names = [f"f{i:03}.txt" for i in range(150)]
        head = self.r.commit({n: "x\n" for n in names})
        fake = FakeGh([head], names)
        with self.with_gh(fake):
            problems, out = self.check("x#1", None)
        self.assertEqual(problems, [])
        self.assertIn("pull request files (150):", out)
        api = [c for c in fake.calls if c[:2] == ["gh", "api"]]
        self.assertEqual(len(api), 1)
        self.assertIn("--paginate", api[0])
        self.assertIn("repos/OnePeek/x/pulls/1/files?per_page=100", api[0])

    def test_renames_count_both_paths_whatever_diff_renames_says(self):
        current = "a.txt"
        for setting in ("false", "true"):
            with self.subTest(setting=setting):
                git(self.r.work, "config", "diff.renames", setting)
                git(self.r.work, "mv", current, f"moved-{setting}.txt")
                current = f"moved-{setting}.txt"
                git(self.r.work, "commit", "-q", "-m", "mv")
                git(self.r.work, "push", "-q", "origin", "HEAD:feat")
                with self.with_gh(FakeGh([self.r.head()], [current, "a.txt"])):  # the API's filename, previous_filename
                    problems, _ = self.check("x#1", None)
                self.assertEqual(problems, [])

    def test_expect_must_equal_the_pull_requests_files(self):
        head = self.r.commit({"b.txt": "b\n", "c.txt": "c\n"})
        with self.with_gh(FakeGh([head], ["b.txt", "c.txt"])):
            self.assertEqual(self.check("x#1", None, ["c.txt", "b.txt"])[0], [])
            problems, out = self.check("x#1", None, ["b.txt", "d.txt"])
        self.assertIn("reported but not in the pull request: d.txt", problems)
        self.assertIn("in the pull request but not reported: c.txt", problems)
        self.assertIn("  c.txt", out)

    def test_a_pull_request_committing_a_context_file_fails(self):
        # Review round 2 of #60: .context/status.md was committed and nothing caught it.
        head = self.r.commit({".context/status.md": "phase: done\n"})
        with self.with_gh(FakeGh([head], [".context/status.md"])):
            problems, _ = self.check("x#1", None)
        self.assertTrue(any(".context/status.md" in p for p in problems), problems)

    def test_a_committed_context_file_fails_without_a_pull_request_too(self):
        # CodeRabbit on #60: the --branch and upstream modes never looked for a committed .context/ file.
        self.r.commit({".context/status.md": "phase: done\n"})
        for branch in (None, "feat"):
            with self.subTest(branch=branch):
                problems = self.check(None, branch)[0]
                self.assertTrue(any("HEAD commits .context/ files" in p and ".context/status.md" in p for p in problems),
                                problems)

    def test_a_pull_request_deleting_an_old_context_file_passes(self):
        # CodeRabbit on #60: the removal of a file committed by mistake must be able to pass.
        self.r.commit({".context/old.md": "x\n"})
        git(self.r.work, "push", "-q", "origin", "HEAD:main")
        git(self.r.work, "rm", "-q", ".context/old.md")
        git(self.r.work, "commit", "-q", "-m", "rm")
        git(self.r.work, "push", "-q", "origin", "HEAD:feat")
        with self.with_gh(FakeGh([self.r.head()], [".context/old.md"])):
            problems, _ = self.check("x#1", None)
        self.assertEqual(problems, [])

    def test_a_fork_pull_request_is_checked_in_the_fork_not_in_origin(self):
        # CodeRabbit on #60: a fork's head branch is not in origin, so origin alone reported it unpushed.
        head = self.r.commit({"b.txt": "b\n"})
        with self.with_gh(FakeGh([head], ["b.txt"], branch="fork-only", fork=("someone", "x", {"fork-only": head}))):
            self.assertEqual(self.check("x#1", None)[0], [])
        with self.with_gh(FakeGh([head], ["b.txt"], branch="fork-only", fork=("someone", "x", {"fork-only": "1" * 40}))):
            problems = self.check("x#1", None)[0]
        self.assertTrue(any(p.startswith("https://github.com/someone/x.git fork-only is 1111111") for p in problems),
                        problems)
        with self.with_gh(FakeGh([head], ["b.txt"], branch="fork-only", fork=("someone", "x", {}))):
            problems = self.check("x#1", None)[0]
        self.assertIn("branch fork-only is not on https://github.com/someone/x.git: push it", problems)

    def test_expect_without_pr_fails(self):
        self.assertTrue(any("--expect needs --pr" in p for p in self.check(None, None, ["a.txt"])[0]))

    def test_over_the_slice_cap_warns_and_generated_files_do_not_count(self):
        head = self.r.commit({"uv.lock": "x\n" * 900, "rendered/p.html": "x\n" * 900, "b.txt": "x\n" * dc.SLICE_CAP})
        with self.with_gh(FakeGh([head], ["uv.lock", "rendered/p.html", "b.txt"])):
            problems, out = self.check("x#1", None)
        self.assertEqual(problems, [])
        self.assertNotIn("WARN", out)
        head = self.r.commit({"c.txt": "x\n"})
        with self.with_gh(FakeGh([head], ["uv.lock", "rendered/p.html", "b.txt", "c.txt"])):
            problems, out = self.check("x#1", None)
        self.assertEqual(problems, [])  # a warning, not a failure
        self.assertIn(f"WARN  {dc.SLICE_CAP + 1} changed lines", out)


class Main(unittest.TestCase):
    def setUp(self):
        self.r = Repo()
        self.addCleanup(self.r.close)

    def main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = dc.main([*argv])
        return code, out.getvalue().splitlines()[-1]

    def test_ok_and_failed_lines(self):
        self.assertEqual(self.main(str(self.r.work)), (0, "DONE CHECK OK"))
        self.assertEqual(self.main(str(self.r.work), "--pr", "not-a-pr"), (1, "DONE CHECK FAILED"))

    def test_a_missing_worktree_fails_closed(self):  # OSError
        self.assertEqual(self.main(str(self.r.work / "gone")), (1, "DONE CHECK FAILED"))

    def test_a_null_head_from_the_api_fails_closed(self):  # TypeError
        fake = FakeGh([None], [])
        with mock.patch.object(dc, "run", side_effect=fake), mock.patch.object(dc, "PR_READ_WAIT", 0):
            self.assertEqual(self.main(str(self.r.work), "--pr", "x#1"), (1, "DONE CHECK FAILED"))

    def test_a_fork_that_was_deleted_fails_closed(self):  # TypeError: GitHub gives a null head repository
        real = dc.run

        def fake(cmd, cwd):
            if cmd[:3] == ["gh", "pr", "view"]:
                return json.dumps({"headRefName": "feat", "headRefOid": self.r.head(), "baseRefName": "main",
                                   "isCrossRepository": True, "headRepositoryOwner": None, "headRepository": None})
            return "" if cmd[:2] == ["gh", "api"] else real(cmd, cwd)
        with mock.patch.object(dc, "run", side_effect=fake):
            self.assertEqual(self.main(str(self.r.work), "--pr", "x#1"), (1, "DONE CHECK FAILED"))

    def test_the_script_is_executable(self):
        self.assertTrue(pathlib.Path(dc.__file__).stat().st_mode & 0o111)


@unittest.skipUnless(shutil.which("jq"), "jq is not installed")
class PrFilesJq(unittest.TestCase):
    def test_the_jq_filter_prints_one_json_string_per_path_and_a_renames_old_path(self):
        seen = []

        def run(cmd, cwd):
            seen.append(cmd)
            page = [{"filename": "new name.py", "previous_filename": "old.py"}, {"filename": "a\nb.txt"}]
            return subprocess.run(["jq", "-r", cmd[cmd.index("--jq") + 1]], input=json.dumps(page), check=True,
                                  capture_output=True, text=True).stdout
        with mock.patch.object(dc, "run", side_effect=run):
            self.assertEqual(dc.pr_files("OnePeek/x", "1", pathlib.Path(".")), ["new name.py", "old.py", "a\nb.txt"])



class ContextNeverCommitted(unittest.TestCase):
    """Review round 2 of #60: a committed .context/status.md slipped through, because done_check skips only untracked ones.
    `context_paths` gets every path HEAD tracks (`git ls-tree -r HEAD`)."""

    def test_a_committed_context_file_fails(self):
        problems = dc.context_paths(["workspace/x.py", ".context/status.md"])
        self.assertEqual(len(problems), 1)
        self.assertIn(".context/status.md", problems[0])

    def test_a_folder_merely_named_like_it_passes(self):
        self.assertEqual(dc.context_paths(["docs/.context-notes.md", "a/.context/b.md"]), [])


if __name__ == "__main__":
    unittest.main()
