"""Tests for workspace/ship_change_set.py: order, input format, pin rewrite, checks, conflicts, errors (no network)."""
from __future__ import annotations

import importlib.util
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "ship_change_set", pathlib.Path(__file__).resolve().parents[1] / "workspace" / "ship_change_set.py"
)
ship = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ship)


class Order(unittest.TestCase):
    def test_docs_then_github_then_infra_then_callers(self) -> None:
        given = ["onepeek-web", "mcp-athena", "backend-service", ".github", "onepeek-infra", "onepeek-docs",
                 "onepeek-design-system"]
        self.assertEqual(
            sorted(given, key=ship.rank),
            ["onepeek-docs", ".github", "onepeek-infra", "onepeek-design-system", "backend-service", "onepeek-web",
             "mcp-athena"],
        )

    def test_bases(self) -> None:
        self.assertEqual(ship.BASE.get("onepeek-docs"), "main")
        self.assertEqual(ship.BASE.get(".github"), "main")
        self.assertEqual(ship.BASE.get("backend-service", "dev"), "dev")


class Parse(unittest.TestCase):
    def test_repo_and_number(self) -> None:
        self.assertEqual(ship.parse(".github#22"), (".github", 22))
        self.assertEqual(ship.parse("onepeek-web#7"), ("onepeek-web", 7))

    def test_rejects_other_shapes(self) -> None:
        for bad in ("onepeek-web", "#3", "onepeek-web#x", "OnePeek/onepeek-web#3"):
            with self.assertRaises(SystemExit):
                ship.parse(bad)


class Pin(unittest.TestCase):
    def test_rewrites_only_shared_workflow_pins(self) -> None:
        old_sha, new_sha = "a" * 40, "b" * 40
        text = (
            f"    uses: OnePeek/.github/.github/workflows/contract.yml@{old_sha} # v1.1.2\n"
            f"    uses: actions/checkout@{old_sha} # v7\n"
        )
        out = ship.PIN.sub(lambda m: ship.moved_pin(m, "v1.1.3", new_sha), text)
        self.assertIn(f"contract.yml@{new_sha} # v1.1.3", out)
        self.assertIn(f"actions/checkout@{old_sha} # v7", out)

    def test_only_the_sha_and_the_tag_change_and_trailing_comment_text_is_kept(self) -> None:
        new = "b" * 40
        cases = {
            f"    uses: OnePeek/.github/.github/workflows/a.yml@{'a' * 40} # v1.1.2 (shared CI, see #12)\n":
                f"    uses: OnePeek/.github/.github/workflows/a.yml@{new} # v1.1.3 (shared CI, see #12)\n",
            f"    uses: onepeek/.GITHUB/.github/workflows/a.yml@main\n":  # any letter case and any ref (P2)
                f"    uses: onepeek/.GITHUB/.github/workflows/a.yml@{new} # v1.1.3\n",
            f"    - uses: 'OnePeek/.github/.github/workflows/a.yml@v1.0.0' # v1.0.0\n":
                f"    - uses: 'OnePeek/.github/.github/workflows/a.yml@{new}' # v1.1.3\n",
            f"    uses: OnePeek/.github/.github/workflows/a.yml@{'c' * 40} # pre-tag (OnePeek/.github#34)\n":
                f"    uses: OnePeek/.github/.github/workflows/a.yml@{new} # v1.1.3\n",
        }
        for before, after in cases.items():
            self.assertEqual(ship.PIN.sub(lambda m: ship.moved_pin(m, "v1.1.3", new), before), after, before)



def done(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class Checks(unittest.TestCase):
    def test_pending_checks_are_read_although_gh_exits_non_zero(self) -> None:
        with mock.patch.object(ship.subprocess, "run", return_value=done(8, '[{"name":"a","bucket":"pending"}]')):
            self.assertEqual(ship.read_checks("r", 1, True), [{"name": "a", "bucket": "pending"}])

    def test_none_required_is_empty_only_from_gh_own_message(self) -> None:
        msg = done(1, "", "no required checks reported on the 'x' branch")
        with mock.patch.object(ship.subprocess, "run", return_value=msg):
            self.assertEqual(ship.read_checks("r", 1, True), [])

    def test_any_other_failure_stops(self) -> None:
        with mock.patch.object(ship.subprocess, "run", return_value=done(1, "", "HTTP 502")), \
                self.assertRaises(SystemExit):
            ship.read_checks("r", 1, True)

    def test_without_required_checks_waits_on_all_but_the_ai_review(self) -> None:
        every = [{"name": "ci", "bucket": "pass"}, {"name": "CodeRabbit", "bucket": "pending"}]
        with mock.patch.object(ship, "read_checks", side_effect=lambda r, n, req: [] if req else every):
            self.assertEqual(ship.checks("r", 1), [{"name": "ci", "bucket": "pass"}])


class Conflicts(unittest.TestCase):
    def test_unknown_is_not_clean(self) -> None:
        with mock.patch.object(ship, "mergeable", return_value="UNKNOWN"), self.assertRaises(SystemExit):
            ship.no_conflicts([("r", 1)])

    def test_mergeable_passes(self) -> None:
        with mock.patch.object(ship, "mergeable", return_value="MERGEABLE"):
            ship.no_conflicts([("r", 1)])


class Errors(unittest.TestCase):
    def test_command_failure_stops_with_its_error_text(self) -> None:
        failed = done(1, "", "Pull request is not mergeable")
        with mock.patch.object(ship.subprocess, "run", return_value=failed), self.assertRaises(SystemExit) as stop:
            ship.gh("pr", "merge", "1")
        self.assertIn("not mergeable", str(stop.exception))


def sh(*cmd: str, cwd: pathlib.Path | None = None) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout


def git_repo(root: pathlib.Path, name: str, workflow: str, base: str = "dev") -> pathlib.Path:
    """A clone `root/name` of a local bare origin whose `base` branch holds .github/workflows/ci.yml."""
    bare = root / "origins" / f"{name}.git"
    sh("git", "init", "-q", "--bare", str(bare))
    src = root / name
    sh("git", "clone", "-q", str(bare), str(src))
    sh("git", "-C", str(src), "switch", "-q", "-c", base)
    (src / ".github/workflows").mkdir(parents=True)
    (src / ".github/workflows/ci.yml").write_text(workflow)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false"), ("core.hooksPath", "/dev/null")):
        sh("git", "-C", str(src), "config", k, v)
    sh("git", "-C", str(src), "add", ".")
    sh("git", "-C", str(src), "commit", "-q", "-m", "init")
    sh("git", "-C", str(src), "push", "-q", "origin", base)
    return src


OLD, NEW = "a" * 40, "b" * 40
USES = "    uses: OnePeek/.github/.github/workflows/secret-scan.yml@{} # {}\n"


class OrderOverride(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(ship, "docs_pin_problems", return_value=[])  # no network in these tests
        patcher.start()
        self.addCleanup(patcher.stop)

    PRS = [("onepeek-infra", 1), ("backend-service", 2), ("onepeek-docs", 3)]

    def test_default_order_is_unchanged(self) -> None:
        self.assertEqual([r for r, _ in ship.ordered(self.PRS, "")], ["onepeek-docs", "onepeek-infra", "backend-service"])

    def test_order_overrides_the_fixed_order(self) -> None:
        got = ship.ordered(self.PRS, "onepeek-docs,backend-service,onepeek-infra")
        self.assertEqual(got, [("onepeek-docs", 3), ("backend-service", 2), ("onepeek-infra", 1)])

    def test_order_never_moves_onepeek_docs_off_first_place(self) -> None:  # §8a P2
        for bad in ("backend-service,onepeek-docs,onepeek-infra", "backend-service,onepeek-infra,onepeek-docs"):
            with self.assertRaises(SystemExit) as stop:
                ship.ordered(self.PRS, bad)
            self.assertIn("onepeek-docs first", str(stop.exception))
        self.assertEqual(ship.ordered([("backend-service", 2), ("onepeek-infra", 1)], "backend-service,onepeek-infra"),
                         [("backend-service", 2), ("onepeek-infra", 1)])  # no docs pull request in the set

    def test_order_must_name_every_repo_of_the_set_once(self) -> None:
        for bad in ("backend-service,onepeek-infra", "onepeek-docs,backend-service,onepeek-infra,onepeek-web",
                    "onepeek-docs,backend-service,backend-service"):
            with self.assertRaises(SystemExit):
                ship.ordered(self.PRS, bad)

    def test_the_flag_reaches_the_plan(self) -> None:
        seen = []
        with mock.patch.object(ship, "pr_info", side_effect=lambda r, n: seen.append(r) or {
                "state": "OPEN", "baseRefName": ship.BASE.get(r, "dev"), "isDraft": False, "mergeable": "MERGEABLE",
                "headRefName": "x", "headRefOid": "c" * 40}):
            self.assertEqual(ship.main(["onepeek-infra#1", "backend-service#2", "--order", "backend-service,onepeek-infra"]), 0)
        self.assertEqual(seen, ["backend-service", "onepeek-infra"])


class MinorBehind(unittest.TestCase):
    def test_one_minor_behind_needs_no_note(self) -> None:
        self.assertFalse(ship.minor_behind({"v1.7.0", "v1.8.1"}, "v1.8.2"))
        self.assertFalse(ship.minor_behind({"v1.7.3"}, "v1.8.0"))

    def test_more_than_one_minor_or_a_major_or_untagged_needs_the_note(self) -> None:
        self.assertTrue(ship.minor_behind({"v1.7.0", "v1.1.1"}, "v1.8.2"))
        self.assertTrue(ship.minor_behind({"v1.9.0"}, "v2.0.0"))
        self.assertTrue(ship.minor_behind({"untagged"}, "v1.8.2"))


class PinMoves(unittest.TestCase):
    def setUp(self) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        patcher = mock.patch.object(ship, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_callers_are_the_repos_whose_base_branch_pins_github(self) -> None:
        git_repo(self.root, "backend-service", "jobs:\n  s:\n" + USES.format(OLD, "v1.7.0"))
        git_repo(self.root, "onepeek-docs", "jobs:\n  s:\n" + USES.format(OLD, "v1.4.0"), base="main")
        git_repo(self.root, "mcp-athena", "jobs:\n  s:\n    uses: onepeek/.github/.github/workflows/x.yml@main\n")
        git_repo(self.root, "marketing-website", "jobs:\n  s:\n    uses: OnePeek/.github-other/x.yml@main\n")
        self.assertEqual(ship.callers(), ["backend-service", "mcp-athena", "onepeek-docs"])

    def fake_gh(self, prs: list | None = None, calls: list | None = None):
        """`gh pr list` answers the open pull requests for the branch; `gh pr create` answers a new URL."""
        def gh(*args):
            if calls is not None:
                calls.append(args)
            if args[:2] == ("pr", "list"):
                return __import__("json").dumps(prs or [])
            if args[:2] == ("pr", "create"):
                return "https://github.com/OnePeek/x/pull/9\n"
            raise AssertionError(args)
        return gh

    def origin_show(self, repo: str, what: str) -> str:
        return sh("git", "--git-dir", str(self.root / f"origins/{repo}.git"), "show", what)

    def test_a_pin_move_pull_request_moves_every_pin_on_a_new_branch(self) -> None:
        src = git_repo(self.root, "onepeek-web", "jobs:\n  s:\n" + USES.format(OLD, "v1.7.0") + USES.format(OLD, "v1.7.0"))
        calls: list = []
        with mock.patch.object(ship, "gh", side_effect=self.fake_gh(calls=calls)):
            self.assertEqual(ship.open_pin_move("onepeek-web", "v1.8.2", NEW), "https://github.com/OnePeek/x/pull/9")
        text = self.origin_show("onepeek-web", "chore/pin-github-v1.8.2:.github/workflows/ci.yml")
        self.assertEqual(text.count(f"@{NEW} # v1.8.2"), 2)
        self.assertNotIn(OLD, text)
        args = calls[-1]
        self.assertEqual(args[args.index("--base") + 1], "dev")
        self.assertNotIn("More than one minor version behind", args[args.index("--body") + 1])
        self.assertEqual(sh("git", "-C", str(src), "worktree", "list").count("\n"), 1)  # nothing left behind
        self.assertEqual(sh("git", "-C", str(src), "branch", "--list", "chore/*"), "")  # no local branch created

    def test_a_caller_far_behind_gets_the_hand_fix_note(self) -> None:
        git_repo(self.root, "mcp-athena", "jobs:\n  s:\n" + USES.format(OLD, "v1.1.1"))
        calls: list = []
        with mock.patch.object(ship, "gh", side_effect=self.fake_gh(calls=calls)):
            ship.open_pin_move("mcp-athena", "v1.8.2", NEW)
        body = calls[-1][calls[-1].index("--body") + 1]
        self.assertIn("More than one minor version behind", body)

    def stale_branch(self, src: pathlib.Path) -> str:
        """An earlier run's chore/pin-github-v1.8.2 on the origin (pushed from a detached HEAD), not on the base."""
        sh("git", "-C", str(src), "switch", "-q", "--detach")
        (src / "stale.txt").write_text("an earlier attempt")
        sh("git", "-C", str(src), "add", "stale.txt")
        sh("git", "-C", str(src), "commit", "-q", "-m", "earlier attempt")
        sh("git", "-C", str(src), "push", "-q", "origin", "HEAD:refs/heads/chore/pin-github-v1.8.2")
        sh("git", "-C", str(src), "switch", "-q", "dev")
        return sh("git", "-C", str(src), "rev-parse", "HEAD@{1}").strip()

    def test_an_existing_pull_request_for_the_tag_is_updated_never_a_second_one(self) -> None:
        src = git_repo(self.root, "onepeek-web", "jobs:\n  s:\n" + USES.format(OLD, "v1.7.0"))
        self.stale_branch(src)
        calls: list = []
        pr = [{"number": 9, "url": "https://github.com/OnePeek/onepeek-web/pull/9"}]
        with mock.patch.object(ship, "gh", side_effect=self.fake_gh(pr, calls)):
            self.assertEqual(ship.open_pin_move("onepeek-web", "v1.8.2", NEW), pr[0]["url"])
        self.assertEqual([c[:2] for c in calls], [("pr", "list")])  # no pr create
        self.assertIn(f"@{NEW} # v1.8.2", self.origin_show("onepeek-web", "chore/pin-github-v1.8.2:.github/workflows/ci.yml"))
        files = sh("git", "--git-dir", str(self.root / "origins/onepeek-web.git"), "ls-tree", "-r", "--name-only", "chore/pin-github-v1.8.2")
        self.assertIn("stale.txt", files)  # someone else's commit on the branch survives (no force)

    def test_a_rerun_keeps_a_commit_by_someone_else_and_adds_nothing_when_pinned(self) -> None:
        src = git_repo(self.root, "onepeek-web", "jobs:\n  s:\n" + USES.format(OLD, "v1.7.0"))
        self.stale_branch(src)
        bare = str(self.root / "origins/onepeek-web.git")
        with mock.patch.object(ship, "gh", side_effect=self.fake_gh([])):
            ship.open_pin_move("onepeek-web", "v1.8.2", NEW)
            first = sh("git", "--git-dir", bare, "rev-parse", "chore/pin-github-v1.8.2").strip()
            ship.open_pin_move("onepeek-web", "v1.8.2", NEW)
        self.assertEqual(sh("git", "--git-dir", bare, "rev-parse", "chore/pin-github-v1.8.2").strip(), first)  # nothing to push
        self.assertEqual(sh("git", "--git-dir", bare, "log", "--format=%s", "chore/pin-github-v1.8.2").splitlines()[1:3],
                         ["earlier attempt", "init"])

    def test_an_existing_branch_without_a_pull_request_is_updated_and_gets_one(self) -> None:  # a re-run
        src = git_repo(self.root, "onepeek-web", "jobs:\n  s:\n" + USES.format(OLD, "v1.7.0"))
        self.stale_branch(src)
        calls: list = []
        with mock.patch.object(ship, "gh", side_effect=self.fake_gh([], calls)):
            ship.open_pin_move("onepeek-web", "v1.8.2", NEW)
        self.assertEqual([c[:2] for c in calls], [("pr", "list"), ("pr", "create")])
        self.assertIn(f"@{NEW} # v1.8.2", self.origin_show("onepeek-web", "chore/pin-github-v1.8.2:.github/workflows/ci.yml"))

    def test_a_branch_that_moved_since_it_was_read_is_not_overwritten(self) -> None:  # the lease
        src = git_repo(self.root, "onepeek-web", "jobs:\n  s:\n" + USES.format(OLD, "v1.7.0"))
        self.stale_branch(src)
        real_git, moved = ship.git, []

        def git(cwd, *args):
            if args[:1] == ("push",) and not moved:  # someone pushes to the branch between the read and our push
                other = self.root / "other"
                sh("git", "clone", "-q", "-b", "chore/pin-github-v1.8.2", str(self.root / "origins/onepeek-web.git"), str(other))
                sh("git", "-C", str(other), "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q",
                   "--allow-empty", "-m", "someone else")
                sh("git", "-C", str(other), "push", "-q", "origin", "HEAD")
                moved.append(sh("git", "-C", str(other), "rev-parse", "HEAD").strip())
            return real_git(cwd, *args)
        with mock.patch.object(ship, "gh", side_effect=self.fake_gh([])), mock.patch.object(ship, "git", side_effect=git), \
                self.assertRaises(SystemExit):
            ship.open_pin_move("onepeek-web", "v1.8.2", NEW)
        self.assertEqual(self.origin_show("onepeek-web", "chore/pin-github-v1.8.2").split()[1], moved[0])

    def test_a_local_branch_with_an_unpushed_commit_is_untouched(self) -> None:
        src = git_repo(self.root, "onepeek-web", "jobs:\n  s:\n" + USES.format(OLD, "v1.7.0"))
        sh("git", "-C", str(src), "switch", "-q", "-c", "chore/pin-github-v1.8.2")
        (src / "mine.txt").write_text("local work")
        sh("git", "-C", str(src), "add", "mine.txt")
        sh("git", "-C", str(src), "commit", "-q", "-m", "local only")
        mine = sh("git", "-C", str(src), "rev-parse", "HEAD").strip()
        sh("git", "-C", str(src), "switch", "-q", "dev")
        with mock.patch.object(ship, "gh", side_effect=self.fake_gh([])):
            ship.open_pin_move("onepeek-web", "v1.8.2", NEW)
        self.assertEqual(sh("git", "-C", str(src), "rev-parse", "refs/heads/chore/pin-github-v1.8.2").strip(), mine)
        self.assertNotIn("mine.txt", sh("git", "--git-dir", str(self.root / "origins/onepeek-web.git"), "ls-tree", "-r",
                                        "--name-only", "chore/pin-github-v1.8.2"))

    def test_a_failure_in_one_caller_does_not_stop_the_others(self) -> None:
        done_for = []

        def open_pin_move(repo, tag, sha):
            if repo == "mcp-athena":
                raise SystemExit("STOP: `git -C x push ...` failed: denied")
            done_for.append(repo)
        with mock.patch.object(ship, "callers", return_value=["mcp-athena", "onepeek-web", "plugin-owl"]), \
                mock.patch.object(ship, "open_pin_move", side_effect=open_pin_move):
            failures = ship.pin_moves(set(), "v1.8.2", NEW)
        self.assertEqual(done_for, ["onepeek-web", "plugin-owl"])
        self.assertEqual(len(failures), 1)
        self.assertIn("mcp-athena", failures[0])

    def test_a_caller_already_at_the_tag_gets_no_pull_request(self) -> None:
        git_repo(self.root, "onepeek-infra", "jobs:\n  s:\n" + USES.format(NEW, "v1.8.2"))
        with mock.patch.object(ship, "gh") as gh:
            self.assertIsNone(ship.open_pin_move("onepeek-infra", "v1.8.2", NEW))
        gh.assert_not_called()


class WorktreeCleanup(unittest.TestCase):
    def setUp(self) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        patcher = mock.patch.object(ship, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.src = git_repo(self.root, "onepeek-web", "jobs: {}\n")
        sh("git", "-C", str(self.src), "branch", "feat/x")
        sh("git", "-C", str(self.src), "branch", "feat/other")
        self.scratch = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.scratch, True)

    def add(self, name: str, branch: str) -> pathlib.Path:
        wt = self.scratch / name
        sh("git", "-C", str(self.src), "worktree", "add", "-q", str(wt), branch)
        return wt

    def test_clean_worktrees_of_the_merged_branch_are_removed_and_others_kept(self) -> None:
        merged, other = self.add("merged", "feat/x"), self.add("other", "feat/other")
        ship.remove_worktrees("onepeek-web", "feat/x")
        self.assertFalse(merged.exists())
        self.assertTrue(other.exists())
        self.assertTrue((self.src / ".git").exists())  # the main checkout is never a candidate

    def test_a_dirty_worktree_is_kept(self) -> None:
        wt = self.add("dirty", "feat/x")
        (wt / "wip.txt").write_text("not committed")
        ship.remove_worktrees("onepeek-web", "feat/x")
        self.assertTrue((wt / "wip.txt").exists())

    def test_the_main_checkout_on_that_branch_is_never_removed(self) -> None:
        sh("git", "-C", str(self.src), "switch", "-q", "feat/x")
        self.assertEqual(ship.worktrees_of(self.src, "feat/x"), [])


class DeclaredDependencies(unittest.TestCase):
    """§8a P1 (lesson L26): listed pull requests merge first; the dependent's failed checks are re-run after."""

    def setUp(self) -> None:
        patcher = mock.patch.object(ship, "docs_pin_problems", return_value=[])  # no network in these tests
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_dependency_moves_ahead_and_the_order_is_otherwise_kept(self) -> None:
        prs = [("onepeek-docs", 1), ("onepeek-infra", 3), ("backend-service", 2), ("onepeek-web", 4)]
        deps = {("onepeek-infra", 3): [("backend-service", 2)]}
        self.assertEqual(ship.with_dependencies(prs, deps),
                         [("onepeek-docs", 1), ("backend-service", 2), ("onepeek-infra", 3), ("onepeek-web", 4)])
        self.assertEqual(ship.with_dependencies(prs, {}), prs)

    def test_onepeek_docs_stays_first_and_may_not_depend_on_the_set(self) -> None:  # §8a P2
        prs = [("onepeek-docs", 1), (".github", 2), ("backend-service", 3)]
        deps = {(".github", 2): [("backend-service", 3)]}
        self.assertEqual(ship.with_dependencies(prs, deps), [("onepeek-docs", 1), ("backend-service", 3), (".github", 2)])
        with self.assertRaises(SystemExit):
            ship.with_dependencies(prs, {("onepeek-docs", 1): [("backend-service", 3)]})
        self.assertEqual(ship.with_dependencies(prs, {("onepeek-docs", 1): [("onepeek-web", 9)]})[0], ("onepeek-docs", 1))

    def test_a_cycle_stops(self) -> None:
        prs = [("a", 1), ("b", 2)]
        with self.assertRaises(SystemExit):
            ship.with_dependencies(prs, {("a", 1): [("b", 2)], ("b", 2): [("a", 1)]})

    def info(self, bodies: dict, outside: dict | None = None):
        def pr_info(r, n):
            if (r, n) in bodies:
                return {"state": "OPEN", "baseRefName": ship.BASE.get(r, "dev"), "isDraft": False, "mergeable": "MERGEABLE",
                        "headRefName": f"b{n}", "headRefOid": "c" * 40, "body": bodies[(r, n)], "mergeCommit": None}
            return (outside or {})[(r, n)]
        return pr_info

    def test_a_dependency_outside_the_set_must_be_merged(self) -> None:
        bodies = {("onepeek-infra", 3): "Depends on: OnePeek/backend-service#73"}
        with mock.patch.object(ship, "pr_info", side_effect=self.info(bodies, {("backend-service", 73): {"state": "OPEN"}})):
            self.assertEqual(ship.main(["onepeek-infra#3"]), 1)
        with mock.patch.object(ship, "pr_info", side_effect=self.info(bodies, {("backend-service", 73): {"state": "MERGED"}})):
            self.assertEqual(ship.main(["onepeek-infra#3"]), 0)

    def test_go_merges_the_dependency_first_then_re_runs_the_dependent(self) -> None:
        bodies = {("onepeek-infra", 3): "x\nDepends on: OnePeek/backend-service#2\n", ("backend-service", 2): ""}
        merged, events = set(), []
        base = self.info(bodies)

        def pr_info(r, n):
            i = dict(base(r, n))
            if (r, n) in merged:
                i.update(state="MERGED", mergeCommit={"oid": "d" * 40})
            return i

        def gh(*args):
            if args[:2] == ("pr", "merge"):
                repo = args[4].split("/")[1]
                merged.add((repo, int(args[2])))
                events.append(("merge", repo))
            return ""
        with mock.patch.object(ship, "pr_info", side_effect=pr_info), mock.patch.object(ship, "gh", side_effect=gh), \
                mock.patch.object(ship, "wait_green"), mock.patch.object(ship, "remove_worktrees"), \
                mock.patch.object(ship, "rerun_failed", side_effect=lambda r, n: events.append(("rerun", r))):
            self.assertEqual(ship.main(["onepeek-infra#3", "backend-service#2", "--go"]), 0)
        self.assertEqual(events, [("merge", "backend-service"), ("rerun", "onepeek-infra"), ("merge", "onepeek-infra")])

    def test_pin_moves_go_to_every_caller_whose_pins_were_not_moved_inside_the_set(self) -> None:  # §8a P2
        root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        for r in ("onepeek-docs", ".github", "backend-service"):
            (root / r / ".git").mkdir(parents=True)
        bodies = {("onepeek-docs", 1): "", (".github", 2): "", ("backend-service", 3): ""}
        merged, base = set(), self.info(bodies)

        def pr_info(r, n):
            i = dict(base(r, n))
            if (r, n) in merged:
                i.update(state="MERGED", mergeCommit={"oid": "d" * 40})
            return i

        def gh(*args):
            if args[:2] == ("pr", "merge"):
                merged.add((args[4].split("/")[1], int(args[2])))
            if args[0] == "api" and "/commits" in args[2]:
                return "c" * 40 + "\n"
            return ""
        moved_to = []
        with mock.patch.object(ship, "ROOT", root), mock.patch.object(ship, "pr_info", side_effect=pr_info), \
                mock.patch.object(ship, "gh", side_effect=gh), mock.patch.object(ship, "wait_green"), \
                mock.patch.object(ship, "remove_worktrees"), mock.patch.object(ship, "wait_tag", return_value=("v1.8.2", NEW)), \
                mock.patch.object(ship, "bump_pins", return_value=None), mock.patch.object(ship, "rerun_failed"), \
                mock.patch.object(ship, "move_docs_pin", return_value=(None, True)), mock.patch.object(ship, "tested_bases", return_value=set()), \
                mock.patch.object(ship.subprocess, "run", return_value=done(0, "onepeek-docs@" + "c" * 40)), \
                mock.patch.object(ship, "callers", return_value=["backend-service", "onepeek-docs", "onepeek-web"]), \
                mock.patch.object(ship, "open_pin_move", side_effect=lambda r, t, s: moved_to.append(r)):
            self.assertEqual(ship.main(["onepeek-docs#1", ".github#2", "backend-service#3", "--go"]), 0)
        self.assertEqual(moved_to, ["onepeek-docs", "onepeek-web"])  # docs merged before .github; web is outside the set

    def test_rerun_failed_re_runs_only_failed_runs(self) -> None:
        cs = [{"name": "depends-on", "bucket": "fail", "link": "https://github.com/OnePeek/x/actions/runs/11/job/1"},
              {"name": "ok", "bucket": "pass", "link": "https://github.com/OnePeek/x/actions/runs/12/job/2"}]
        with mock.patch.object(ship, "read_checks", return_value=cs), mock.patch.object(ship, "gh") as gh, \
                mock.patch.object(ship.time, "sleep"):
            ship.rerun_failed("x", 1)
        gh.assert_called_once_with("run", "rerun", "11", "-R", "OnePeek/x", "--failed")


DOCS_HEAD, DOCS_OLDER, DOCS_MERGE = "1" * 40, "2" * 40, "3" * 40


class DocsPin(unittest.TestCase):
    """Lesson L27 (b, d): a code pin may be any commit of the set's docs pull request; after the docs merge the code
    pull request's checks are re-run, then its pin moves to the docs merge commit and it merges without a new CI run."""

    def setUp(self) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        for r in ("onepeek-docs", "backend-service", "onepeek-web"):
            (self.root / r / ".git").mkdir(parents=True)
        patcher = mock.patch.object(ship, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def harness(self, pins: dict, bodies: dict | None = None):
        """Fake gh/pr_info for a set of onepeek-docs#1 and code pull requests whose docs-version.txt is `pins`."""
        self.merged, self.events, self.heads = set(), [], {}
        prs = [("onepeek-docs", 1), *pins]
        bodies = bodies or {}

        def pr_info(r, n):
            i = {"state": "OPEN", "baseRefName": ship.BASE.get(r, "dev"), "isDraft": False, "mergeable": "MERGEABLE",
                 "headRefName": f"b{n}",
                 "headRefOid": self.heads.get(r) or (DOCS_HEAD if r == "onepeek-docs" else f"{n}" * 40),
                 "body": bodies.get((r, n), ""), "mergeCommit": None}
            if (r, n) in self.merged:
                i.update(state="MERGED", mergeCommit={"oid": DOCS_MERGE if r == "onepeek-docs" else "e" * 40})
            return i

        def gh(*args):
            if args[:2] == ("pr", "merge"):
                repo = args[4].split("/")[1]
                self.merged.add((repo, int(args[2])))
                self.events.append(("merge", repo, args[args.index("--match-head-commit") + 1]))
            if args[0] == "api" and args[2].startswith("repos/OnePeek/onepeek-docs/pulls/1/commits"):
                return f"{DOCS_OLDER}\n{DOCS_HEAD}\n"
            return ""

        def run(cmd, **kw):
            repo = cmd[2].split("/")[2]
            return done(0, f"# pin\nonepeek-docs@{pins[next(p for p in pins if p[0] == repo)]}\n")
        return prs, pr_info, gh, run

    def go(self, pins: dict, only_pin: bool = True, bodies: dict | None = None, go: bool = True,
           base_moved: bool = False) -> int:
        prs, pr_info, gh, run = self.harness(pins, bodies)

        def move(repo, branch, green, merge_sha):
            self.events.append(("pin", repo, green, merge_sha))
            self.heads[repo] = "f" * 40  # the pushed pin commit is the new head
            return "f" * 40, only_pin
        with mock.patch.object(ship, "pr_info", side_effect=pr_info), mock.patch.object(ship, "gh", side_effect=gh), \
                mock.patch.object(ship.subprocess, "run", side_effect=run), mock.patch.object(ship, "remove_worktrees"), \
                mock.patch.object(ship, "wait_green", side_effect=lambda r, n, m: self.events.append(("green", r))), \
                mock.patch.object(ship, "tested_bases", return_value={"b" * 40}), \
                mock.patch.object(ship, "base_head", return_value="c" * 40 if base_moved else "b" * 40), \
                mock.patch.object(ship, "wait_head"), mock.patch.object(ship, "move_docs_pin", side_effect=move), \
                mock.patch.object(ship, "rerun_failed", side_effect=lambda r, n: self.events.append(("rerun", r))):
            return ship.main([f"{r}#{n}" for r, n in prs] + (["--go"] if go else []))

    def test_any_commit_of_the_docs_pull_request_is_accepted(self) -> None:
        self.assertEqual(self.go({("backend-service", 2): DOCS_OLDER, ("onepeek-web", 4): DOCS_HEAD}, go=False), 0)

    def test_a_pin_round_docs_pull_request_does_not_check_code_pins(self) -> None:
        with mock.patch.object(ship, "pr_files", return_value=[".github/workflows/checks.yml"]):
            self.assertEqual(self.go({("backend-service", 2): "9" * 40}, go=False), 0)
        with mock.patch.object(ship, "pr_files", return_value=[".github/workflows/checks.yml", "standards/x.md"]):
            self.assertEqual(self.go({("backend-service", 2): "9" * 40}, go=False), 1)

    def test_a_pin_outside_the_docs_pull_request_is_a_problem(self) -> None:
        self.assertEqual(self.go({("backend-service", 2): "9" * 40}, go=False), 1)

    def test_after_the_docs_merge_checks_rerun_then_the_pin_moves_and_it_merges_at_that_commit(self) -> None:
        self.assertEqual(self.go({("backend-service", 2): DOCS_OLDER}), 0)
        self.assertEqual(self.events, [
            ("green", "onepeek-docs"), ("merge", "onepeek-docs", DOCS_HEAD),
            ("rerun", "backend-service"),  # implicit dependency: its depends-on failed while docs was open
            ("green", "backend-service"),
            ("pin", "backend-service", "2" * 40, DOCS_MERGE),
            ("merge", "backend-service", "f" * 40),  # no new CI wait: only the pin line changed
        ])

    def test_more_than_the_pin_changed_waits_for_ci(self) -> None:
        self.assertEqual(self.go({("backend-service", 2): DOCS_OLDER}, only_pin=False), 0)
        self.assertEqual(self.events[-3:], [("pin", "backend-service", "2" * 40, DOCS_MERGE),
                                            ("green", "backend-service"), ("merge", "backend-service", "f" * 40)])

    def test_a_base_that_moved_since_the_green_run_waits_for_the_new_run(self) -> None:
        self.assertEqual(self.go({("backend-service", 2): DOCS_OLDER}, base_moved=True), 0)
        self.assertEqual(self.events[-3:], [("pin", "backend-service", "2" * 40, DOCS_MERGE),
                                            ("green", "backend-service"), ("merge", "backend-service", "f" * 40)])

    def test_explicit_and_implicit_dependents_are_both_re_run(self) -> None:
        bodies = {("onepeek-web", 4): "Depends on: OnePeek/backend-service#2"}
        self.assertEqual(self.go({("backend-service", 2): DOCS_HEAD, ("onepeek-web", 4): DOCS_HEAD}, bodies=bodies), 0)
        reruns = [e for e in self.events if e[0] in ("rerun", "merge")]
        self.assertEqual(reruns, [("merge", "onepeek-docs", DOCS_HEAD), ("rerun", "backend-service"), ("rerun", "onepeek-web"),
                                  ("merge", "backend-service", "f" * 40), ("rerun", "onepeek-web"),
                                  ("merge", "onepeek-web", "f" * 40)])


class MoveDocsPin(unittest.TestCase):
    """The pin commit on a real git branch (local bare origin)."""

    def setUp(self) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        patcher = mock.patch.object(ship, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.src = git_repo(self.root, "backend-service", "jobs: {}\n")
        sh("git", "-C", str(self.src), "switch", "-q", "-c", "feat/x")
        (self.src / "docs-version.txt").write_text(f"# docs\nonepeek-docs@{DOCS_OLDER}\n")
        sh("git", "-C", str(self.src), "add", ".")
        sh("git", "-C", str(self.src), "commit", "-q", "-m", "pin")
        sh("git", "-C", str(self.src), "push", "-q", "origin", "feat/x")
        self.green = sh("git", "-C", str(self.src), "rev-parse", "HEAD").strip()

    def origin(self, *args: str) -> str:
        return sh("git", "--git-dir", str(self.root / "origins/backend-service.git"), *args)

    def test_one_commit_moves_only_the_pin_line_on_top_of_the_green_head(self) -> None:
        new, only = ship.move_docs_pin("backend-service", "feat/x", self.green, DOCS_MERGE)
        self.assertTrue(only)
        self.assertEqual(self.origin("rev-parse", "feat/x").strip(), new)
        self.assertEqual(self.origin("rev-parse", "feat/x~1").strip(), self.green)
        self.assertEqual(self.origin("show", "feat/x:docs-version.txt"), f"# docs\nonepeek-docs@{DOCS_MERGE}\n")
        self.assertEqual(sh("git", "-C", str(self.src), "worktree", "list").count("\n"), 1)  # nothing left behind

    def test_a_pin_already_at_the_merge_commit_pushes_nothing(self) -> None:
        self.assertEqual(ship.move_docs_pin("backend-service", "feat/x", self.green, DOCS_OLDER), (None, True))
        self.assertEqual(self.origin("rev-parse", "feat/x").strip(), self.green)

    def test_a_branch_that_moved_since_the_green_run_is_not_overwritten(self) -> None:
        (self.src / "other.txt").write_text("x")
        sh("git", "-C", str(self.src), "add", ".")
        sh("git", "-C", str(self.src), "commit", "-q", "-m", "later")
        sh("git", "-C", str(self.src), "push", "-q", "origin", "feat/x")
        later = self.origin("rev-parse", "feat/x").strip()
        with self.assertRaises(SystemExit):
            ship.move_docs_pin("backend-service", "feat/x", self.green, DOCS_MERGE)
        self.assertEqual(self.origin("rev-parse", "feat/x").strip(), later)

    def test_a_comment_line_holding_the_old_pin_is_left_alone(self) -> None:
        (self.src / "docs-version.txt").write_text(f"# was onepeek-docs@{DOCS_OLDER} before\nonepeek-docs@{DOCS_OLDER}\n")
        sh("git", "-C", str(self.src), "commit", "-q", "-am", "comment")
        sh("git", "-C", str(self.src), "push", "-q", "origin", "feat/x")
        green = sh("git", "-C", str(self.src), "rev-parse", "HEAD").strip()
        ship.move_docs_pin("backend-service", "feat/x", green, DOCS_MERGE)
        self.assertEqual(self.origin("show", "feat/x:docs-version.txt"),
                         f"# was onepeek-docs@{DOCS_OLDER} before\nonepeek-docs@{DOCS_MERGE}\n")

    def test_base_unchanged_compares_the_tested_base_with_the_base_head_now(self) -> None:
        with mock.patch.object(ship, "base_head", return_value="b" * 40):
            self.assertTrue(ship.base_unchanged("backend-service", 2, {"b" * 40}, "dev"))
            self.assertFalse(ship.base_unchanged("backend-service", 2, {"a" * 40}, "dev"))
            self.assertFalse(ship.base_unchanged("backend-service", 2, set(), "dev"))  # unknown: wait

    def test_tested_bases_reads_each_runs_base_and_is_empty_when_unknown(self) -> None:
        chk = [{"name": "x", "bucket": "pass", "link": "https://github.com/OnePeek/r/actions/runs/9/job/1"}]
        with mock.patch.object(ship, "read_checks", return_value=chk), \
                mock.patch.object(ship, "gh", return_value='[{"number": 2, "base": {"sha": "%s"}}]' % ("b" * 40)):
            self.assertEqual(ship.tested_bases("backend-service", 2), {"b" * 40})
        with mock.patch.object(ship, "read_checks", return_value=chk), mock.patch.object(ship, "gh", return_value="[]"):
            self.assertEqual(ship.tested_bases("backend-service", 2), set())

    def test_tested_bases_ignores_checks_without_an_actions_run_and_needs_one_run(self) -> None:
        # CodeRabbit and other apps link outside Actions; they must not make the base "unknown" (review of #35).
        run = {"name": "x", "bucket": "pass", "link": "https://github.com/OnePeek/r/actions/runs/9/job/1"}
        bot = {"name": "CodeRabbit", "bucket": "pass", "link": "https://coderabbit.ai?utm_source=x"}
        with mock.patch.object(ship, "read_checks", return_value=[bot, run]), \
                mock.patch.object(ship, "gh", return_value='[{"number": 2, "base": {"sha": "%s"}}]' % ("b" * 40)):
            self.assertEqual(ship.tested_bases("backend-service", 2), {"b" * 40})
        with mock.patch.object(ship, "read_checks", return_value=[bot]), mock.patch.object(ship, "gh", return_value="[]"):
            self.assertEqual(ship.tested_bases("backend-service", 2), set())  # no Actions run at all: unknown

    def test_only_pin_changed_is_false_for_any_other_change(self) -> None:
        wt = self.src
        (wt / "docs-version.txt").write_text(f"# docs\nonepeek-docs@{DOCS_MERGE}\n")
        sh("git", "-C", str(wt), "commit", "-q", "-am", "pin")
        pin_only = sh("git", "-C", str(wt), "rev-parse", "HEAD").strip()
        self.assertTrue(ship.only_pin_changed(wt, self.green, pin_only))
        (wt / "other.txt").write_text("x")
        sh("git", "-C", str(wt), "add", ".")
        sh("git", "-C", str(wt), "commit", "-q", "-m", "more")
        self.assertFalse(ship.only_pin_changed(wt, self.green, sh("git", "-C", str(wt), "rev-parse", "HEAD").strip()))
        (wt / "docs-version.txt").write_text(f"# docs changed\nonepeek-docs@{DOCS_MERGE}\n")
        sh("git", "-C", str(wt), "commit", "-q", "-am", "comment")
        self.assertFalse(ship.only_pin_changed(wt, pin_only, sh("git", "-C", str(wt), "rev-parse", "HEAD").strip()))


class DocsPinStrict(unittest.TestCase):
    """The docs wait is strict here: with no docs pull request in the set, a pin not on docs main stops the run."""

    def pins(self, status: str, prs: list[int]) -> list[str]:
        with mock.patch.object(ship.subprocess, "run", side_effect=lambda cmd, **kw: done(0, f"onepeek-docs@{DOCS_HEAD}\n"
                                                                                       if cmd[0] == "gh" and cmd[1] == "api"
                                                                                       else "tok\n")), \
                mock.patch.object(ship.check_pins, "compare_status", return_value=status), \
                mock.patch.object(ship.depends_on, "open_docs_prs", return_value=prs):
            return ship.docs_pin_problems("backend-service", 2, "feat/x")

    def test_a_pin_on_main_passes(self) -> None:
        self.assertEqual(self.pins("ahead", []), [])

    def test_a_pin_in_an_open_docs_pull_request_not_in_the_set_fails(self) -> None:
        out = self.pins("diverged", [145])
        self.assertEqual(len(out), 1)
        self.assertIn("waiting for onepeek-docs#145", out[0])

    def test_a_pin_in_no_pull_request_fails(self) -> None:
        self.assertIn("in no open docs PR", self.pins("diverged", [])[0])


def commit_all(repo: pathlib.Path, msg: str) -> None:
    sh("git", "-C", str(repo), "add", ".")
    sh("git", "-C", str(repo), "commit", "-q", "-m", msg)


class RefreshDocsPin(unittest.TestCase):
    """A code pull request whose only conflict with dev is docs-version.txt (temporary git repo, local bare origin)."""

    def setUp(self) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        patcher = mock.patch.object(ship, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.src = git_repo(self.root, "backend-service", "jobs: {}\n")
        (self.src / "docs-version.txt").write_text(f"# docs\nonepeek-docs@{DOCS_OLDER}\n")
        commit_all(self.src, "pin")
        sh("git", "-C", str(self.src), "push", "-q", "origin", "dev")
        sh("git", "-C", str(self.src), "switch", "-q", "-c", "feat/x")
        (self.src / "docs-version.txt").write_text(f"# docs\nonepeek-docs@{DOCS_HEAD}\n")
        commit_all(self.src, "my pin")
        sh("git", "-C", str(self.src), "push", "-q", "origin", "feat/x")
        sh("git", "-C", str(self.src), "switch", "-q", "dev")

    def origin(self, *args: str) -> str:
        return sh("git", "--git-dir", str(self.root / "origins/backend-service.git"), *args)

    def advance_dev(self, files: dict[str, str]) -> None:
        for name, text in files.items():
            (self.src / name).write_text(text)
        commit_all(self.src, "another change set merged")
        sh("git", "-C", str(self.src), "push", "-q", "origin", "dev")

    def test_only_the_pin_conflicting_is_merged_and_pinned_to_the_docs_head(self) -> None:
        self.advance_dev({"docs-version.txt": f"# docs\nonepeek-docs@{DOCS_MERGE}\n", "new.txt": "x"})
        new = ship.refresh_docs_pin("backend-service", "feat/x", "dev", "onepeek-docs#7", "4" * 40)
        self.assertEqual(self.origin("rev-parse", "feat/x").strip(), new)
        self.assertEqual(self.origin("show", "feat/x:docs-version.txt"), f"# docs\nonepeek-docs@{'4' * 40}\n")
        self.assertEqual(self.origin("show", "feat/x:new.txt"), "x")  # the base's other change came in
        self.assertEqual(len(self.origin("log", "-1", "--format=%P", "feat/x").split()), 2)  # a merge commit
        self.assertEqual(self.origin("log", "-1", "--format=%s", "feat/x").strip(),
                         "Merge dev; docs-version to onepeek-docs#7 head")
        self.assertEqual(sh("git", "-C", str(self.src), "worktree", "list").count("\n"), 1)  # nothing left behind

    def test_another_conflicting_file_changes_nothing_and_returns_none(self) -> None:
        sh("git", "-C", str(self.src), "switch", "-q", "feat/x")
        (self.src / "app.txt").write_text("mine\n")
        commit_all(self.src, "mine")
        sh("git", "-C", str(self.src), "push", "-q", "origin", "feat/x")
        sh("git", "-C", str(self.src), "switch", "-q", "dev")
        self.advance_dev({"docs-version.txt": f"# docs\nonepeek-docs@{DOCS_MERGE}\n", "app.txt": "theirs\n"})
        before = self.origin("rev-parse", "feat/x").strip()
        self.assertIsNone(ship.refresh_docs_pin("backend-service", "feat/x", "dev", "onepeek-docs#7", "4" * 40))
        self.assertEqual(self.origin("rev-parse", "feat/x").strip(), before)
        self.assertEqual(sh("git", "-C", str(self.src), "worktree", "list").count("\n"), 1)

    def test_a_dry_run_pushes_nothing(self) -> None:
        self.advance_dev({"docs-version.txt": f"# docs\nonepeek-docs@{DOCS_MERGE}\n"})
        before = self.origin("rev-parse", "feat/x").strip()
        self.assertEqual(ship.refresh_docs_pin("backend-service", "feat/x", "dev", "onepeek-docs#7", "4" * 40, push=False), "")
        self.assertEqual(self.origin("rev-parse", "feat/x").strip(), before)

    def test_nothing_to_do_when_up_to_date_with_the_pin_already_set(self) -> None:
        self.assertEqual(ship.refresh_docs_pin("backend-service", "feat/x", "dev", "onepeek-docs#7", DOCS_HEAD), "")

    def test_a_conflicting_pull_request_is_refreshed_then_waited_on(self) -> None:
        states = iter(["CONFLICTING", "MERGEABLE"])
        with mock.patch.object(ship, "mergeable", side_effect=lambda r, n: next(states)), \
                mock.patch.object(ship, "wait_head") as wh:
            ship.no_conflicts([("backend-service", 2)], lambda r, n: "f" * 40)
        wh.assert_called_once_with("backend-service", 2, "f" * 40)

    def test_a_conflict_the_refresh_cannot_fix_still_stops(self) -> None:
        with mock.patch.object(ship, "mergeable", return_value="CONFLICTING"), mock.patch.object(ship, "wait_head"):
            with self.assertRaises(SystemExit) as stop:
                ship.no_conflicts([("backend-service", 2)], lambda r, n: None)
        self.assertIn("not mergeable", str(stop.exception))

    INFO = {"state": "OPEN", "headRefName": "feat/x", "baseRefName": "dev", "headRefOid": "4" * 40,
            "isCrossRepository": False, "headRepositoryOwner": {"login": "OnePeek"}}

    def refresh(self, info=None, state="CONFLICTING", found=(7,), result="f" * 40):
        with mock.patch.object(ship, "pr_info", return_value={**self.INFO, **(info or {})}), \
                mock.patch.object(ship, "mergeable", return_value=state), \
                mock.patch.object(ship, "branch_pin", return_value=DOCS_HEAD), \
                mock.patch.object(ship.subprocess, "run", side_effect=lambda cmd, **kw: done(0, "tok\n")), \
                mock.patch.object(ship.depends_on, "open_docs_prs", return_value=list(found)), \
                mock.patch.object(ship, "refresh_docs_pin", return_value=result) as rf:
            try:
                rc = ship.main(["--refresh", "backend-service#2"])
            except SystemExit as e:
                return e, rf
            return rc, rf

    def test_refresh_flag_finds_the_docs_pull_request_from_the_pin_and_refreshes(self) -> None:
        rc, rf = self.refresh()
        self.assertEqual(rc, 0)
        rf.assert_called_once_with("backend-service", "feat/x", "dev", "onepeek-docs#7", "4" * 40)

    def test_refresh_flag_stops_when_another_file_conflicts(self) -> None:
        rc, _ = self.refresh(result=None)
        self.assertIsInstance(rc, SystemExit)

    def test_refresh_flag_stops_when_no_open_docs_pull_request_holds_the_pin(self) -> None:
        rc, rf = self.refresh(found=())
        self.assertIn("no open onepeek-docs pull request", str(rc))
        rf.assert_not_called()

    def test_refresh_flag_stops_when_several_open_docs_pull_requests_hold_the_pin(self) -> None:
        rc, rf = self.refresh(found=(7, 9))
        self.assertIn("2 open onepeek-docs pull requests", str(rc))
        rf.assert_not_called()

    def test_refresh_flag_needs_an_open_conflicting_pull_request_of_this_repository(self) -> None:
        for kwargs in ({"info": {"state": "MERGED"}}, {"state": "MERGEABLE"}, {"info": {"isCrossRepository": True}},
                       {"info": {"headRepositoryOwner": {"login": "someone"}}},
                       {"info": {"headRefName": "dev"}}, {"info": {"headRefName": "main", "baseRefName": "main"}}):
            rc, rf = self.refresh(**kwargs)
            self.assertIsInstance(rc, SystemExit, kwargs)
            rf.assert_not_called()

    def test_a_branch_that_is_a_base_branch_is_never_pushed_to(self) -> None:  # a fork's `dev` would be the base `dev`
        before = self.origin("rev-parse", "dev").strip()
        self.advance_dev({"docs-version.txt": f"# docs\nonepeek-docs@{DOCS_MERGE}\n"})
        before = self.origin("rev-parse", "dev").strip()
        for fn, args in ((ship.refresh_docs_pin, ("backend-service", "dev", "dev", "onepeek-docs#7", "4" * 40)),
                         (ship.bump_pins, ("backend-service", "dev", "v1.0.0", "a" * 40)),
                         (ship.move_docs_pin, ("backend-service", "dev", before, DOCS_MERGE))):
            with self.assertRaises(SystemExit) as stop:
                fn(*args)
            self.assertIn("a base branch", str(stop.exception))
        self.assertEqual(self.origin("rev-parse", "dev").strip(), before)

    def test_a_clean_merge_leaves_docs_version_as_merged(self) -> None:
        self.advance_dev({"new.txt": "x"})
        new = ship.refresh_docs_pin("backend-service", "feat/x", "dev", "onepeek-docs#7", "4" * 40)
        self.assertTrue(new)
        self.assertEqual(self.origin("show", "feat/x:docs-version.txt"), f"# docs\nonepeek-docs@{DOCS_HEAD}\n")

    def test_a_clean_merge_of_docs_version_keeps_the_base_other_lines_and_is_not_overwritten(self) -> None:
        sh("git", "-C", str(self.src), "switch", "-q", "feat/x")
        (self.src / "docs-version.txt").write_text(f"# docs\n# a\n# b\n# c\nonepeek-docs@{DOCS_HEAD}\n")
        commit_all(self.src, "mine")
        sh("git", "-C", str(self.src), "push", "-q", "origin", "feat/x")
        sh("git", "-C", str(self.src), "switch", "-q", "dev")
        self.advance_dev({"docs-version.txt": f"# theirs\n# docs\nonepeek-docs@{DOCS_OLDER}\n"})
        ship.refresh_docs_pin("backend-service", "feat/x", "dev", "onepeek-docs#7", "4" * 40)
        text = self.origin("show", "feat/x:docs-version.txt")
        self.assertIn(f"onepeek-docs@{DOCS_HEAD}", text)  # not overwritten by the docs head
        self.assertIn("# theirs", text)


class ReadErrors(unittest.TestCase):
    def pins(self, rc: int, stderr: str) -> list[str]:
        with mock.patch.object(ship.subprocess, "run", return_value=done(rc, "", stderr)):
            return ship.docs_pin_problems("backend-service", 2, "feat/x")

    def test_only_a_404_means_no_pin(self) -> None:
        self.assertEqual(self.pins(1, "gh: Not Found (HTTP 404)"), [])
        for bad in ("gh: Bad gateway (HTTP 502)", "gh: Forbidden (HTTP 403)"):
            out = self.pins(1, bad)
            self.assertEqual(len(out), 1, bad)
            self.assertIn("cannot read docs-version.txt", out[0])


class ConflictFlow(DocsPin):
    """main() with conflicting pull requests: when the refresh may run, and what it must not touch."""

    def flow(self, pins: dict, conflicting=(), go: bool = True, drafts=(), cross=(), only_files=None,
             after_refresh: str = "MERGEABLE", loop_conflict=(), with_docs: bool = True):
        prs, pr_info, gh, run = self.harness(pins)
        if not with_docs:
            prs = prs[1:]
        self.refreshed, self.waited = [], []
        state = {c: "CONFLICTING" for c in loop_conflict}

        def info(r, n):
            i = pr_info(r, n)
            if (r, n) in conflicting:
                i["mergeable"] = "CONFLICTING"
            i["isDraft"] = (r, n) in drafts
            if (r, n) in cross:
                i["isCrossRepository"] = True
            return i

        def refresh(repo, branch, base, label, head, push=True):
            self.refreshed.append((repo, label, push))
            state[(repo, 2)] = after_refresh
            return "f" * 40 if push else ""

        def merge_state(r, n):
            return state.get((r, n), "MERGEABLE")
        out = []
        with mock.patch.object(ship, "pr_info", side_effect=info), mock.patch.object(ship, "gh", side_effect=gh), \
                mock.patch.object(ship.subprocess, "run", side_effect=run), mock.patch.object(ship, "remove_worktrees"), \
                mock.patch.object(ship, "wait_green"), mock.patch.object(ship, "tested_bases", return_value={"b" * 40}), \
                mock.patch.object(ship, "base_head", return_value="b" * 40), mock.patch.object(ship, "rerun_failed"), \
                mock.patch.object(ship, "wait_head", side_effect=lambda r, n, s: self.waited.append((r, n))), \
                mock.patch.object(ship, "move_docs_pin", return_value=("f" * 40, True)), \
                mock.patch.object(ship, "mergeable", side_effect=merge_state), \
                mock.patch.object(ship, "pr_files", return_value=only_files or ["standards/x.md"]), \
                mock.patch.object(ship, "refresh_docs_pin", side_effect=refresh), \
                mock.patch.object(ship, "say", side_effect=out.append):
            rc = ship.main([f"{r}#{n}" for r, n in prs] + (["--go"] if go else []))
        return rc, out

    PIN_OK = {("backend-service", 2): DOCS_OLDER}
    CONF = {("backend-service", 2)}

    def test_a_dry_run_only_says_it_would_refresh(self) -> None:  # kills push=a.go -> True
        rc, out = self.flow(self.PIN_OK, self.CONF, go=False)
        self.assertEqual(rc, 0)
        self.assertEqual(self.refreshed, [("backend-service", "onepeek-docs#1", False)])
        self.assertTrue(any(o.startswith("would refresh backend-service#2") for o in out), out)

    def test_go_refreshes_waits_for_the_head_and_reads_the_state_again(self) -> None:
        rc, _ = self.flow(self.PIN_OK, self.CONF)
        self.assertEqual(rc, 0)
        self.assertEqual(self.refreshed, [("backend-service", "onepeek-docs#1", True)])
        self.assertIn(("backend-service", 2), self.waited)

    def test_still_conflicting_after_the_refresh_stops(self) -> None:  # kills replacing the re-check by MERGEABLE
        rc, out = self.flow(self.PIN_OK, self.CONF, after_refresh="CONFLICTING")
        self.assertEqual(rc, 1)
        self.assertEqual(self.events, [])  # nothing merged
        self.assertTrue(any("after the refresh" in o for o in out), out)

    def test_no_refresh_while_any_other_problem_is_open(self) -> None:  # B1
        rc, _ = self.flow(self.PIN_OK, self.CONF, drafts={("backend-service", 2)})
        self.assertEqual(rc, 1)
        self.assertEqual(self.refreshed, [])

    def test_a_pin_that_is_not_a_commit_of_the_docs_pull_request_is_reported_not_repinned(self) -> None:  # B1
        rc, out = self.flow({("backend-service", 2): "9" * 40}, self.CONF)
        self.assertEqual(rc, 1)
        self.assertEqual(self.refreshed, [])
        self.assertTrue(any("not a commit of onepeek-docs#1" in o for o in out), out)

    def test_a_fork_pull_request_is_never_refreshed(self) -> None:  # B2
        rc, _ = self.flow(self.PIN_OK, self.CONF, cross=self.CONF)
        self.assertEqual(rc, 1)
        self.assertEqual(self.refreshed, [])

    def test_a_pin_round_is_not_refreshed(self) -> None:  # S5
        rc, _ = self.flow(self.PIN_OK, self.CONF, only_files=[".github/workflows/checks.yml"])
        self.assertEqual(rc, 1)
        self.assertEqual(self.refreshed, [])

    def test_the_docs_pull_request_itself_is_never_refreshed(self) -> None:  # kills dropping the guard
        rc, out = self.flow(self.PIN_OK, {("onepeek-docs", 1)})
        self.assertEqual(rc, 1)
        self.assertEqual(self.refreshed, [])
        self.assertTrue(any("resolve conflicts before any check is waited on" in o for o in out), out)

    def test_a_conflict_that_appears_in_the_merge_loop_is_refreshed(self) -> None:  # kills dropping resolve_pin there
        rc, _ = self.flow(self.PIN_OK, loop_conflict={("backend-service", 2)})
        self.assertEqual(rc, 0)
        self.assertEqual(self.refreshed, [("backend-service", "onepeek-docs#1", True)])
        self.assertIn(("backend-service", 2), self.waited)

    def test_the_merge_loop_never_refreshes_the_docs_pull_request(self) -> None:  # S2
        with self.assertRaises(SystemExit):
            self.flow(self.PIN_OK, loop_conflict={("onepeek-docs", 1)})
        self.assertEqual(self.refreshed, [])

    def test_with_no_docs_pull_request_the_strict_check_runs_unpatched(self) -> None:  # kills `if not docs` -> False
        prs, pr_info, gh, run = self.harness({("backend-service", 2): DOCS_OLDER})

        def sub(cmd, **kw):
            return done(0, "tok\n") if cmd[1] == "auth" else done(0, f"onepeek-docs@{DOCS_OLDER}\n")
        with mock.patch.object(ship, "pr_info", side_effect=pr_info), mock.patch.object(ship.subprocess, "run", side_effect=sub), \
                mock.patch.object(ship.check_pins, "compare_status", return_value="diverged"), \
                mock.patch.object(ship.depends_on, "open_docs_prs", return_value=[145]):
            self.assertEqual(ship.main(["backend-service#2"]), 1)  # a pin in an open docs pull request outside the set
        with mock.patch.object(ship, "pr_info", side_effect=pr_info), mock.patch.object(ship.subprocess, "run", side_effect=sub), \
                mock.patch.object(ship.check_pins, "compare_status", return_value="ahead"):
            self.assertEqual(ship.main(["backend-service#2"]), 0)  # on docs main: fine


if __name__ == "__main__":
    unittest.main()
