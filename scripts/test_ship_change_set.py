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
        out = ship.PIN.sub(lambda m: f"{m.group(1)}@{new_sha} # v1.1.3", text)
        self.assertIn(f"contract.yml@{new_sha} # v1.1.3", out)
        self.assertIn(f"actions/checkout@{old_sha} # v7", out)



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
        git_repo(self.root, "marketing-website", "jobs: {}\n")
        self.assertEqual(ship.callers(), ["backend-service", "onepeek-docs"])

    def test_a_pin_move_pull_request_moves_every_pin_on_a_new_branch(self) -> None:
        src = git_repo(self.root, "onepeek-web", "jobs:\n  s:\n" + USES.format(OLD, "v1.7.0") + USES.format(OLD, "v1.7.0"))
        with mock.patch.object(ship, "gh", return_value="https://github.com/OnePeek/onepeek-web/pull/9\n") as gh:
            self.assertEqual(ship.open_pin_move("onepeek-web", "v1.8.2", NEW), "https://github.com/OnePeek/onepeek-web/pull/9")
        text = sh("git", "--git-dir", str(self.root / "origins/onepeek-web.git"), "show", "chore/pin-github-v1.8.2:.github/workflows/ci.yml")
        self.assertEqual(text.count(f"@{NEW} # v1.8.2"), 2)
        self.assertNotIn(OLD, text)
        args = gh.call_args.args
        self.assertEqual(args[args.index("--base") + 1], "dev")
        self.assertNotIn("More than one minor version behind", args[args.index("--body") + 1])
        self.assertEqual(sh("git", "-C", str(src), "worktree", "list").count("\n"), 1)  # nothing left behind

    def test_a_caller_far_behind_gets_the_hand_fix_note(self) -> None:
        git_repo(self.root, "mcp-athena", "jobs:\n  s:\n" + USES.format(OLD, "v1.1.1"))
        with mock.patch.object(ship, "gh", return_value="url") as gh:
            ship.open_pin_move("mcp-athena", "v1.8.2", NEW)
        body = gh.call_args.args[gh.call_args.args.index("--body") + 1]
        self.assertIn("More than one minor version behind", body)

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

    def test_rerun_failed_re_runs_only_failed_runs(self) -> None:
        cs = [{"name": "depends-on", "bucket": "fail", "link": "https://github.com/OnePeek/x/actions/runs/11/job/1"},
              {"name": "ok", "bucket": "pass", "link": "https://github.com/OnePeek/x/actions/runs/12/job/2"}]
        with mock.patch.object(ship, "read_checks", return_value=cs), mock.patch.object(ship, "gh") as gh:
            ship.rerun_failed("x", 1)
        gh.assert_called_once_with("run", "rerun", "11", "-R", "OnePeek/x", "--failed")


if __name__ == "__main__":
    unittest.main()
