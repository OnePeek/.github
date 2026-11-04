"""Tests for scripts/spot_rerun.py and spot-rerun.yml (ci-runners.md section 4, row R1). A fake gh answers the calls."""
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

import yaml

import spot_rerun as sr

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/spot-rerun.yml").read_text(encoding="utf-8"))
SHUTDOWN = "2026-11-04T10:00:00Z The runner has received a shutdown signal. This can happen when the runner is reclaimed.\n"
LOST = "The self-hosted runner lost communication with the server. Verify the machine is running.\n"


class FakeGh:
    def __init__(self, attempt=1, conclusion="failure", jobs=None, logs=None):
        self.attempt, self.conclusion = attempt, conclusion
        self.jobs = jobs if jobs is not None else [(1, "tests", "failure"), (2, "lint", "success")]
        self.logs = logs or {}
        self.calls: list[tuple] = []

    def __call__(self, *args: str) -> str:
        self.calls.append(args)
        path = next((a for a in args[1:] if not a.startswith("--")), "") if args[0] == "api" else ""
        if args[0] == "run":
            return ""
        if path.endswith("/logs"):
            job = int(path.split("/")[-2])
            if job not in self.logs:
                raise sr.GhError("404")
            return self.logs[job]
        if "/jobs" in path:
            return "".join(json.dumps({"id": i, "name": n, "conclusion": c}) + "\n" for i, n, c in self.jobs)
        return json.dumps({"run_attempt": self.attempt, "conclusion": self.conclusion})

    def reruns(self) -> list[tuple]:
        return [c for c in self.calls if c[0] == "run"]


def go(fake: FakeGh, run_id="42", repo="OnePeek/x") -> tuple[int, str]:
    out = io.StringIO()
    with mock.patch.object(sr, "gh", fake), contextlib.redirect_stdout(out):
        return sr.run(run_id, repo), out.getvalue()


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

    def test_second_attempt_stays_failed(self) -> None:
        fake = FakeGh(attempt=2, logs={1: SHUTDOWN})
        go(fake)
        self.assertEqual(fake.reruns(), [])

    def test_ordinary_failure_is_not_rerun(self) -> None:
        fake = FakeGh(logs={1: "AssertionError: 1 != 2\n"})
        go(fake)
        self.assertEqual(fake.reruns(), [])

    def test_marker_in_a_passing_job_does_not_count(self) -> None:
        fake = FakeGh(logs={1: "boring failure", 2: SHUTDOWN})
        go(fake)
        self.assertEqual(fake.reruns(), [])

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

    def test_permissions_are_actions_only(self) -> None:
        self.assertEqual(self.job["permissions"], {"actions": "write", "contents": "read"})

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
        self.assertEqual(job["permissions"], {"actions": "write", "contents": "read"})


if __name__ == "__main__":
    unittest.main()
