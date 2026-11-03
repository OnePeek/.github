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
        self.assertNotIn("stale.txt", files)  # rebuilt off the base

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
            return ""
        moved_to = []
        with mock.patch.object(ship, "ROOT", root), mock.patch.object(ship, "pr_info", side_effect=pr_info), \
                mock.patch.object(ship, "gh", side_effect=gh), mock.patch.object(ship, "wait_green"), \
                mock.patch.object(ship, "remove_worktrees"), mock.patch.object(ship, "wait_tag", return_value=("v1.8.2", NEW)), \
                mock.patch.object(ship, "bump_pins", return_value=None), \
                mock.patch.object(ship.subprocess, "run", return_value=done(0, "onepeek-docs@" + "c" * 40)), \
                mock.patch.object(ship, "callers", return_value=["backend-service", "onepeek-docs", "onepeek-web"]), \
                mock.patch.object(ship, "open_pin_move", side_effect=lambda r, t, s: moved_to.append(r)):
            self.assertEqual(ship.main(["onepeek-docs#1", ".github#2", "backend-service#3", "--go"]), 0)
        self.assertEqual(moved_to, ["onepeek-docs", "onepeek-web"])  # docs merged before .github; web is outside the set

    def test_rerun_failed_re_runs_only_failed_runs(self) -> None:
        cs = [{"name": "depends-on", "bucket": "fail", "link": "https://github.com/OnePeek/x/actions/runs/11/job/1"},
              {"name": "ok", "bucket": "pass", "link": "https://github.com/OnePeek/x/actions/runs/12/job/2"}]
        with mock.patch.object(ship, "read_checks", return_value=cs), mock.patch.object(ship, "gh") as gh:
            ship.rerun_failed("x", 1)
        gh.assert_called_once_with("run", "rerun", "11", "-R", "OnePeek/x", "--failed")


if __name__ == "__main__":
    unittest.main()
