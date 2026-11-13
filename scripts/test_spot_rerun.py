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
# Trimmed from OnePeek/vanguard run 43198978758 (attempt 1, job test): the Docker socket answered EOF, then the step was cancelled.
DOCKER_EOF = ('2026-11-12T10:19:04.0267694Z FAIL  checks.yml:a: ./boot.sh  (0s)\n'
              '2026-11-12T10:19:04.0270500Z error during connect: Get "http://%2Fvar%2Frun%2Fdocker.sock/_ping": EOF\n')
CANCEL_LINE = "2026-11-12T10:19:04.1632830Z ##[error]The operation was canceled.\n"
LOST_RUNNER_LOG = DOCKER_EOF + "\n" + CANCEL_LINE
ONEPEEK = ["onepeek-arm64"]


T0 = "2026-11-06T10:00:10Z"
NEWER = "2026-11-06T10:05:00Z"
OLDER = "2026-11-06T09:00:00Z"


class FakeGh:
    def __init__(self, attempt=1, conclusion="failure", jobs=None, logs=None, notes=None, event="pull_request",
                 others=None, head="aaaaaaa1111", runs_error=False, branch_error=None, on_logs=None, branch="feat/x"):
        self.branch = branch
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
                # branch_error is the HTTP status gh ends its error with; the message carries the branch name like the real one
                raise sr.GhError(f"gh api repos/OnePeek/x/branches/{self.branch} failed: gh: error (HTTP {self.branch_error})", self.branch_error)
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
            return "".join(json.dumps({"id": j[0], "name": j[1], "conclusion": j[2], "labels": j[3] if len(j) > 3 else []}) + "\n"
                           for j in self.jobs)
        return json.dumps({"run_attempt": self.attempt, "conclusion": self.conclusion, "workflow_id": 7,
                           "name": "checks", "event": self.event, "head_branch": self.branch, "head_sha": "aaaaaaa1111",
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

    def test_docker_eof_and_canceled_on_an_onepeek_runner_is_a_lost_runner(self) -> None:
        fake = FakeGh(jobs=[(1, "test", "failure", ONEPEEK)], logs={1: LOST_RUNNER_LOG})
        code, out = go(fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.reruns(), [("run", "rerun", "42", "--failed", "--repo", "OnePeek/x")])
        self.assertIn("test", out)

    def test_the_cancel_line_alone_is_not_a_lost_runner(self) -> None:
        fake = FakeGh(jobs=[(1, "test", "failure", ONEPEEK)], logs={1: CANCEL_LINE})
        go(fake)
        self.assertEqual(fake.reruns(), [])

    def test_the_docker_eof_line_alone_is_not_a_lost_runner(self) -> None:
        fake = FakeGh(jobs=[(1, "test", "failure", ONEPEEK)], logs={1: DOCKER_EOF})
        go(fake)
        self.assertEqual(fake.reruns(), [])

    def test_docker_eof_and_canceled_on_another_runner_is_not_a_lost_runner(self) -> None:
        for labels in (["ubuntu-latest"], [], None, ["self-hosted", "linux"]):
            with self.subTest(labels=labels):
                fake = FakeGh(jobs=[(1, "test", "failure", labels)], logs={1: LOST_RUNNER_LOG})
                go(fake)
                self.assertEqual(fake.reruns(), [])

    def test_the_job_list_asks_for_labels(self) -> None:
        fake = FakeGh(jobs=[(1, "test", "failure", ONEPEEK)], logs={1: LOST_RUNNER_LOG})
        go(fake)
        jq = next(c[-1] for c in fake.calls if c[0] == "api" and "/jobs?" in " ".join(c))
        self.assertIn("labels", jq)

    def test_job_printing_the_marker_forces_at_most_two_extra_reruns(self) -> None:
        # A job that prints a marker itself triggers a re-run on attempts 1 and 2 only; attempt 3 stays failed (bounded).
        fake = FakeGh(attempt=3, logs={1: SHUTDOWN_REAL})
        go(fake)
        self.assertEqual(fake.reruns(), [])

    def test_second_attempt_lost_again_is_rerun_once_more(self) -> None:  # F558: a2 lost again stayed failed
        fake = FakeGh(attempt=2, logs={1: SHUTDOWN})
        code, out = go(fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.reruns(), [("run", "rerun", "42", "--failed", "--repo", "OnePeek/x")])
        self.assertIn("tests", out)

    def test_third_attempt_stays_failed_and_is_reported(self) -> None:
        fake = FakeGh(attempt=3, logs={1: SHUTDOWN})
        code, out = go(fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.reruns(), [])
        self.assertIn("No re-run: run 42 is on attempt 3", out)
        self.assertEqual([c for c in fake.calls if any(x.endswith("/logs") for x in c)], [])  # no log read

    def test_only_attempts_one_and_two_are_rerun(self) -> None:
        for attempt, want in ((1, 1), (2, 1), (3, 0), (4, 0), (0, 0), (None, 0), ("2", 0), (2.0, 0), (True, 0)):
            fake = FakeGh(attempt=attempt, logs={1: SHUTDOWN})
            go(fake)
            self.assertEqual(len(fake.reruns()), want, msg=repr(attempt))

    def test_second_attempt_without_a_marker_is_never_rerun(self) -> None:
        for logs in ({1: "AssertionError: 1 != 2\n"}, {1: CANCELED}, {}):
            fake = FakeGh(attempt=2, logs=logs)
            go(fake)
            self.assertEqual(fake.reruns(), [], msg=repr(logs))

    def test_second_attempt_still_skips_a_superseded_run(self) -> None:
        fake = FakeGh(attempt=2, logs={1: SHUTDOWN}, head="bbbbbbb2222")
        code, out = go(fake)
        self.assertEqual(fake.reruns(), [])
        self.assertIn("superseded", out)

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
        self.check(FakeGh(branch_error=404), False, "no longer exists")

    def test_schedule_and_dispatch_use_the_newer_run_rule_only(self) -> None:
        for event in ("schedule", "workflow_dispatch"):
            self.check(FakeGh(event=event, head="bbbbbbb2222", branch_error=404), True)
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

    def test_a_branch_named_with_404_and_a_server_error_is_not_a_deleted_branch(self) -> None:
        fake = FakeGh(event="push", branch="fix/404-page", branch_error=500)
        err = io.StringIO()
        with mock.patch.object(sr, "gh", fake), mock.patch.dict(os.environ, {"RUN_ID": "42", "REPO": "OnePeek/x"}), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(err):
            self.assertEqual(sr.main(), 1)
        self.assertEqual(fake.reruns(), [])
        self.assertNotIn("no longer exists", out.getvalue())
        self.assertIn("head of branch fix/404-page could not be read", err.getvalue())
        # a real 404 on that same branch name is still a deleted branch
        self.check(FakeGh(event="push", branch="fix/404-page", branch_error=404), False, "no longer exists")

    def test_gh_reports_the_http_status_it_prints(self) -> None:
        def done(stderr: str) -> mock.Mock:
            return mock.Mock(returncode=1, stdout="", stderr=stderr)
        for stderr, status in (("gh: Not Found (HTTP 404)", 404), ("gh: Internal Server Error (HTTP 500)", 500),
                               ("could not connect to api.github.com", 0)):
            with mock.patch.object(sr.subprocess, "run", return_value=done(stderr)):
                with self.assertRaises(sr.GhError) as ctx:
                    sr.gh("api", "repos/o/fix/404-page")
            self.assertEqual(ctx.exception.status, status, stderr)

    def test_a_failed_branch_read_re_runs_nothing_and_exits_1(self) -> None:
        fake = FakeGh(branch_error=500)
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

    def test_this_repos_caller_is_the_scheduled_scan(self) -> None:  # GH-1: */10 schedule + dispatch, one at a time
        caller = yaml.safe_load((ROOT / ".github/workflows/spot-rerun-caller.yml").read_text(encoding="utf-8"))
        on = caller.get("on", caller.get(True))
        self.assertEqual(on["schedule"], [{"cron": "*/10 * * * *"}])
        self.assertIn("workflow_dispatch", on)
        self.assertNotIn("workflow_run", on)
        self.assertEqual(caller["concurrency"], {"group": "spot-rerun-caller", "cancel-in-progress": False})
        job = caller["jobs"]["spot-rerun"]
        self.assertEqual(job["with"], {"runner": "github", "workflows": "workflow-lint"})
        self.assertEqual(job["permissions"], {"actions": "write", "checks": "read", "contents": "read"})

    def test_both_caller_forms_stay_documented_and_the_scan_input_reaches_the_script(self) -> None:
        self.assertIn('#       - cron: "*/10 * * * *"', self.text)
        self.assertIn("#     workflow_run:", self.text)
        self.assertIn("#       if: ${{ github.event.workflow_run.conclusion == 'failure' }}", self.text)
        inputs = WORKFLOW.get("on", WORKFLOW.get(True))["workflow_call"]["inputs"]
        self.assertEqual((inputs["workflows"]["type"], inputs["workflows"]["default"]), ("string", ""))
        self.assertEqual(sorted(inputs), ["rerun_all_workflows", "runner", "workflows"])  # nothing a caller uses went
        step = next(s for s in self.job["steps"] if "spot_rerun" in s.get("run", ""))
        self.assertEqual(step["env"]["WORKFLOWS"], "${{ inputs.workflows }}")


class ScanGh(FakeGh):
    """FakeGh that also answers the failed-runs list of scan mode; attempts per run id, re-runs raise the attempt."""

    def __init__(self, listing=None, list_error=False, attempts=None, **kw):
        super().__init__(**kw)
        self.listing = listing if listing is not None else [(42, "checks", 1)]
        self.list_error, self.attempts = list_error, attempts or {}

    def __call__(self, *args: str) -> str:
        path = next((a for a in args[1:] if not a.startswith("--")), "") if args and args[0] == "api" else ""
        if path.split("?")[0].endswith("/actions/runs") and "?" in path:
            self.calls.append(args)
            self.list_query = urllib.parse.parse_qs(path.split("?", 1)[1])
            if self.list_error:
                raise sr.GhError("gh api failed: (HTTP 502)", 502)
            return "".join(json.dumps({"id": e[0], "name": e[1], "run_attempt": self.attempts.get(e[0], e[2]),
                                       "updated_at": e[3] if len(e) > 3 else JUST_NOW}) + "\n" for e in self.listing)
        if args and args[0] == "run" and args[1] == "rerun":
            self.attempts[int(args[2])] = self.attempts.get(int(args[2]), self.attempt) + 1
        if path.startswith("repos/") and "/actions/runs/" in path and path.count("/") == 5:
            self.calls.append(args)
            run_id = int(path.rsplit("/", 1)[1])
            return json.dumps({"run_attempt": self.attempts.get(run_id, self.attempt), "conclusion": self.conclusion,
                               "workflow_id": 7, "name": "checks", "event": self.event, "head_branch": self.branch,
                               "head_sha": "aaaaaaa1111", "created_at": T0})
        return super().__call__(*args)


NOW = 1_800_000_000.0  # 2027-01-15T08:00:00Z
JUST_NOW = "2027-01-15T07:59:00Z"


def scan(fake: ScanGh, workflows="checks", repo="OnePeek/x", now=NOW, clock=None) -> tuple[int, str]:
    out = io.StringIO()
    with mock.patch.object(sr, "gh", fake), mock.patch.object(sr, "LOG_WAIT_SECONDS", 0), contextlib.redirect_stdout(out):
        return sr.scan(repo, workflows, "", now=now, clock=clock or (lambda: 0.0)), out.getvalue()


class Scan(unittest.TestCase):
    """Scan mode (GH-1): a scheduled caller decides every failed first- or second-attempt run of its workflows, each
    attempt once (F558)."""

    def test_a_reclaimed_run_is_rerun_once_and_a_second_scan_leaves_it_alone(self) -> None:
        fake = ScanGh(logs={1: SHUTDOWN})
        self.assertEqual(scan(fake)[0], 0)
        self.assertEqual(fake.reruns(), [("run", "rerun", "42", "--failed", "--repo", "OnePeek/x")])
        fake.conclusion = None  # attempt 2 is running; a listing read before the re-run still names the run
        code, out = scan(fake)  # the next 10-minute pass
        self.assertEqual(code, 0)
        self.assertEqual(len(fake.reruns()), 1)
        self.assertIn("No re-run: run 42 did not fail", out)

    def test_a_run_rerun_between_the_list_and_the_decision_is_not_rerun_again(self) -> None:
        fake = ScanGh(logs={1: SHUTDOWN}, attempts={})
        fake.attempt = 3  # the run's own read says attempt 3: it was re-run twice after the list was read
        code, out = scan(fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.reruns(), [])
        self.assertIn("is on attempt 3", out)

    def test_only_the_named_workflows_on_attempt_one_or_two_are_decided(self) -> None:
        fake = ScanGh(listing=[(42, "checks", 1), (43, "other", 1), (44, "checks", 2), (45, "checks extra", 1),
                               (46, "checks", 3), (47, "checks", 0), (48, "checks", "2")],
                      logs={1: SHUTDOWN})
        scan(fake, workflows="checks")
        decided = [c for c in fake.calls if c[0] == "api" and "/actions/runs/" in c[1] and c[1].count("/") == 5]
        self.assertEqual({c[1].rsplit("/", 1)[1] for c in decided}, {"42", "44"})

    def test_a_run_lost_twice_is_rerun_twice_and_a_third_loss_stays_failed(self) -> None:  # F558
        fake = ScanGh(logs={1: SHUTDOWN})
        for attempt in (1, 2, 3):
            fake.attempts[42] = attempt  # attempt <n> failed with a reclaim and finished just now
            code, out = scan(fake)
            self.assertEqual(code, 0)
        self.assertEqual(fake.reruns(), [("run", "rerun", "42", "--failed", "--repo", "OnePeek/x")] * 2)
        self.assertIn("No re-run: no failed first- or second-attempt run", out)

    def test_the_list_asks_for_failed_runs_of_the_last_window(self) -> None:
        fake = ScanGh(listing=[])
        code, out = scan(fake, now=1_800_000_000.0)
        self.assertEqual(code, 0)
        self.assertEqual(fake.list_query["status"], ["failure"])
        self.assertEqual(fake.list_query["created"], [">=2027-01-14T08:00:00Z"])  # now - 24 hours, UTC (B1)
        self.assertIn("No re-run: no failed first- or second-attempt run of checks", out)

    def test_a_failed_list_read_fails_visibly(self) -> None:  # brief failure mode: GitHub API error
        fake = ScanGh(list_error=True)
        out = io.StringIO()
        with mock.patch.object(sr, "gh", fake), mock.patch.dict(os.environ, {"RUN_ID": "", "WORKFLOWS": "checks",
                                                                             "REPO": "OnePeek/x"}), \
                contextlib.redirect_stdout(out):
            self.assertEqual(sr.main(), 1)
        self.assertIn("::error::", out.getvalue())
        self.assertEqual(fake.reruns(), [])
        self.assertTrue(any(c[0] == "api" and "status=failure" in c[2] for c in fake.calls), fake.calls)  # scan mode ran

    def test_one_unreadable_run_fails_the_job_but_the_others_are_still_decided(self) -> None:
        fake = ScanGh(listing=[(41, "checks", 1), (42, "checks", 1)], logs={1: SHUTDOWN})
        real = fake.__call__

        def flaky(*args):
            if args[0] == "api" and args[1].endswith("/actions/runs/41"):
                raise sr.GhError("gh api failed: (HTTP 500)", 500)
            return real(*args)

        code, out = scan(flaky_wrap(fake, flaky))
        self.assertEqual(code, 1)
        self.assertIn("::error::run 41:", out)
        self.assertIn(("run", "rerun", "42", "--failed", "--repo", "OnePeek/x"), fake.reruns())

    def test_a_three_hour_old_run_that_just_failed_is_decided(self) -> None:  # B1: long sandbox or release runs
        fake = ScanGh(listing=[(42, "checks", 1, JUST_NOW)], logs={1: SHUTDOWN})  # created 3 h ago is inside 24 h
        self.assertEqual(scan(fake)[0], 0)
        self.assertEqual(fake.reruns(), [("run", "rerun", "42", "--failed", "--repo", "OnePeek/x")])

    def test_a_run_that_finished_before_the_decide_window_is_left_alone(self) -> None:
        fake = ScanGh(listing=[(42, "checks", 1, "2027-01-15T07:39:59Z")], logs={1: SHUTDOWN})
        code, out = scan(fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.reruns(), [])
        self.assertIn("finished since 2027-01-15T07:40:00Z", out)

    def test_an_ordinary_failure_is_decided_at_most_twice(self) -> None:  # B2: one run's log waits are not paid forever
        fake = ScanGh(listing=[(42, "checks", 1, JUST_NOW)], logs={1: "plain test failure\n"})
        decided = 0
        for minutes in (0, 10, 20, 30, 40):
            before = len([c for c in fake.calls if c[0] == "api" and any(x.endswith("/logs") for x in c)])
            self.assertEqual(scan(fake, now=NOW + minutes * 60)[0], 0)
            decided += len([c for c in fake.calls if c[0] == "api" and any(x.endswith("/logs") for x in c)]) > before
        self.assertEqual(decided, 2)
        self.assertEqual(fake.reruns(), [])

    def test_the_time_budget_stops_cleanly_and_names_the_runs_left(self) -> None:  # B2: never killed by the timeout
        fake = ScanGh(listing=[(41, "checks", 1, "2027-01-15T07:50:00Z"), (42, "checks", 1, "2027-01-15T07:55:00Z"),
                               (43, "checks", 1, JUST_NOW)], logs={1: SHUTDOWN})
        ticks = iter([0.0, 0.0, 130.0, 130.0])  # start, before run 41, before run 42 (no room for another worst case)
        code, out = scan(fake, clock=lambda: next(ticks))
        self.assertEqual(code, 1)
        self.assertIn("::error::the scan's time budget (240 s) ran out; not decided: run(s) 42, 43", out)
        decided = {c[1].rsplit("/", 1)[1] for c in fake.calls if c[0] == "api" and c[1].count("/") == 5
                   and "/actions/runs/" in c[1]}
        self.assertEqual(decided, {"41"})  # oldest finish first; nothing started after the budget check

    def test_bad_workflow_names_or_repo_are_refused_before_any_call(self) -> None:
        for workflows, repo in (("", "OnePeek/x"), ("a;b", "OnePeek/x"), ("ok", "a/$(x)"), ("$(x)", "OnePeek/x")):
            fake = ScanGh()
            with mock.patch.object(sr, "gh", fake), self.assertRaises(ValueError, msg=(workflows, repo)):
                sr.scan(repo, workflows)
            self.assertEqual(fake.calls, [])

    def test_main_needs_run_id_or_workflows(self) -> None:
        fake = ScanGh()
        out = io.StringIO()
        with mock.patch.object(sr, "gh", fake), mock.patch.dict(os.environ, {"RUN_ID": "", "WORKFLOWS": " ",
                                                                             "REPO": "OnePeek/x"}), \
                contextlib.redirect_stdout(out):
            self.assertEqual(sr.main(), 1)
        self.assertEqual(fake.calls, [])


def flaky_wrap(fake: ScanGh, fn):
    """A callable that answers through fn but keeps the fake's record (calls, reruns)."""
    class Wrapped:
        def __call__(self, *args):
            return fn(*args)

        def __getattr__(self, name):
            return getattr(fake, name)
    return Wrapped()

if __name__ == "__main__":
    unittest.main()
