"""Tests for scripts/check_depends_on.py and depends-on.yml (foundation-playbook §8a P1, lessons L26 and L27)."""
from __future__ import annotations

import io
import json
import os
import pathlib
import contextlib
import shutil
import tempfile
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


PIN = "a" * 40


class DocsWaiting(unittest.TestCase):  # lesson L27 (a): the implicit docs dependency
    def test_no_pin_or_a_pin_on_main_waits_for_nothing(self) -> None:
        never = mock.Mock(side_effect=AssertionError("not asked"))
        self.assertEqual(cd.docs_waiting(None, never, never), ([], []))
        for st in ("ahead", "identical"):
            for strict in (False, True):
                self.assertEqual(cd.docs_waiting(PIN, lambda s, st=st: st, never, strict), ([], []))

    def test_a_pin_in_an_open_docs_pull_request_passes_with_a_note(self) -> None:
        for st in ("diverged", "behind"):
            problems, notes = cd.docs_waiting(PIN, lambda s, st=st: st, lambda s: [145, 150])
            self.assertEqual(problems, [])
            self.assertEqual(len(notes), 1)
            self.assertTrue(notes[0].startswith("waiting for onepeek-docs#145 (docs-version.txt pins aaaaaaa)"), notes[0])
            self.assertIn("ship_change_set.py merges it first", notes[0])

    def test_strict_fails_that_wait(self) -> None:
        problems, notes = cd.docs_waiting(PIN, lambda s: "diverged", lambda s: [145, 150], strict=True)
        self.assertEqual(notes, [])
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("waiting for onepeek-docs#145"), problems[0])

    def test_a_pin_in_no_open_docs_pull_request_fails_even_when_not_strict(self) -> None:
        for st in ("diverged", "missing"):
            for strict in (False, True):
                problems, notes = cd.docs_waiting(PIN, lambda s, st=st: st, lambda s: [], strict)
                self.assertEqual(notes, [])
                self.assertEqual(len(problems), 1)
                self.assertIn("docs pin aaaaaaa not on main and in no open docs PR", problems[0])

    def test_only_open_pull_requests_count_lowest_first(self) -> None:
        prs = [{"number": 9, "state": "open"}, {"number": 3, "state": "closed"}, {"number": 4, "state": "open"}]
        resp = mock.MagicMock()
        resp.__enter__.return_value = io.StringIO(json.dumps(prs))
        with mock.patch.object(cd.urllib.request, "urlopen", return_value=resp) as u:
            self.assertEqual(cd.open_docs_prs(PIN, "t"), [4, 9])
        self.assertTrue(u.call_args[0][0].full_url.endswith(f"/repos/OnePeek/onepeek-docs/commits/{PIN}/pulls"))

    def test_a_commit_docs_does_not_have_is_in_no_pull_request(self) -> None:
        err = cd.urllib.error.HTTPError("u", 422, "x", {}, None)
        with mock.patch.object(cd.urllib.request, "urlopen", side_effect=err):
            self.assertEqual(cd.open_docs_prs(PIN, "t"), [])


class Main(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)

    def run_main(self, argv: list[str], body: str, state=None, pin: str | None = None, status: str = "diverged",
                 prs: list[int] | None = None, docs_token: str = "d") -> tuple[int, str]:
        if pin:
            (self.dir / "docs-version.txt").write_text(f"# the docs commit\nonepeek-docs@{pin}\n", encoding="utf-8")
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"BODY": body, "GH_TOKEN": "t", "DOCS_TOKEN": docs_token}), \
                contextlib.redirect_stdout(buf), \
                mock.patch.object(cd, "api_state", side_effect=state or (lambda r, n, t: ("open", False))), \
                mock.patch.object(cd.check_pins, "compare_status", return_value=status), \
                mock.patch.object(cd, "open_docs_prs", return_value=prs or []):
            rc = cd.main([*argv, "--repo-dir", str(self.dir)])
        return rc, buf.getvalue()

    def test_docs_pin_prints_the_pinned_commit_or_nothing(self) -> None:
        self.assertEqual(self.run_main(["--docs-pin"], ""), (0, "\n"))
        self.assertEqual(self.run_main(["--docs-pin"], "", pin=PIN), (0, PIN + "\n"))

    def test_check_passes_with_a_notice_while_the_docs_pull_request_is_open(self) -> None:
        rc, out = self.run_main(["--check"], "", pin=PIN, prs=[145])
        self.assertEqual(rc, 0)
        self.assertIn("::notice title=depends-on::waiting for onepeek-docs#145 (docs-version.txt pins aaaaaaa); "
                      "ship_change_set.py merges it first", out)
        self.assertNotIn("::error", out)

    def test_check_strict_fails_on_that_wait(self) -> None:
        rc, out = self.run_main(["--check", "--strict"], "", pin=PIN, prs=[145])
        self.assertEqual(rc, 1)
        self.assertIn("::error::waiting for onepeek-docs#145", out)

    def test_check_fails_on_a_pin_in_no_open_docs_pull_request(self) -> None:
        rc, out = self.run_main(["--check"], "", pin=PIN)
        self.assertEqual(rc, 1)
        self.assertIn("not on main and in no open docs PR", out)

    def test_check_passes_on_a_pin_on_main_and_still_checks_declared_lines(self) -> None:
        self.assertEqual(self.run_main(["--check"], "", pin=PIN, status="ahead")[0], 0)
        rc, out = self.run_main(["--check"], BODY, pin=PIN, status="identical")
        self.assertEqual(rc, 1)
        self.assertIn("::error::waiting for backend-service#73", out)

    def test_check_reports_both_kinds_together(self) -> None:
        rc, out = self.run_main(["--check", "--strict"], BODY, pin=PIN, prs=[7])
        self.assertEqual(rc, 1)
        self.assertIn("::error::waiting for onepeek-docs#7", out)
        self.assertIn("::error::waiting for backend-service#73", out)

    def test_a_declared_dependency_still_fails_when_the_docs_wait_is_only_a_notice(self) -> None:
        rc, out = self.run_main(["--check"], BODY, pin=PIN, prs=[7])
        self.assertEqual(rc, 1)
        self.assertIn("::notice title=depends-on::waiting for onepeek-docs#7", out)
        self.assertIn("::error::waiting for backend-service#73", out)

    def test_a_declared_dependency_closed_unmerged_fails(self) -> None:
        rc, out = self.run_main(["--check"], "Depends on: OnePeek/backend-service#73", state=lambda r, n, t: ("closed", False))
        self.assertEqual(rc, 1)
        self.assertIn("closed without merging", out)

    def test_check_with_a_pin_needs_the_docs_token(self) -> None:
        rc, out = self.run_main(["--check"], "", pin=PIN, status="ahead", docs_token="")
        self.assertEqual(rc, 1)
        self.assertIn("no token to read onepeek-docs", out)

    def test_with_no_pull_request_there_is_no_body_and_nothing_to_wait_for(self) -> None:
        # push, schedule and dispatch runs: depends-on.yml skips the body step, so neither BODY_FILE nor BODY is set.
        env = {k: v for k, v in os.environ.items() if k not in ("BODY", "BODY_FILE", "GH_TOKEN", "DOCS_TOKEN")}
        for flag, want in (("--repos", ""), ("--docs-pin", ""), ("--check", "nothing to wait for")):
            buf = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(buf):
                rc = cd.main([flag, "--repo-dir", str(self.dir)])
            self.assertEqual(rc, 0, flag)
            self.assertIn(want, buf.getvalue())

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


class LiveBody(unittest.TestCase):
    def test_body_file_wins_over_the_event_body_and_parsing_stays_strict(self) -> None:
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        f = d / "body.txt"
        f.write_text("Depends on: OnePeek/onepeek-web#5\nDepends on: OnePeek/../x#1\nxDepends on: OnePeek/a#2\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"BODY_FILE": str(f), "BODY": "Depends on: OnePeek/stale#1"}):
            self.assertEqual(cd.parse(cd.read_body()), [("onepeek-web", 5)])
        with mock.patch.dict(os.environ, {"BODY": "Depends on: OnePeek/a#1"}, clear=True):
            self.assertEqual(cd.parse(cd.read_body()), [("a", 1)])


class Workflow(unittest.TestCase):
    def test_the_app_token_is_only_minted_for_named_repos_with_pull_requests_read(self) -> None:
        app = next(s for s in STEPS if s.get("id") == "app")
        self.assertEqual(app["if"], "${{ github.event.pull_request.number != '' && steps.deps.outputs.repos != '' }}")
        self.assertEqual(app["with"]["repositories"], "${{ steps.deps.outputs.repos }}")
        self.assertEqual(app["with"]["permission-pull-requests"], "read")
        self.assertEqual(set(k for k in app["with"] if k.startswith("permission-")), {"permission-pull-requests"})

    def test_the_body_is_read_live_from_the_api_never_from_the_event_payload(self) -> None:
        self.assertNotIn("github.event.pull_request.body", WORKFLOW.read_text(encoding="utf-8").replace("# ", ""),)
        body = next(s for s in STEPS if s.get("id") == "body")
        self.assertIn("gh api", body["run"])
        self.assertIn("pulls/${NUMBER}", body["run"])
        self.assertEqual(body["env"]["GH_TOKEN"], "${{ github.token }}")
        self.assertEqual(STEPS[-1]["env"]["BODY_FILE"], "${{ steps.body.outputs.file }}")
        self.assertNotIn("BODY", [k for k in STEPS[-1]["env"]])
        self.assertIn("--check", STEPS[-1]["run"])
        wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        self.assertEqual(wf["permissions"], {"contents": "read", "pull-requests": "read"})
        lint = yaml.safe_load((WORKFLOW.parent / "workflow-lint.yml").read_text(encoding="utf-8"))
        self.assertEqual(lint["jobs"]["depends-on"]["permissions"], {"contents": "read", "pull-requests": "read"})

    def test_with_no_pull_request_the_job_passes_with_a_notice_and_every_other_step_is_skipped(self) -> None:
        has_pr = "${{ github.event.pull_request.number != '' }}"
        notice = STEPS[0]
        self.assertEqual(notice["if"], "${{ github.event.pull_request.number == '' }}")
        self.assertIn("::notice::not a pull request: nothing to wait for", notice["run"])
        for step in STEPS[1:]:  # the body (gh api repos/<repo>/pulls/), both tokens, the docs wait and the check
            guard = step.get("if", "")
            name = step.get("name", step.get("uses"))
            self.assertTrue(guard.startswith("${{ github.event.pull_request.number != '' ") or guard == has_pr, name)
        self.assertEqual(next(s for s in STEPS if s.get("id") == "body")["if"], has_pr)
        self.assertEqual(STEPS[-1]["if"], has_pr)
        on = yaml.safe_load((WORKFLOW.parent / "workflow-lint.yml").read_text(encoding="utf-8"))[True]
        self.assertEqual(on["push"]["branches"], ["main"])  # this repo's own caller is fine on push

    def test_the_docs_pin_gets_its_own_token_read_only_on_onepeek_docs(self) -> None:  # L27 (a)
        deps = next(s for s in STEPS if s.get("id") == "deps")
        self.assertIn('--docs-pin', deps["run"])
        docs = next(s for s in STEPS if s.get("id") == "docs-app")
        self.assertEqual(docs["if"], "${{ github.event.pull_request.number != '' && steps.deps.outputs.docs != '' }}")
        self.assertEqual(docs["with"]["repositories"], "onepeek-docs")
        self.assertEqual({k: v for k, v in docs["with"].items() if k.startswith("permission-")},
                         {"permission-contents": "read", "permission-pull-requests": "read"})
        self.assertEqual(STEPS[-1]["env"]["DOCS_TOKEN"], "${{ steps.docs-app.outputs.token }}")
        guard = next(s for s in STEPS if "HAS_APP" in s.get("env", {}))
        self.assertIn("steps.deps.outputs.docs != ''", guard["if"])

    def test_the_callers_docs_version_file_is_checked_out(self) -> None:
        # Cone-mode sparse checkout always keeps the root's files, so docs-version.txt is there; never turn cone off.
        self.assertNotIn("sparse-checkout-cone-mode", STEPS[1]["with"])

    def test_the_callers_in_this_repo_run_it_first_and_every_other_job_needs_it(self) -> None:
        jobs = yaml.safe_load((WORKFLOW.parent / "workflow-lint.yml").read_text(encoding="utf-8"))["jobs"]
        self.assertEqual(jobs["depends-on"]["uses"], "./.github/workflows/depends-on.yml")
        for name, job in jobs.items():
            if name != "depends-on":
                self.assertEqual(job.get("needs"), "depends-on", name)
        on = yaml.safe_load((WORKFLOW.parent / "workflow-lint.yml").read_text(encoding="utf-8"))[True]
        self.assertNotIn("edited", on["pull_request"]["types"])  # L27 (d): the ship tool re-runs instead
        self.assertEqual(on["pull_request"]["types"], ["opened", "reopened", "synchronize"])


if __name__ == "__main__":
    unittest.main()


class DraftGate(unittest.TestCase):
    """development-cycle §2c step 2: a pull request with an open dependency is a draft (a notice; ship_change_set.py enforces)."""

    OPEN, MERGED = staticmethod(lambda r, n: ("open", False)), staticmethod(lambda r, n: ("closed", True))

    def test_a_non_draft_with_an_open_dependency_gets_one_message_per_dependency(self) -> None:
        deps = [("backend-service", 73), ("onepeek-web", 5)]
        out = cd.draft_gate(deps, False, self.OPEN)
        self.assertEqual(len(out), 2)
        self.assertIn("backend-service#73", out[0])
        self.assertIn("draft", out[0])

    def test_a_draft_a_merged_dependency_or_an_unknown_flag_is_quiet(self) -> None:
        deps = [("backend-service", 73)]
        self.assertEqual(cd.draft_gate(deps, True, self.OPEN), [])
        self.assertEqual(cd.draft_gate(deps, False, self.MERGED), [])
        self.assertEqual(cd.draft_gate(deps, None, self.OPEN), [])

    def test_check_prints_it_as_a_notice_and_still_passes_only_on_its_own_merits(self) -> None:
        env = {"BODY": "Depends on: OnePeek/backend-service#73", "IS_DRAFT": "false", "GH_TOKEN": "t"}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(cd, "api_state", return_value=("open", False)), \
                contextlib.redirect_stdout(out), tempfile.TemporaryDirectory() as d:
            code = cd.main(["--check", "--repo-dir", d])
        self.assertEqual(code, 1)  # waiting for the dependency fails as before
        self.assertIn("::notice title=depends-on::backend-service#73 is still open", out.getvalue())

    def test_the_workflow_passes_the_draft_flag(self) -> None:
        step = next(s for s in STEPS if s.get("name", "").startswith("Every dependency is merged"))
        self.assertIn("github.event.pull_request.draft", step["env"]["IS_DRAFT"])

    def test_an_api_error_in_the_notice_path_never_fails_the_job(self) -> None:
        env = {"BODY": "Depends on: OnePeek/backend-service#73", "IS_DRAFT": "false", "GH_TOKEN": "t"}
        calls = iter([("closed", True)])  # the wait sees it merged; the notice path's own read then fails

        def state(r, n, t):
            try:
                return next(calls)
            except StopIteration:
                raise SystemExit("::error::cannot read backend-service#73: HTTP 502")
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(cd, "api_state", side_effect=state), \
                contextlib.redirect_stdout(out), tempfile.TemporaryDirectory() as d:
            code = cd.main(["--check", "--repo-dir", d])
        self.assertEqual(code, 0)
        self.assertIn("draft check skipped", out.getvalue())
