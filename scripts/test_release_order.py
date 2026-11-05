"""release_order.settled() and the declared lines (foundation-playbook §5.4a "The release order"), plus the merge
tool's use of them and check_lock_pins.py. Run: python -m unittest discover -s scripts -p 'test_*.py'"""

import base64
import datetime
import importlib.util
import json
import pathlib
import sys
import unittest
from unittest import mock

import check_lock_pins as pins
import release_order as ro

ROOT = pathlib.Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("ship_ro", ROOT / "workspace" / "ship_change_set.py")
ship = importlib.util.module_from_spec(_SPEC)
sys.modules["ship_ro"] = ship
_SPEC.loader.exec_module(ship)

NOW = datetime.datetime(2026, 11, 5, 12, 0, tzinfo=datetime.timezone.utc)
HEAD, OLD = "a" * 40, "b" * 40


def facts(**kw):
    base = {"key_held": False, "runs": [ro.Run(HEAD, "completed", "success", NOW)], "head": HEAD, "head_pushed": NOW - datetime.timedelta(hours=1)}
    return ro.Facts(**{**base, **kw})


class Declared(unittest.TestCase):
    def test_the_two_lines_are_read_alone_on_a_line(self):
        lines = ro.declared("Merge pull request #5\n\nrelease-independent-of: onepeek-infra\nrelease-after: backend-service\n")
        self.assertEqual(lines, {ro.INDEPENDENT: {"onepeek-infra"}, ro.AFTER: {"backend-service"}})
        self.assertEqual(ro.declared("no lines here"), {ro.INDEPENDENT: set(), ro.AFTER: set()})

    def test_any_other_release_line_fails_so_a_typo_never_means_no_edge(self):
        for bad in ("release-afterr: backend-service", "release-after: backend", "release-independent-of:", "Release-After backend-service"):
            with self.assertRaises(ro.BadLines, msg=bad):
                ro.declared(bad)

    def test_edges(self):
        none = {ro.INDEPENDENT: set(), ro.AFTER: set()}
        after = {ro.INDEPENDENT: set(), ro.AFTER: {"backend-service"}}
        self.assertEqual(ro.edges("infra", none), [])
        self.assertEqual(ro.edges("backend", none), ["infra"])
        self.assertEqual(ro.edges("web", none), ["infra"])
        self.assertEqual(ro.edges("web", after), ["infra", "backend"])
        self.assertEqual(ro.edges("mcp", after), ["infra", "backend"])


class Settled(unittest.TestCase):
    def test_a_released_head_with_nothing_running_is_settled(self):
        self.assertEqual(ro.settled("infra", facts(), now=NOW)[0], "settled")

    def test_a_held_key_or_a_live_run_waits(self):
        self.assertEqual(ro.settled("infra", facts(key_held=True), now=NOW)[0], "wait")
        self.assertEqual(ro.settled("infra", facts(runs=[ro.Run(HEAD, "queued")]), now=NOW)[0], "wait")
        # release-independent-of lifts only the unreleased-head part: a queued or running infra release is waited for
        self.assertEqual(ro.settled("infra", facts(runs=[ro.Run(HEAD, "in_progress")]), independent=True, now=NOW)[0], "wait")

    def test_a_backend_release_with_infras_head_not_released_fails_at_once(self):
        for runs in ([ro.Run(HEAD, "completed", "failure", NOW - datetime.timedelta(hours=1))], [ro.Run(HEAD, "completed", "cancelled")]):
            state, why = ro.settled("infra", facts(runs=runs), now=NOW)
            self.assertEqual(state, "fail")
            self.assertIn("onepeek-infra", why)

    def test_infra_changes_that_skip_the_release_go_on(self):
        older = [ro.Run(OLD, "completed", "success", NOW - datetime.timedelta(days=1))]
        skip = facts(runs=older, last_success_head=OLD, changed_since_success=lambda: ["README.md", "docs/x.md"])
        self.assertEqual(ro.settled("infra", skip, now=NOW)[0], "settled")
        plan = facts(runs=older, last_success_head=OLD, changed_since_success=lambda: ["README.md", "modules/x/main.tf"])
        self.assertEqual(ro.settled("infra", plan, now=NOW)[0], "fail")

    def test_an_app_head_with_no_run_is_never_settled_but_a_fresh_push_counts_as_queued(self):
        older = [ro.Run(OLD, "completed", "success")]
        self.assertEqual(ro.settled("backend", facts(runs=older), now=NOW)[0], "fail")
        self.assertEqual(ro.settled("backend", facts(runs=older, head_pushed=NOW - datetime.timedelta(minutes=2)), now=NOW)[0], "wait")

    def test_a_run_that_failed_at_a_wait_cap_counts_as_queued_for_10_minutes(self):
        capped = ro.Run(HEAD, "completed", "failure", NOW - datetime.timedelta(minutes=3), failed_at_cap=True)
        self.assertEqual(ro.settled("infra", facts(runs=[capped]), now=NOW)[0], "wait")
        capped.ended = NOW - datetime.timedelta(minutes=11)
        self.assertEqual(ro.settled("infra", facts(runs=[capped]), now=NOW)[0], "fail")

    def test_release_independent_of_infra_needs_only_the_first_bullet(self):
        self.assertEqual(ro.settled("infra", facts(runs=[ro.Run(HEAD, "completed", "failure")]), independent=True, now=NOW)[0], "settled")


class ShipUsesSettled(unittest.TestCase):
    def test_backend_needs_infra_settled_unless_the_body_declares_it_independent(self):
        failed = ro.Facts(False, [ro.Run(HEAD, "completed", "failure")], HEAD)
        self.assertIn("onepeek-infra", ship.chain_problem("backend-service", ["backend-service"], facts=lambda s: failed))
        self.assertIsNone(ship.chain_problem("backend-service", ["backend-service"], facts=lambda s: failed,
                                             body="Why\n\nrelease-independent-of: onepeek-infra\n"))

    def test_web_needs_backend_only_with_release_after(self):
        seen = []

        def f(scope):
            seen.append(scope)
            return facts()
        self.assertIsNone(ship.chain_problem("onepeek-web", ["onepeek-web"], facts=f))
        self.assertEqual(seen, ["infra"])
        seen.clear()
        self.assertIsNone(ship.chain_problem("onepeek-web", ["onepeek-web"], facts=f, body="release-after: backend-service"))
        self.assertEqual(seen, ["infra", "backend"])

    def test_a_malformed_line_in_the_body_stops(self):
        self.assertIn("not a release order line", ship.chain_problem("onepeek-web", ["onepeek-web"], facts=lambda s: facts(), body="release-aftr: x"))

    def test_the_lock_keys_by_repo(self):
        self.assertEqual(ship.lock_keys("onepeek-infra"), ["locks/infra.json", "locks/frozen.json", "locks/release.json"])
        self.assertEqual(ship.lock_keys("onepeek-web"), ["locks/infra.json", "locks/web.json", "locks/frozen.json", "locks/release.json"])

    def test_only_head_object_on_those_keys(self):
        calls = []

        def aws(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(returncode=254 if "head-object" in cmd else 0, stdout="", stderr="An error occurred (404) when calling the HeadObject operation: Not Found")
        self.assertIsNone(ship.release_lock("preprod", "p", aws, keys=ship.lock_keys("backend-service")))
        objects = [c[c.index("--key") + 1] for c in calls if "head-object" in c]
        self.assertEqual(objects, ["locks/infra.json", "locks/backend.json", "locks/frozen.json", "locks/release.json"])
        self.assertFalse([c for c in calls if "get-object" in c])

    def test_a_held_key_is_named(self):
        def aws(cmd, **kw):
            held = "head-object" in cmd and "locks/backend.json" in cmd
            return mock.Mock(returncode=0 if held or "head-bucket" in cmd else 254, stdout="2026-11-05T10:00:00+00:00" if held else "",
                             stderr="" if held else "(404) Not Found")
        since = ship.release_lock("preprod", "p", aws, keys=ship.lock_keys("backend-service"))
        self.assertIn("held since 2026-11-05T10:00:00+00:00 (locks/backend.json)", ship.lock_problem("preprod", since))


class MergeCommitLines(unittest.TestCase):
    def test_the_reviewed_body_lines_go_into_the_merge_commit_and_a_body_line_alone_is_never_read(self):
        calls = []
        with mock.patch.object(ship, "gh", side_effect=lambda *a: calls.append(a) or ""):
            ship.merge_pr("onepeek-web", 5, "--merge", [], "c" * 40, "\n".join(ro.body_lines("Text\nrelease-after: backend-service\nmore")), "dev")
        (merge,) = calls
        self.assertEqual(merge[merge.index("--body") + 1], "release-after: backend-service")
        self.assertIn("--match-head-commit", merge)

    def test_rest_fallback_on_a_graphql_error_only(self):
        calls = []

        def gh(*a):
            calls.append(a)
            if a[:2] == ("pr", "merge"):
                raise SystemExit("STOP: `gh pr merge 5 ...` failed: GraphQL: Something went wrong while executing your query")
            return ""
        with mock.patch.object(ship, "gh", side_effect=gh), mock.patch.object(ship, "say"):
            ship.merge_pr("backend-service", 5, "--squash", ["--delete-branch"], "c" * 40, None, "feat/x")
        self.assertEqual(calls[1], ("api", "-X", "PUT", "repos/OnePeek/backend-service/pulls/5/merge", "-f", "merge_method=squash", "-f", "sha=" + "c" * 40))
        self.assertEqual(calls[2], ("api", "-X", "DELETE", "repos/OnePeek/backend-service/git/refs/heads/feat/x"))

        def other(*a):
            raise SystemExit("STOP: `gh pr merge 5 ...` failed: Pull request is not mergeable")
        with mock.patch.object(ship, "gh", side_effect=other), self.assertRaises(SystemExit) as stop:
            ship.merge_pr("backend-service", 5, "--squash", [], "c" * 40, None, "feat/x")
        self.assertIn("not mergeable", str(stop.exception))

    def test_pr_create_falls_back_to_rest_on_a_graphql_error(self):
        def gh(*a):
            if a[:2] == ("pr", "create"):
                raise SystemExit("STOP: failed: GraphQL: timeout")
            return "https://github.com/OnePeek/x/pull/9\n"
        with mock.patch.object(ship, "gh", side_effect=gh) as g, mock.patch.object(ship, "say"):
            self.assertEqual(ship.create_pr("x", "dev", "b", "t", "body"), "https://github.com/OnePeek/x/pull/9")
        self.assertEqual(g.call_args.args[:4], ("api", "-X", "POST", "repos/OnePeek/x/pulls"))


class CapReruns(unittest.TestCase):
    """A run that failed only at a wait cap is re-run at once, at most 3 times (playbook §5.4a rule 3)."""

    def watch(self, conclusions, capped=True):
        state = {"i": 0}
        calls = []

        def gh(*a):
            calls.append(a)
            if a[:2] == ("run", "list"):
                c = conclusions[min(state["i"], len(conclusions) - 1)]
                return json.dumps([{"databaseId": 42, "status": "completed", "conclusion": c, "headSha": HEAD}])
            if a[:2] == ("run", "rerun"):
                state["i"] += 1
            return ""
        with mock.patch.object(ship, "gh", side_effect=gh), mock.patch.object(ship, "failed_at_cap", return_value=capped), \
                mock.patch.object(ship, "say"):
            try:
                clock = iter(range(0, 10 ** 9, 600))  # each poll ten minutes later, so a loop ends at the 5-hour limit
                ship.watch_release("backend-service", HEAD, sleep=lambda s: None, now=lambda: next(clock))
                return 0, calls
            except SystemExit as e:
                return str(e), calls

    def test_a_capped_run_is_re_run_with_all_jobs_and_then_succeeds(self):
        rc, calls = self.watch(["failure", "success"])
        self.assertEqual(rc, 0)
        self.assertEqual([c for c in calls if c[:2] == ("run", "rerun")], [("run", "rerun", "42", "-R", "OnePeek/backend-service")])

    def test_at_most_3_re_runs_then_it_stops_naming_lock_status(self):
        rc, calls = self.watch(["failure"] * 9)
        self.assertEqual(len([c for c in calls if c[:2] == ("run", "rerun")]), 3)
        self.assertIn("lock_status.py preprod", rc)

    def test_any_other_failure_is_never_re_run(self):
        rc, calls = self.watch(["failure"], capped=False)
        self.assertIn("ended failure", rc)
        self.assertFalse([c for c in calls if c[:2] == ("run", "rerun")])

    def test_failed_at_cap_reads_every_failed_jobs_error_annotations(self):
        def gh(*a):
            if "jobs" in a[1]:
                return json.dumps({"jobs": [{"id": 1, "conclusion": "failure"}, {"id": 2, "conclusion": "success"}]})
            return json.dumps(notes)
        for notes, want in (([{"annotation_level": "failure", "message": "waiting for OnePeek/onepeek-infra (infra, run 7); re-run"},
                              {"annotation_level": "failure", "message": "Process completed with exit code 1."}], True),
                            ([{"annotation_level": "failure", "message": "Release lock unreadable"}], False),
                            ([{"annotation_level": "failure", "message": "Process completed with exit code 1."}], False)):
            with mock.patch.object(ship, "gh", side_effect=gh):
                self.assertEqual(ship.failed_at_cap("backend-service", 42), want, notes)


def blob(text):
    return {"content": base64.b64encode(text.encode()).decode()}


class LockPins(unittest.TestCase):
    TAGS = [{"name": "v1.15.0", "commit": {"sha": "1" * 40}}, {"name": "v1.16.0", "commit": {"sha": "2" * 40}}]

    def api(self, files: dict):
        def api(path):
            if path.startswith("repos/OnePeek/.github/tags"):
                return self.TAGS
            repo = path.split("/")[2]
            if path.endswith("/branches?per_page=100"):
                return [{"name": b} for b in ("dev", "release", "hotfix/x", "feat/y")]
            ref = path.split("?ref=")[1]
            if "/contents/.github/workflows?" in path:
                return [{"name": "release.yml", "path": ".github/workflows/release.yml"}] if (repo, ref) in files else []
            return blob(files[(repo, ref)])
        return api

    def caller(self, sha):
        return f"jobs:\n  r:\n    uses: OnePeek/.github/.github/workflows/release-ecs.yml@{sha} # v\n"

    def test_every_caller_on_dev_release_and_hotfix_must_pin_at_least_the_tag(self):
        new = {(r, b): self.caller("2" * 40) for r in ("backend-service",) for b in ("dev", "release", "hotfix/x")}
        self.assertEqual(pins.problems("v1.16.0", ("backend-service",), self.api(new)), [])
        old = {**new, ("backend-service", "hotfix/x"): self.caller("1" * 40)}
        found = pins.problems("v1.16.0", ("backend-service",), self.api(old))
        self.assertEqual(len(found), 1)
        self.assertIn("backend-service@hotfix/x", found[0])
        self.assertIn("v1.15.0, below v1.16.0", found[0])

    def test_a_pin_that_is_not_a_tag_commit_or_unreadable_refuses(self):
        found = pins.problems("v1.15.0", ("backend-service",), self.api({("backend-service", "dev"): self.caller("9" * 40)}))
        self.assertIn("not a OnePeek/.github tag commit", found[0])

        def broken(path):
            raise RuntimeError("HTTP 500")
        self.assertTrue(pins.problems("v1.15.0", ("backend-service",), broken))

    def test_other_workflows_and_other_branches_are_not_gated(self):
        files = {("backend-service", "feat/y"): self.caller("1" * 40),
                 ("backend-service", "dev"): "jobs:\n  c:\n    uses: OnePeek/.github/.github/workflows/python-checks.yml@" + "1" * 40 + "\n"}
        self.assertEqual(pins.problems("v1.16.0", ("backend-service",), self.api(files)), [])


if __name__ == "__main__":
    unittest.main()
