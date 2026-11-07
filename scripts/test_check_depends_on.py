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
                self.assertIn("docs pin aaaaaaa not on docs dev and in no open docs PR", problems[0])

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

    def test_a_pin_on_docs_dev_passes_and_says_so(self) -> None:  # decision 176: the trunk is dev
        rc, out = self.run_main(["--check"], "", pin=PIN, status="ahead")
        self.assertEqual(rc, 0)
        self.assertIn("docs-version.txt pins aaaaaaa, on onepeek-docs dev.", out)
        self.assertNotIn("main", out)

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
        self.assertIn("not on docs dev and in no open docs PR", out)

    def test_the_docs_compare_reads_docs_trunk_and_an_unreadable_trunk_fails(self) -> None:  # decision 176
        (self.dir / "docs-version.txt").write_text(f"onepeek-docs@{PIN}\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"BODY": "", "GH_TOKEN": "t", "DOCS_TOKEN": "d"}), \
                contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(cd.check_pins, "docs_trunk",
                                  side_effect=SystemExit("cannot read the default branch of onepeek-docs: HTTP 404")):
            with self.assertRaises(SystemExit) as e:
                cd.main(["--check", "--repo-dir", str(self.dir)])
        self.assertIn("cannot read the default branch of onepeek-docs", str(e.exception.code))

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
        self.assertEqual(on["push"]["branches"], ["dev"])  # this repo's own caller is fine on push (decision 176)

    def test_the_docs_pin_gets_its_own_token_read_only_on_onepeek_docs(self) -> None:  # L27 (a)
        deps = next(s for s in STEPS if s.get("id") == "deps")
        self.assertIn('--docs-read', deps["run"])
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


DEPENDABOT_ENV = {"PR_AUTHOR": "dependabot[bot]", "GITHUB_ACTOR": "dependabot[bot]", "GITHUB_EVENT_NAME": "pull_request"}


class Dependabot(unittest.TestCase):  # pitfall 8: Dependabot runs get no Actions secrets, so no App token
    def setUp(self) -> None:
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        (self.dir / "docs-version.txt").write_text(f"onepeek-docs@{PIN}\n", encoding="utf-8")

    def run_main(self, argv: list[str], env: dict[str, str], body: str = "", files: list[str] | None = None,
                 docs_token: str = "") -> tuple[int, str]:
        env = {"BODY": body, "GH_TOKEN": "", "DOCS_TOKEN": docs_token, **env}
        if files is not None:
            (self.dir / "files.txt").write_text("\n".join(files) + "\n", encoding="utf-8")
            env["CHANGED_FILES"] = str(self.dir / "files.txt")
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("PR_AUTHOR", "GITHUB_ACTOR", "GITHUB_EVENT_NAME", "CHANGED_FILES", "BODY_FILE", "IS_DRAFT")}
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {**clean, **env}, clear=True), contextlib.redirect_stdout(buf), \
                mock.patch.object(cd, "api_state", side_effect=lambda r, n, t: ("open", False)), \
                mock.patch.object(cd.check_pins, "compare_status", side_effect=AssertionError("no docs read")), \
                mock.patch.object(cd, "open_docs_prs", side_effect=AssertionError("no docs read")):
            rc = cd.main([*argv, "--repo-dir", str(self.dir)])
        return rc, buf.getvalue()

    def test_a_dependabot_bump_needs_no_read_and_passes_with_a_notice(self) -> None:
        files = [".github/workflows/prerelease.yml"]  # onepeek-design-system#71
        rc, out = self.run_main(["--check"], DEPENDABOT_ENV, files=files)
        self.assertEqual(rc, 0, out)
        self.assertIn("::notice title=depends-on::Dependabot pull request with no `Depends on:` line that does not "
                      "change docs-version.txt: no cross-repo read needed", out)
        self.assertNotIn("::error", out)
        self.assertEqual(self.run_main(["--docs-read"], DEPENDABOT_ENV, files=files), (0, "\n"))
        self.assertEqual(self.run_main(["--docs-pin"], DEPENDABOT_ENV, files=files), (0, PIN + "\n"))

    def test_a_human_pull_request_made_to_look_like_dependabot_still_needs_the_token(self) -> None:
        body = "Bumps pnpm/action-setup from 6.1.0 to 6.1.1.\n---\nupdated-dependencies:\n- dependency-name: x\n"
        for env in ({**DEPENDABOT_ENV, "PR_AUTHOR": "karan"},          # branch dependabot/..., title chore(deps): bump
                    {**DEPENDABOT_ENV, "PR_AUTHOR": "dependabot"},     # a user named like the bot
                    {**DEPENDABOT_ENV, "GITHUB_ACTOR": "karan"},       # a human pushed to Dependabot's branch
                    {**DEPENDABOT_ENV, "GITHUB_EVENT_NAME": "pull_request_target"},
                    {"PR_AUTHOR": "dependabot[bot]"}, {}):
            rc, out = self.run_main(["--check"], env, body=body, files=["package.json"])
            self.assertEqual(rc, 1, env)
            self.assertIn("::error::no token to read onepeek-docs", out)
            self.assertEqual(self.run_main(["--docs-read"], env, files=["package.json"]), (0, PIN + "\n"), env)
        rc, out = self.run_main(["--check"], {**DEPENDABOT_ENV, "PR_AUTHOR": "karan"}, files=["package.json"])
        self.assertNotIn("Dependabot secrets", out)  # the Dependabot hint names only Dependabot's own runs

    def test_a_dependabot_pull_request_that_adds_a_depends_on_line_needs_the_token(self) -> None:
        body = "Bump.\nDepends on: OnePeek/onepeek-docs#7\n"
        self.assertEqual(self.run_main(["--repos"], DEPENDABOT_ENV, body=body, files=["package.json"]), (0, "onepeek-docs\n"))
        self.assertEqual(self.run_main(["--docs-read"], DEPENDABOT_ENV, body=body, files=["package.json"]), (0, PIN + "\n"))
        rc, out = self.run_main(["--check"], DEPENDABOT_ENV, body=body, files=["package.json"])
        self.assertEqual(rc, 1)
        self.assertIn("set AGENTS_APP_CLIENT_ID and AGENTS_APP_PRIVATE_KEY as Dependabot secrets "
                      "(gh secret set --app dependabot)", out)

    def test_a_dependabot_pull_request_that_changes_docs_version_needs_the_token(self) -> None:
        for files in (["docs-version.txt"], ["package.json", "docs-version.txt"]):
            self.assertEqual(self.run_main(["--docs-read"], DEPENDABOT_ENV, files=files), (0, PIN + "\n"))
            rc, out = self.run_main(["--check"], DEPENDABOT_ENV, files=files)
            self.assertEqual(rc, 1, files)
            self.assertIn("as Dependabot secrets (gh secret set --app dependabot)", out)

    def test_an_unknown_file_list_never_exempts(self) -> None:  # fail closed
        for env in (DEPENDABOT_ENV, {**DEPENDABOT_ENV, "CHANGED_FILES": str(self.dir / "missing.txt")}):
            self.assertEqual(self.run_main(["--docs-read"], env), (0, PIN + "\n"))
            self.assertEqual(self.run_main(["--check"], env)[0], 1)

    def test_with_the_token_a_dependabot_pull_request_that_needs_a_read_is_checked_as_any_other(self) -> None:
        buf = io.StringIO()
        (self.dir / "files.txt").write_text("docs-version.txt\n", encoding="utf-8")
        env = {**DEPENDABOT_ENV, "BODY": "", "DOCS_TOKEN": "d", "CHANGED_FILES": str(self.dir / "files.txt")}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(buf), \
                mock.patch.object(cd.check_pins, "compare_status", return_value="diverged"), \
                mock.patch.object(cd, "open_docs_prs", return_value=[]):
            rc = cd.main(["--check", "--repo-dir", str(self.dir)])
        self.assertEqual(rc, 1)
        self.assertIn("not on docs dev and in no open docs PR", buf.getvalue())

    def test_the_workflow_lists_files_only_for_dependabots_own_run_and_passes_them_on(self) -> None:
        files = next(s for s in STEPS if s.get("id") == "files")
        for cond in ("github.event_name == 'pull_request'", "github.event.pull_request.user.login == 'dependabot[bot]'"):
            self.assertIn(cond, files["if"])
        self.assertNotIn("github.actor", files["if"])  # zizmor bot-conditions; the script checks GITHUB_ACTOR as well
        self.assertIn('gh api --paginate "repos/${REPO}/pulls/${NUMBER}/files"', files["run"])
        self.assertIn(".previous_filename", files["run"])
        names = [s.get("id") for s in STEPS]
        self.assertLess(names.index("files"), names.index("deps"))
        for step in (next(s for s in STEPS if s.get("id") == "deps"), STEPS[-1]):
            self.assertEqual(step["env"]["CHANGED_FILES"], "${{ steps.files.outputs.file }}")
            self.assertEqual(step["env"]["PR_AUTHOR"], "${{ github.event.pull_request.user.login }}")
        guard = next(s for s in STEPS if "HAS_APP" in s.get("env", {}))
        self.assertIn("gh secret set --app dependabot", guard["run"])
        self.assertEqual(guard["env"]["ACTOR"], "${{ github.actor }}")


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
