"""Tests for workspace/preflight.py (foundation-playbook §8a P4): the refusal, tool versions, tokens, release state."""
from __future__ import annotations

import importlib.util
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("preflight", ROOT / "workspace" / "preflight.py")
pf = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pf)

APP_STEP = """name: w
on: [pull_request]
jobs:
  contract:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/create-github-app-token@{sha}
        with:
          client-id: ${{{{ secrets.AGENTS_APP_CLIENT_ID }}}}
          repositories: backend-service,onepeek-infra
          permission-contents: read
          permission-pull-requests: read
""".format(sha="1" * 40)


def statuses(rep: pf.Report) -> list[str]:
    return [s for s, _ in rep.lines]


def api(answers: dict):
    """A fake `gh api`: the first answer whose key is a prefix of the path."""
    def call(path: str):
        for k, v in answers.items():
            if path.startswith(k):
                return v
        return False, f"unexpected {path}"
    return call


class Repo(unittest.TestCase):
    def repo(self, files: dict[str, str]) -> pathlib.Path:
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        subprocess.run(["git", "init", "-q", str(d)], check=True)
        (d / ".github/workflows").mkdir(parents=True)
        for name, text in files.items():
            (d / name).parent.mkdir(parents=True, exist_ok=True)
            (d / name).write_text(text)
        subprocess.run(["git", "-C", str(d), "add", "."], check=True)
        return d


class Refusal(Repo):
    def test_refuses_unless_local_ci_ends_local_ci_ok_and_runs_nothing_else(self) -> None:
        d = self.repo({".github/workflows/w.yml": "on: [pull_request]\njobs: {}\n"})
        for rc, out in ((1, "FAIL  x\n"), (0, "LOCAL CI OK (3s)\nsomething after\n"), (0, "")):
            with mock.patch.object(pf.subprocess, "run", return_value=subprocess.CompletedProcess([], rc, out, "")), \
                    mock.patch.object(pf, "dependency_check", return_value=True), \
                    mock.patch.object(pf, "pin_checks") as pins:
                self.assertEqual(pf.main([str(d)]), 1, out)
            pins.assert_not_called()

    def test_goes_on_after_local_ci_ok(self) -> None:
        d = self.repo({".github/workflows/w.yml": "on: [pull_request]\njobs: {}\n"})
        ok = subprocess.CompletedProcess([], 0, "PASS a\n\nLOCAL CI OK (3s)\n", "")
        with mock.patch.object(pf, "run_local_ci", return_value=(True, "LOCAL CI OK (3s)")), \
                mock.patch.object(pf, "dependency_check", return_value=True), \
                mock.patch.object(pf, "pin_checks") as pins, mock.patch.object(pf, "tool_checks"):
            self.assertEqual(pf.main([str(d)]), 0)
        pins.assert_called_once()
        with mock.patch.object(pf.subprocess, "run", return_value=ok):
            self.assertEqual(pf.run_local_ci(d), (True, "LOCAL CI OK (3s)"))


class Tools(Repo):
    def test_a_mismatch_with_a_version_file_fails_and_a_match_passes(self) -> None:
        d = self.repo({".github/workflows/w.yml": "jobs: {}\n", ".terraform-version": "1.16.6\n",
                       ".python-version": "3.13\n", "web/.node-version": "24\n"})
        have = {"terraform": "1.17.0", "python": "3.13.5", "node": "24.7.0"}
        rep = pf.Report()
        with mock.patch.object(pf.local_ci, "uv_problem", return_value=""):
            pf.tool_checks(d, rep, version=lambda kind, want, cwd: have[kind])
        got = {t.split()[1]: s for s, t in rep.lines}
        self.assertEqual(got["terraform"], "FAIL")
        self.assertEqual(got["python"], "PASS")
        self.assertEqual(got["node"], "PASS")

    def test_a_missing_tool_fails(self) -> None:
        d = self.repo({".github/workflows/w.yml": "jobs: {}\n", ".node-version": "24\n"})
        rep = pf.Report()
        with mock.patch.object(pf.local_ci, "uv_problem", return_value=""):
            pf.tool_checks(d, rep, version=lambda *a: None)
        self.assertEqual(statuses(rep)[0], "FAIL")

    def test_uv_mismatch_fails_and_tflint_mismatch_warns(self) -> None:
        d = self.repo({".github/workflows/w.yml": "jobs:\n  t:\n    steps:\n      - run: tflint\n"})
        rep = pf.Report()
        with mock.patch.object(pf.local_ci, "uv_problem", return_value="uv 0.9.7 is installed"), \
                mock.patch.object(pf.local_ci, "tflint_problem", return_value="tflint 0.50.0 is installed; CI pins 0.65.0"):
            pf.tool_checks(d, rep)
        self.assertEqual(statuses(rep), ["FAIL", "WARN"])

    def test_versions_match_by_prefix_only_on_a_dot(self) -> None:
        self.assertTrue(pf.matches("3.13.5", "3.13"))
        self.assertFalse(pf.matches("3.130.0", "3.13"))
        self.assertFalse(pf.matches("1.16.7", "1.16.6"))


class Tokens(unittest.TestCase):
    TEXTS = [("w.yml", APP_STEP), ("x.yml", "jobs:\n  j:\n    env:\n      T: ${{ secrets.NPM_READ_TOKEN }}\n")]

    def test_the_requests_are_read_from_the_steps(self) -> None:
        (r,) = pf.app_requests(self.TEXTS, "backend-service")
        self.assertEqual(r["permissions"], {"contents": "read", "pull_requests": "read"})
        self.assertEqual(r["repositories"], ["backend-service", "onepeek-infra"])

    def test_a_permission_the_app_lacks_fails(self) -> None:  # the release-candidate lookup needs pull requests read
        rep = pf.Report()
        inst = {"installations": [{"id": 7, "app_slug": pf.APP_SLUG, "repository_selection": "all",
                                   "permissions": {"actions": "read", "metadata": "read", "contents": "read"}}]}
        pf.token_checks(self.TEXTS, "backend-service", rep, api=api({"orgs/": (True, inst)}))
        fails = [t for s, t in rep.lines if s == "FAIL"]
        self.assertEqual(len(fails), 1)
        self.assertIn("pull_requests: read (has none)", fails[0])
        self.assertIn(("UNVERIFIED", "secrets: AGENTS_APP_CLIENT_ID, NPM_READ_TOKEN cannot be read from a laptop; "
                                     "check they are set in backend-service"), rep.lines)

    def test_granted_and_installed_passes(self) -> None:
        rep = pf.Report()
        inst = {"installations": [{"id": 7, "app_slug": pf.APP_SLUG, "repository_selection": "selected",
                                   "permissions": {"contents": "read", "pull_requests": "write"}}]}
        repos = {"repositories": [{"name": "backend-service"}, {"name": "onepeek-infra"}]}
        pf.token_checks(self.TEXTS[:1], "backend-service", rep, api=api({"orgs/": (True, inst), "user/installations/7": (True, repos)}))
        self.assertEqual([s for s in statuses(rep) if s != "UNVERIFIED"], ["PASS", "PASS"])

    def test_a_repository_the_app_is_not_installed_on_fails(self) -> None:
        rep = pf.Report()
        inst = {"installations": [{"id": 7, "app_slug": pf.APP_SLUG, "repository_selection": "selected",
                                   "permissions": {"contents": "read", "pull_requests": "read"}}]}
        repos = {"repositories": [{"name": "backend-service"}]}
        pf.token_checks(self.TEXTS[:1], "backend-service", rep, api=api({"orgs/": (True, inst), "user/installations/7": (True, repos)}))
        self.assertTrue(any(s == "FAIL" and "onepeek-infra" in t for s, t in rep.lines), rep.lines)

    def test_without_org_admin_read_installations_are_unverified_and_app_permissions_still_checked(self) -> None:
        rep = pf.Report()
        pf.token_checks(self.TEXTS[:1], "backend-service", rep, api=api({
            "orgs/": (False, "HTTP 403"), "apps/": (True, {"permissions": {"actions": "read", "metadata": "read"}})}))
        self.assertIn("UNVERIFIED", statuses(rep))
        self.assertIn("FAIL", statuses(rep))  # contents and pull requests are not granted


class ReleaseState(unittest.TestCase):
    """§8a P4: stale = the second parent of the newest merge commit reachable from release is not on dev."""
    R = "repos/OnePeek/onepeek-web/"
    MERGE, MERGED, HOTFIX = "m" * 40, "2" * 40, "h" * 40

    def state(self, prs, release_on_dev=False, commits=(), on_dev=()) -> list[tuple[str, str]]:
        """`commits`: release's history newest first, as parent counts; `on_dev`: the commits that are on dev."""
        answers = {self.R + "pulls": (True, prs),
                   self.R + "compare/release...dev": (True, {"status": "ahead" if release_on_dev else "diverged"})}
        answers.update({self.R + f"compare/{c}...dev": (True, {"status": "behind"}) for c in (self.MERGED,)})
        answers.update({self.R + f"compare/{c}...dev": (True, {"status": "ahead"}) for c in on_dev})
        history = [{"sha": sha, "parents": [{"sha": "1" * 40}] + ([{"sha": self.MERGED}] if n > 1 else [])}
                   for sha, n in commits]
        answers[self.R + "commits?sha=release"] = (True, history)
        rep = pf.Report()
        seen = []
        call = api(answers)
        pf.release_state("onepeek-web", rep, api=lambda path: seen.append(path) or call(path))
        self.seen = seen
        return rep.lines

    def only(self, lines) -> str:
        self.assertEqual(len(lines), 1, lines)
        return lines[0][0]

    def test_more_than_one_open_release_pull_request_fails(self) -> None:
        pr = {"number": 1, "head": {"sha": "a" * 40}}
        self.assertEqual(self.only(self.state([pr, pr])), "FAIL")

    def test_one_open_release_pull_request_passes(self) -> None:
        self.assertEqual(self.only(self.state([{"number": 1, "head": {"sha": "a" * 40}}])), "PASS")

    def test_a_merge_whose_second_parent_is_on_dev_passes(self) -> None:
        self.assertEqual(self.only(self.state([], commits=[(self.MERGE, 2)], on_dev=[self.MERGED])), "PASS")
        self.assertIn(self.R + f"compare/{self.MERGED}...dev", self.seen)

    def test_a_merge_whose_second_parent_is_off_dev_fails(self) -> None:
        lines = self.state([], commits=[(self.MERGE, 2)])
        self.assertEqual(self.only(lines), "FAIL")
        self.assertIn("stale", lines[0][1])

    def test_release_fast_forwarded_onto_dev_passes(self) -> None:
        for status in ("ahead", "identical"):
            rep = pf.Report()
            pf.release_state("onepeek-web", rep, api=api({self.R + "pulls": (True, []),
                                                          self.R + "compare/release...dev": (True, {"status": status})}))
            self.assertEqual(statuses(rep), ["PASS"], status)

    def test_a_hotfix_squashed_after_the_merge_passes(self) -> None:
        lines = self.state([], commits=[(self.HOTFIX, 1), (self.MERGE, 2), ("o" * 40, 2)], on_dev=[self.MERGED])
        self.assertEqual(self.only(lines), "PASS")
        self.assertIn(self.MERGE[:7], lines[0][1])  # the newest merge, not the hotfix

    def test_no_merge_and_not_on_dev_fails_with_no_release_merge_found(self) -> None:
        lines = self.state([], commits=[(self.HOTFIX, 1), ("o" * 40, 1)])
        self.assertEqual(self.only(lines), "FAIL")
        self.assertIn("no release merge found", lines[0][1])

    def test_the_history_is_walked_past_the_first_page(self) -> None:
        page1 = [{"sha": f"{i:040x}", "parents": [{"sha": "1" * 40}]} for i in range(100)]
        page2 = [{"sha": self.MERGE, "parents": [{"sha": "1" * 40}, {"sha": self.MERGED}]}]
        rep = pf.Report()
        pf.release_state("onepeek-web", rep, api=api({
            self.R + "pulls": (True, []), self.R + "compare/release...dev": (True, {"status": "diverged"}),
            self.R + "commits?sha=release&per_page=100&page=1": (True, page1),
            self.R + "commits?sha=release&per_page=100&page=2": (True, page2),
            self.R + f"compare/{self.MERGED}...dev": (True, {"status": "ahead"})}))
        self.assertEqual(statuses(rep), ["PASS"])

    def test_an_unreadable_release_fails(self) -> None:
        rep = pf.Report()
        pf.release_state("onepeek-web", rep, api=api({self.R + "pulls": (True, []), self.R + "compare": (False, "HTTP 404")}))
        self.assertEqual(statuses(rep), ["FAIL"])

    def test_the_repos_read_are_the_contract_and_lock_sides_but_not_itself(self) -> None:
        contract = [("c.yml", "uses: OnePeek/.github/.github/workflows/contract.yml@" + "a" * 40)]
        self.assertEqual(pf.release_repos("onepeek-web", contract), ["backend-service", "onepeek-infra"])
        lock = [("r.yml", "uses: OnePeek/.github/.github/workflows/release-ecs.yml@" + "a" * 40)]
        self.assertEqual(pf.release_repos("backend-service", lock), ["mcp-athena", "onepeek-infra", "onepeek-web"])
        self.assertEqual(pf.release_repos("onepeek-docs", [("s.yml", "secret-scan")]), [])
        lower = [("c.yml", "uses: onepeek/.github/.github/workflows/contract.yml@" + "a" * 40)]
        self.assertEqual(pf.release_repos("onepeek-web", lower), ["backend-service", "onepeek-infra"])


class DependsOn(Repo):
    """§8a P1 (lesson L26): no push while a declared dependency is unmerged, unless the pull request is a draft."""

    def check(self, prs, deps_state, given=()) -> tuple[bool, list[str]]:
        d = self.repo({".github/workflows/w.yml": "jobs: {}\n"})
        rep = pf.Report()
        answers = {"repos/OnePeek/onepeek-infra/pulls?": (True, prs)}
        answers.update({f"repos/OnePeek/{r}/pulls/{n}": (True, s) for (r, n), s in deps_state.items()})
        ok = pf.dependency_check(d, "onepeek-infra", list(given), rep, api=api(answers))
        return ok, statuses(rep)

    OPEN = {"state": "open", "merged": False}
    MERGED = {"state": "closed", "merged": True}

    def test_an_unmerged_dependency_in_the_pull_request_body_refuses_the_push(self) -> None:
        pr = [{"number": 122, "draft": False, "body": "Depends on: OnePeek/backend-service#73"}]
        self.assertEqual(self.check(pr, {("backend-service", 73): self.OPEN}), (False, ["FAIL"]))

    def test_a_draft_pull_request_may_push_with_a_warning(self) -> None:
        pr = [{"number": 122, "draft": True, "body": "Depends on: OnePeek/backend-service#73"}]
        self.assertEqual(self.check(pr, {("backend-service", 73): self.OPEN}), (True, ["WARN"]))

    def test_a_merged_dependency_passes(self) -> None:
        pr = [{"number": 122, "draft": False, "body": "Depends on: OnePeek/backend-service#73"}]
        self.assertEqual(self.check(pr, {("backend-service", 73): self.MERGED}), (True, ["PASS"]))

    def test_before_a_pull_request_exists_the_flag_is_the_source(self) -> None:
        self.assertEqual(self.check([], {("backend-service", 73): self.OPEN}, ["OnePeek/backend-service#73"]), (False, ["FAIL"]))
        self.assertEqual(self.check([], {}), (True, ["PASS"]))

    def test_release_reaches_local_ci_and_a_plain_run_does_not_ask_for_it(self) -> None:
        d = self.repo({".github/workflows/w.yml": "jobs: {}\n"})
        for release in (True, False):
            with mock.patch.object(pf.subprocess, "run") as run:
                run.return_value.returncode, run.return_value.stdout, run.return_value.stderr = 0, "LOCAL CI OK (1s)\n", ""
                self.assertEqual(pf.run_local_ci(d, release)[0], True)
            self.assertEqual("--release" in run.call_args[0][0], release)

    def test_main_refuses_before_local_ci(self) -> None:
        d = self.repo({".github/workflows/w.yml": "jobs: {}\n"})
        with mock.patch.object(pf, "dependency_check", return_value=False), \
                mock.patch.object(pf, "run_local_ci") as lci:
            self.assertEqual(pf.main([str(d), "--depends-on", "OnePeek/backend-service#73"]), 1)
        lci.assert_not_called()


class DocsWait(Repo):
    """Lesson L27 (a): the docs-pin wait is a note locally, a failure with --release; a pin in no pull request fails."""
    PIN = "a" * 40

    def check(self, strict: bool, status: str, prs: list[int]) -> list[str]:
        d = self.repo({".github/workflows/w.yml": "jobs: {}\n", "docs-version.txt": f"onepeek-docs@{self.PIN}\n"})
        rep = pf.Report()
        pf.docs_wait_check(d, rep, strict, status=lambda s: status, prs=lambda s: prs)
        return statuses(rep)

    def test_waiting_for_the_open_docs_pull_request_is_a_note(self) -> None:
        self.assertEqual(self.check(False, "diverged", [145]), ["NOTE"])

    def test_with_release_it_fails(self) -> None:
        self.assertEqual(self.check(True, "diverged", [145]), ["FAIL"])

    def test_a_pin_in_no_pull_request_fails_either_way(self) -> None:
        self.assertEqual(self.check(False, "diverged", []), ["FAIL"])

    def test_an_api_error_is_unverified_not_an_abort(self) -> None:  # S6
        d = self.repo({".github/workflows/w.yml": "jobs: {}\n", "docs-version.txt": f"onepeek-docs@{self.PIN}\n"})
        rep = pf.Report()

        def boom(s):
            raise SystemExit("cannot compare: HTTP 401")
        pf.docs_wait_check(d, rep, False, status=boom, prs=lambda s: [])
        self.assertEqual(statuses(rep), ["UNVERIFIED"])

    def test_with_release_a_read_error_fails(self) -> None:  # R2-3
        d = self.repo({".github/workflows/w.yml": "jobs: {}\n", "docs-version.txt": f"onepeek-docs@{self.PIN}\n"})

        def boom(s):
            raise SystemExit("cannot compare: HTTP 502")
        for strict, want in ((False, ["UNVERIFIED"]), (True, ["FAIL"])):
            rep = pf.Report()
            pf.docs_wait_check(d, rep, strict, status=boom, prs=lambda s: [])
            self.assertEqual(statuses(rep), want)

    def test_main_is_strict_only_with_release(self) -> None:  # kills strict=a.release -> False
        d = self.repo({".github/workflows/w.yml": "jobs: {}\n"})
        for flags, strict in (([], False), (["--release"], True)):
            with mock.patch.object(pf, "dependency_check", return_value=True), \
                    mock.patch.object(pf, "run_local_ci", return_value=(True, "LOCAL CI OK")) as lci, \
                    mock.patch.object(pf, "pin_checks"), mock.patch.object(pf, "tool_checks"), \
                    mock.patch.object(pf, "docs_check"), mock.patch.object(pf, "token_checks"), \
                    mock.patch.object(pf, "release_repos", return_value=[]), \
                    mock.patch.object(pf, "docs_wait_check") as dw:
                pf.main([str(d), *flags])
            self.assertEqual(dw.call_args.kwargs["strict"], strict, flags)
            self.assertEqual(lci.call_args[0][1], strict, flags)  # --release reaches local CI

    def test_a_pin_on_main_prints_nothing(self) -> None:
        self.assertEqual(self.check(True, "ahead", []), [])


if __name__ == "__main__":
    unittest.main()
