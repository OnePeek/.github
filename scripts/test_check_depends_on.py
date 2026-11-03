"""Tests for scripts/check_depends_on.py and depends-on.yml (foundation-playbook §8a P1, lesson L26)."""
from __future__ import annotations

import io
import os
import pathlib
import contextlib
import unittest
from unittest import mock

import yaml

import check_depends_on as cd

WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / ".github/workflows/depends-on.yml"
STEPS = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["depends-on"]["steps"]
BODY = "Fixes the boot.\n\nDepends on: OnePeek/backend-service#73\n depends on: OnePeek/onepeek-web#5 \n" \
       "Depends on: OnePeek/backend-service#73\nSee OnePeek/onepeek-infra#9 (not a dependency)\n"


class Parse(unittest.TestCase):
    def test_one_line_each_in_order_once(self) -> None:
        self.assertEqual(cd.parse(BODY), [("backend-service", 73), ("onepeek-web", 5)])

    def test_no_lines_or_no_body(self) -> None:
        self.assertEqual(cd.parse(None), [])
        self.assertEqual(cd.parse("Depends on: backend-service#73\nDepends on: OnePeek/x"), [])

    def test_repo_names_are_ascii_and_never_only_dots(self) -> None:
        self.assertEqual(cd.parse("Depends on: OnePeek/.github#34\nDepends on: OnePeek/a_b.c-9#1"), [(".github", 34), ("a_b.c-9", 1)])
        for bad in ("OnePeek/.#1", "OnePeek/..#2", "OnePeek/...#3", "OnePeek/bäckend#4", "OnePeek/web١#5",
                    "OnePeek/x/y#6", "OnePeek/x#١"):
            self.assertEqual(cd.parse(f"Depends on: {bad}"), [], bad)


class Waiting(unittest.TestCase):
    def test_open_waits_closed_unmerged_fails_and_merged_passes(self) -> None:
        states = {("a", 1): ("open", False), ("b", 2): ("closed", False), ("c", 3): ("closed", True)}
        out = cd.waiting([("a", 1), ("b", 2), ("c", 3)], lambda r, n: states[(r, n)])
        self.assertEqual(out[0], "waiting for a#1")
        self.assertIn("closed without merging", out[1])
        self.assertEqual(len(out), 2)


class Main(unittest.TestCase):
    def run_main(self, argv: list[str], body: str, state=None) -> tuple[int, str]:
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"BODY": body, "GH_TOKEN": "t"}), contextlib.redirect_stdout(buf), \
                mock.patch.object(cd, "api_state", side_effect=state or (lambda r, n, t: ("open", False))):
            rc = cd.main(argv)
        return rc, buf.getvalue()

    def test_repos_lists_the_named_repos_for_the_token(self) -> None:
        self.assertEqual(self.run_main(["--repos"], BODY), (0, "backend-service,onepeek-web\n"))
        self.assertEqual(self.run_main(["--repos"], "no lines"), (0, "\n"))

    def test_check_fails_with_waiting_for_while_unmerged(self) -> None:
        rc, out = self.run_main(["--check"], BODY)
        self.assertEqual(rc, 1)
        self.assertIn("::error::waiting for backend-service#73", out)

    def test_check_passes_once_merged_or_with_no_lines(self) -> None:
        self.assertEqual(self.run_main(["--check"], BODY, lambda r, n, t: ("closed", True))[0], 0)
        self.assertEqual(self.run_main(["--check"], "")[0], 0)


class Workflow(unittest.TestCase):
    def test_the_app_token_is_only_minted_for_named_repos_with_pull_requests_read(self) -> None:
        app = next(s for s in STEPS if s.get("id") == "app")
        self.assertEqual(app["if"], "${{ steps.deps.outputs.repos != '' }}")
        self.assertEqual(app["with"]["repositories"], "${{ steps.deps.outputs.repos }}")
        self.assertEqual(app["with"]["permission-pull-requests"], "read")
        self.assertEqual(set(k for k in app["with"] if k.startswith("permission-")), {"permission-pull-requests"})

    def test_the_body_reaches_the_script_through_env_never_inline(self) -> None:
        for s in STEPS:
            self.assertNotIn("github.event.pull_request.body", str(s.get("run", "")))
        self.assertEqual(STEPS[-1]["env"]["BODY"], "${{ github.event.pull_request.body }}")
        self.assertIn("--check", STEPS[-1]["run"])

    def test_the_callers_in_this_repo_run_it_first_and_every_other_job_needs_it(self) -> None:
        jobs = yaml.safe_load((WORKFLOW.parent / "workflow-lint.yml").read_text(encoding="utf-8"))["jobs"]
        self.assertEqual(jobs["depends-on"]["uses"], "./.github/workflows/depends-on.yml")
        for name, job in jobs.items():
            if name != "depends-on":
                self.assertEqual(job.get("needs"), "depends-on", name)
        on = yaml.safe_load((WORKFLOW.parent / "workflow-lint.yml").read_text(encoding="utf-8"))[True]
        self.assertIn("edited", on["pull_request"]["types"])


if __name__ == "__main__":
    unittest.main()
