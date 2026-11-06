"""Tests for scripts/spot_rerun.py and spot-rerun.yml (ci-runners.md section 4, row R1). A fake gh answers the calls."""
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import tempfile
import unittest
import urllib.parse
from unittest import mock

import yaml

import spot_rerun as sr

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/spot-rerun.yml").read_text(encoding="utf-8"))
SHUTDOWN = "2026-11-04T10:00:00Z The runner has received a shutdown signal. This can happen when the runner is reclaimed.\n"
LOST = ("The self-hosted runner: onepeek-arm64-abc123 lost communication with the server. Verify the machine is "
        "running and has a healthy network connection. Anything in your workflow that terminates the runner process, "
        "starves it for CPU/Memory, or blocks its network access can cause this error.\n")
SHUTDOWN_REAL = ("The runner has received a shutdown signal. This can happen when the runner service is stopped, or a "
                 "manually started runner is canceled.\n")
CANCELED = "The operation was canceled."


T0 = "2026-11-06T10:00:10Z"
NEWER = "2026-11-06T10:05:00Z"
OLDER = "2026-11-06T09:00:00Z"


class FakeGh:
    def __init__(self, attempt=1, conclusion="failure", jobs=None, logs=None, notes=None, event="pull_request",
                 others=None, head="aaaaaaa1111", runs_error=False, branch_error=None, on_logs=None):
        self.attempt, self.conclusion = attempt, conclusion
        self.event, self.others, self.head = event, others if others is not None else [], head
        self.runs_error, self.branch_error, self.on_logs = runs_error, branch_error, on_logs
        self.jobs = jobs if jobs is not None else [(1, "tests", "failure"), (2, "lint", "success")]
        self.logs = logs or {}
        self.notes = notes or {}
        self.calls: list[tuple] = []

    def __call__(self, *args: str) -> str:
        self.calls.append(args)
        path = next((a for a in args[1:] if not a.startswith("--")), "") if args[0] == "api" else ""
        if args[0] == "run":
            return ""
        if "/actions/workflows/" in path:
            if self.runs_error:
                raise sr.GhError("500")
            self.runs_query = urllib.parse.parse_qs(path.split("?", 1)[1])
            return "".join(json.dumps({"id": i, "created_at": c, "html_url": f"https://github.com/OnePeek/x/actions/runs/{i}"}) + "\n"
                           for i, c in self.others)
        if "/branches/" in path:
            if self.branch_error:
                raise sr.GhError(self.branch_error)
            return self.head + "\n"
        if path.startswith("repos/") and "/check-runs/" in path:
            job = int(path.split("/check-runs/")[1].split("/")[0])
            if job not in self.notes:
                raise sr.GhError("403")
            return "".join(m + "\n" for m in self.notes[job])
        if path.endswith("/logs") and getattr(self, "old_gh", False) and "--allow-escape-sequences" in args:
            raise sr.GhError("gh api failed: unknown flag: --allow-escape-sequences")
        if path.endswith("/logs") and self.on_logs:
            self.on_logs(self)
        if path.endswith("/logs"):
            job = int(path.split("/")[-2])
            if job not in self.logs:
                raise sr.GhError("404")
            return self.logs[job]
        if "/jobs" in path:
            return "".join(json.dumps({"id": i, "name": n, "conclusion": c}) + "\n" for i, n, c in self.jobs)
        return json.dumps({"run_attempt": self.attempt, "conclusion": self.conclusion, "workflow_id": 7,
                           "name": "checks", "event": self.event, "head_branch": "feat/x", "head_sha": "aaaaaaa1111",
                           "created_at": T0})

    def reruns(self) -> list[tuple]:
        return [c for c in self.calls if c[0] == "run"]


def go(fake: FakeGh, run_id="42", repo="OnePeek/x", workflow="", rerun_all="") -> tuple[int, str]:
    out = io.StringIO()
    with mock.patch.object(sr, "gh", fake), mock.patch.object(sr, "LOG_WAIT_SECONDS", 0), contextlib.redirect_stdout(out):
        return sr.run(run_id, repo, workflow, rerun_all), out.getvalue()


class Decision(unittest.TestCase):
    def test_shutdown_signal_reruns_once_and_names_the_job(self) -> None:
        fake = FakeGh(logs={1: SHUTDOWN})
        code, out = go(fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.reruns(), [("run", "rerun", "42", "--failed", "--repo", "OnePeek/x")])
        self.assertIn("tests", out)

    def test_lost_communication_reruns(self) -> None:
        fake = FakeGh(logs={1: LOST})
        go(fake)
        self.assertEqual(len(fake.reruns()), 1)

    def test_real_log_wording_in_the_log_only(self) -> None:
        fake = FakeGh(logs={1: SHUTDOWN_REAL}, notes={1: [CANCELED]})
        go(fake)
        self.assertEqual(len(fake.reruns()), 1)

    def test_marker_only_in_annotation_with_no_log(self) -> None:
        fake = FakeGh(logs={}, notes={1: [LOST]})
        _, out = go(fake)
        self.assertEqual(len(fake.reruns()), 1)
        self.assertIn("tests", out)

    def test_both_sources_unreadable_is_no_reclaim(self) -> None:
        fake = FakeGh(logs={}, notes={})
        go(fake)
        self.assertEqual(fake.reruns(), [])

    def test_one_job_readable_and_one_with_no_log(self) -> None:
        fake = FakeGh(jobs=[(1, "a", "failure"), (2, "b", "failure")], logs={1: "plain failure"}, notes={2: [LOST]})
        _, out = go(fake)
        self.assertEqual(len(fake.reruns()), 1)
        self.assertIn("reclaim in b.", out)

    def test_operation_was_canceled_alone_is_not_rerun(self) -> None:
        fake = FakeGh(logs={1: "plain failure"}, notes={1: [CANCELED]})
        go(fake)
        self.assertEqual(fake.reruns(), [])

    def test_job_printing_the_marker_forces_at_most_one_extra_rerun(self) -> None:
        # A job that prints a marker itself triggers a re-run on attempt 1 only; attempt 2 stays failed (bounded).
        fake = FakeGh(attempt=2, logs={1: SHUTDOWN_REAL})
        go(fake)
        self.assertEqual(fake.reruns(), [])

    def test_second_attempt_stays_failed(self) -> None:
        fake = FakeGh(attempt=2, logs={1: SHUTDOWN})
        go(fake)
        self.assertEqual(fake.reruns(), [])

    def test_a_listed_workflow_is_rerun_whole(self) -> None:
        fake = FakeGh(logs={1: SHUTDOWN})
        go(fake, workflow="sandbox", rerun_all="sandbox other")
        self.assertEqual(fake.reruns(), [("run", "rerun", "42", "--repo", "OnePeek/x")])

    def test_an_unlisted_workflow_reruns_only_the_failed_jobs(self) -> None:
        fake = FakeGh(logs={1: SHUTDOWN})
        go(fake, workflow="checks", rerun_all="sandbox")
        self.assertEqual(fake.reruns(), [("run", "rerun", "42", "--failed", "--repo", "OnePeek/x")])

    def test_a_listed_workflow_without_a_reclaim_is_not_rerun(self) -> None:
        fake = FakeGh(logs={1: "plain failure"})
        go(fake, workflow="sandbox", rerun_all="sandbox")
        self.assertEqual(fake.reruns(), [])

    def test_ordinary_failure_is_not_rerun(self) -> None:
        fake = FakeGh(logs={1: "AssertionError: 1 != 2\n"})
        go(fake)
        self.assertEqual(fake.reruns(), [])

    def test_marker_in_a_passing_job_does_not_count(self) -> None:
        fake = FakeGh(logs={1: "boring failure", 2: SHUTDOWN})
        go(fake)
        self.assertEqual(fake.reruns(), [])

    def test_a_log_that_arrives_late_still_counts(self) -> None:
        fake = FakeGh(logs={})
        reads = {"n": 0}
        real = fake.__call__

        def late(*args: str) -> str:
            if args[0] == "api" and any(str(a).endswith("/logs") for a in args):
                reads["n"] += 1
                if reads["n"] >= 3:
                    fake.logs[1] = SHUTDOWN
            return real(*args)
        _, out = go(late)
        self.assertEqual(len(fake.reruns()), 1)
        self.assertEqual(reads["n"], 3)

    def test_an_unreadable_log_is_reported_with_the_reason(self) -> None:
        fake = FakeGh(logs={})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            go(fake)
        self.assertIn("no reclaim seen after", err.getvalue())
        self.assertIn("404", err.getvalue())

    def test_the_log_is_read_with_escape_sequences_allowed(self) -> None:
        fake = FakeGh(logs={1: SHUTDOWN})
        go(fake)
        log_calls = [c for c in fake.calls if c[0] == "api" and str(c[-1]).endswith("/logs")]
        self.assertTrue(log_calls and all("--allow-escape-sequences" in c for c in log_calls), log_calls)
        self.assertEqual(len(fake.reruns()), 1)

    def test_an_older_gh_without_the_flag_still_reads_the_log(self) -> None:
        fake = FakeGh(logs={1: SHUTDOWN})
        fake.old_gh = True
        go(fake)
        self.assertEqual(len(fake.reruns()), 1)

    def test_unreadable_log_is_no_reclaim(self) -> None:
        fake = FakeGh(logs={})
        go(fake)
        self.assertEqual(fake.reruns(), [])

    def test_run_that_did_not_fail_is_left_alone(self) -> None:
        fake = FakeGh(conclusion="success", logs={1: SHUTDOWN})
        go(fake)
        self.assertEqual(fake.reruns(), [])

    def test_summary_file_gets_the_reason(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "summary.md")
            with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": path}):
                go(FakeGh(logs={1: SHUTDOWN}))
            self.assertIn("spot reclaim in tests", pathlib.Path(path).read_text(encoding="utf-8"))

    def test_job_name_cannot_inject_markup(self) -> None:
        fake = FakeGh(jobs=[(1, "a`\n## x", "failure")], logs={1: SHUTDOWN})
        _, out = go(fake)
        self.assertNotIn("`", out)
        self.assertEqual(len(out.strip().splitlines()), 1)


class Superseded(unittest.TestCase):
    """ci-runners.md section 4.1: a superseded run is never re-run (a re-run would cancel the current run)."""

    def check(self, fake: FakeGh, expect_rerun: bool, text: str = "", **kw) -> str:
        fake.logs = fake.logs or {1: SHUTDOWN}
        code, out = go(fake, **kw)
        self.assertEqual(len(fake.reruns()), 1 if expect_rerun else 0, out)
        self.assertEqual(code, 0)
        self.assertIn(text, out)
        return out

    def test_a_newer_run_of_the_same_workflow_and_branch_supersedes_it(self) -> None:
        fake = FakeGh(others=[(41, OLDER), (43, NEWER)])
        self.check(fake, False, "superseded by https://github.com/OnePeek/x/actions/runs/43 (newer run of checks on feat/x)")
        self.assertEqual(fake.runs_query["branch"], ["feat/x"])
        self.assertEqual(fake.runs_query["event"], ["pull_request"])
        self.assertEqual(fake.runs_query["created"], [">=" + T0])

    def test_a_tie_on_created_at_counts_as_newer(self) -> None:
        self.check(FakeGh(others=[(42, T0), (99, T0)]), False, "superseded by https://github.com/OnePeek/x/actions/runs/99")

    def test_the_run_itself_does_not_supersede_it(self) -> None:
        self.check(FakeGh(others=[(42, T0)]), True)

    def test_an_older_run_does_not_supersede_it(self) -> None:
        self.check(FakeGh(others=[(41, OLDER)]), True)

    def test_a_superseded_run_costs_no_log_read(self) -> None:
        fake = FakeGh(others=[(43, NEWER)])
        self.check(fake, False)
        self.assertFalse([c for c in fake.calls if any(str(a).endswith("/logs") for a in c)])

    def test_a_moved_branch_head_supersedes_push_and_pull_request(self) -> None:
        for event in ("push", "pull_request"):
            self.check(FakeGh(event=event, head="bbbbbbb2222"), False,
                       "head of feat/x is now bbbbbbb, not aaaaaaa")

    def test_the_head_still_the_run_head_is_rerun(self) -> None:
        self.check(FakeGh(event="push", head="aaaaaaa1111"), True)

    def test_a_deleted_branch_supersedes(self) -> None:
        self.check(FakeGh(branch_error="gh: Not Found (HTTP 404)"), False, "no longer exists")

    def test_schedule_and_dispatch_use_the_newer_run_rule_only(self) -> None:
        for event in ("schedule", "workflow_dispatch"):
            self.check(FakeGh(event=event, head="bbbbbbb2222", branch_error="404"), True)
            self.check(FakeGh(event=event, others=[(43, NEWER)]), False)

    def test_a_push_during_the_log_wait_is_caught_by_the_second_check(self) -> None:
        def push_lands(fake: FakeGh) -> None:
            fake.head = "bbbbbbb2222"
        fake = FakeGh(on_logs=push_lands)
        self.check(fake, False, "head of feat/x is now bbbbbbb")

    def test_a_newer_run_during_the_log_wait_is_caught_by_the_second_check(self) -> None:
        def run_starts(fake: FakeGh) -> None:
            fake.others = [(43, NEWER)]
        self.check(FakeGh(on_logs=run_starts), False, "superseded by https://github.com/OnePeek/x/actions/runs/43")

    def test_runs_are_matched_by_workflow_id_in_the_path(self) -> None:
        fake = FakeGh()
        self.check(fake, True)
        self.assertTrue(any("/actions/workflows/7/runs" in str(a) for c in fake.calls for a in c))

    def test_a_failed_runs_read_re_runs_nothing_and_exits_1(self) -> None:
        fake = FakeGh(runs_error=True)
        err = io.StringIO()
        with mock.patch.object(sr, "gh", fake), mock.patch.dict(os.environ, {"RUN_ID": "42", "REPO": "OnePeek/x"}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(sr.main(), 1)
        self.assertEqual(fake.reruns(), [])
        self.assertIn("::warning::", err.getvalue())
        self.assertIn("runs of workflow 7", err.getvalue())

    def test_a_failed_branch_read_re_runs_nothing_and_exits_1(self) -> None:
        fake = FakeGh(branch_error="500")
        err = io.StringIO()
        with mock.patch.object(sr, "gh", fake), mock.patch.dict(os.environ, {"RUN_ID": "42", "REPO": "OnePeek/x"}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(sr.main(), 1)
        self.assertEqual(fake.reruns(), [])
        self.assertIn("head of branch feat/x could not be read", err.getvalue())

    def test_a_run_without_comparable_fields_re_runs_nothing(self) -> None:
        fake = FakeGh()
        real = fake.__call__

        def bare(*args: str) -> str:
            out = real(*args)
            return json.dumps({"run_attempt": 1, "conclusion": "failure"}) if "--jq" in args and "run_attempt" in args[-1] else out
        with self.assertRaises(sr.GhError), contextlib.redirect_stderr(io.StringIO()), mock.patch.object(sr, "gh", bare):
            sr.run("42", "OnePeek/x")
        self.assertEqual(fake.reruns(), [])

    def test_rerun_all_workflows_follow_the_same_rules(self) -> None:
        fake = FakeGh(others=[(43, NEWER)])
        self.check(fake, False, workflow="sandbox", rerun_all="sandbox")
        self.check(FakeGh(), True, workflow="sandbox", rerun_all="sandbox")

    def test_a_different_workflow_or_branch_is_not_in_the_list_the_api_returns(self) -> None:
        # The query names the workflow id, branch and event, so the API returns no such run: re-run.
        self.check(FakeGh(others=[]), True)


class Inputs(unittest.TestCase):
    def test_bad_run_ids_and_repos_are_refused(self) -> None:
        for run_id in ["", "0", "1;ls", "12 34", "-1", "1" * 21, "４２"]:
            with self.assertRaises(ValueError, msg=run_id):
                sr.validate(run_id, "OnePeek/x")
        for repo in ["", "x", "a/b/c", "a/..", "../b", "a/b c", "a/b;c", "a/$(x)"]:
            with self.assertRaises(ValueError, msg=repo):
                sr.validate("42", repo)
        sr.validate("42", "OnePeek/.github")

    def test_main_reports_an_error_without_calling_gh(self) -> None:
        fake = FakeGh()
        out = io.StringIO()
        with mock.patch.object(sr, "gh", fake), mock.patch.dict(os.environ, {"RUN_ID": "1;x", "REPO": "OnePeek/x"}), \
                contextlib.redirect_stdout(out):
            self.assertEqual(sr.main(), 1)
        self.assertEqual(fake.calls, [])
        self.assertIn("::error::", out.getvalue())


class Workflow(unittest.TestCase):
    job = WORKFLOW["jobs"]["spot-rerun"]
    text = (ROOT / ".github/workflows/spot-rerun.yml").read_text(encoding="utf-8")

    def test_is_reusable_with_runner_input_on_the_small_class(self) -> None:
        on = WORKFLOW.get("on", WORKFLOW.get(True))
        self.assertEqual(list(on), ["workflow_call"])
        self.assertEqual(on["workflow_call"]["inputs"]["runner"]["default"], "github")
        self.assertIn("onepeek-arm64-small", self.job["runs-on"])
        self.assertLessEqual(self.job["timeout-minutes"], 30)

    def test_permissions_are_actions_and_checks_only(self) -> None:
        self.assertEqual(self.job["permissions"], {"actions": "write", "checks": "read", "contents": "read"})

    def test_run_id_and_repo_come_only_from_the_event_and_the_repository(self) -> None:
        step = next(s for s in self.job["steps"] if "spot_rerun" in s.get("run", ""))
        self.assertEqual(step["env"]["RUN_ID"], "${{ github.event.workflow_run.id }}")
        self.assertEqual(step["env"]["REPO"], "${{ github.repository }}")
        self.assertNotIn("${{", step["run"])

    def test_no_untrusted_expression_in_any_run(self) -> None:
        for step in self.job["steps"]:
            self.assertNotIn("${{", step.get("run", ""), step)

    def test_caller_example_runs_only_on_failed_workflow_runs(self) -> None:
        caller = yaml.safe_load((ROOT / ".github/workflows/spot-rerun-caller.yml").read_text(encoding="utf-8"))
        on = caller.get("on", caller.get(True))
        self.assertEqual(on["workflow_run"]["types"], ["completed"])
        job = caller["jobs"]["spot-rerun"]
        self.assertIn("conclusion == 'failure'", job["if"])
        self.assertEqual(job["permissions"], {"actions": "write", "checks": "read", "contents": "read"})


if __name__ == "__main__":
    unittest.main()
