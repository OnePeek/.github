"""Tests for release_gate.py (delivery-pipelines.md §8.1 "Tests"): a fake `gh` on PATH and real throwaway Git repos."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("release_gate", HERE / "release_gate.py")
rg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rg)

REPO = "OnePeek/backend-service"
WORKFLOWS = ["checks.yml", "image-smoke.yml"]

FAKE_GH = textwrap.dedent("""\
    #!/usr/bin/env python3
    # A stand-in for `gh api -X GET <path>`: answers from responses.json; a list answers in turn (the last repeats).
    import json, os, pathlib, sys
    d = pathlib.Path(os.environ["FAKE_GH_DIR"])
    args = sys.argv[1:]
    if args[:3] != ["api", "-X", "GET"] or len(args) != 4:
        sys.stderr.write("fake gh: unexpected arguments %r\\n" % (args,)); sys.exit(2)
    path = args[3]
    with open(d / "calls.log", "a") as f:
        f.write(path + "\\n")
    responses = json.loads((d / "responses.json").read_text())
    if path not in responses:
        sys.stderr.write("gh: Not Found (HTTP 404)\\n"); sys.exit(1)
    answers = responses[path]
    counts = json.loads((d / "counts.json").read_text()) if (d / "counts.json").exists() else {}
    n = counts.get(path, 0)
    counts[path] = n + 1
    (d / "counts.json").write_text(json.dumps(counts))
    answer = answers[min(n, len(answers) - 1)]
    if answer.get("exit", 0) != 0:
        sys.stdout.write(json.dumps(answer.get("body", {"message": "Server Error"})))
        sys.stderr.write("gh: Server Error (HTTP 502)\\n"); sys.exit(answer["exit"])
    sys.stdout.write(json.dumps(answer["body"]) if "body" in answer else answer["raw"])
""")


def clean_git_env() -> dict[str, str]:
    """Git's repository-local variables cleared (pitfall 27), and no user or system config."""
    env = dict(os.environ)
    out = subprocess.run(["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True).stdout.split()
    for name in out:
        env.pop(name, None)
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    return env


class Fixture(unittest.TestCase):
    """A remote with `dev` and `release`, a fake gh, and helpers to describe runs."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = clean_git_env()
        (self.tmp / "gitconfig").write_text("")
        self.env["GIT_CONFIG_GLOBAL"] = str(self.tmp / "gitconfig")
        self.ghdir = self.tmp / "gh"
        self.ghdir.mkdir()
        (self.ghdir / "gh").write_text(FAKE_GH)
        (self.ghdir / "gh").chmod(0o755)
        self.env["FAKE_GH_DIR"] = str(self.ghdir)
        self.env["PATH"] = f"{self.ghdir}{os.pathsep}{self.env['PATH']}"
        self.env.pop("GITHUB_STEP_SUMMARY", None)
        patcher = mock.patch.dict(os.environ, self.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.remote = self.tmp / "remote.git"
        self.work = self.tmp / "work"
        self.git(self.tmp, "init", "-q", "--bare", str(self.remote))
        self.git(self.tmp, "init", "-q", "-b", "dev", str(self.work))
        self.write("app.txt", "one\n")
        self.commit("first")
        self.git(self.work, "branch", "release")
        self.responses: dict[str, list] = {}
        self.pr(head_ref="dev")
        self.sleeps: list[float] = []
        self.now = 0.0

    # ---- git
    def git(self, cwd, *args) -> str:
        return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
                              env=self.env).stdout.strip()

    def write(self, name, text):
        (self.work / name).write_text(text)

    def commit(self, msg, allow_empty=False) -> str:
        self.git(self.work, "add", "-A")
        self.git(self.work, "commit", "-q", "-m", msg, *(["--allow-empty"] if allow_empty else []))
        return self.git(self.work, "rev-parse", "HEAD")

    def push(self):
        self.git(self.work, "push", "-q", "-f", str(self.remote), "dev", "release")

    def head(self) -> str:
        return self.git(self.work, "rev-parse", "dev")

    # ---- gh
    def answer(self, path, *answers):
        self.responses[path] = list(answers)

    def pr(self, head_ref="dev", base="release", head_repo=REPO, sha=None):
        self.answer(f"repos/{REPO}/pulls/7", {"body": {
            "base": {"ref": base}, "head": {"ref": head_ref, "sha": sha or "0" * 40,
                                             "repo": {"full_name": head_repo} if head_repo else None}}})

    def runs_path(self, wf, sha):
        return f"repos/{REPO}/actions/workflows/{wf}/runs?head_sha={sha}&branch=dev&per_page=100&page=1"

    def jobs_path(self, run_id):
        return f"repos/{REPO}/actions/runs/{run_id}/jobs?filter=latest&per_page=100&page=1"

    @staticmethod
    def wrun(run_id, sha, status="completed", conclusion="success", event="push", created="2026-11-06T10:00:00Z"):
        return {"id": run_id, "head_sha": sha, "head_branch": "dev", "event": event, "status": status,
                "conclusion": conclusion if status == "completed" else None, "created_at": created,
                "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}"}

    def set_runs(self, wf, sha, *runs_per_call):
        self.answer(self.runs_path(wf, sha), *[{"body": {"workflow_runs": list(r)}} for r in runs_per_call])

    def set_jobs(self, run_id, certified="success", name="certified"):
        jobs = [{"name": "python", "conclusion": "success"}]
        if certified is not None:
            jobs.append({"name": name, "conclusion": certified})
        self.answer(self.jobs_path(run_id), {"body": {"jobs": jobs}})

    def green(self, sha, base_id=100):
        """A green checks.yml (with `certified`) and image run for sha."""
        self.set_runs("checks.yml", sha, [self.wrun(base_id, sha)])
        self.set_jobs(base_id)
        self.set_runs("image-smoke.yml", sha, [self.wrun(base_id + 1, sha)])

    def decide(self, workflows=None):
        self.pr_sha_from_dev()
        (self.ghdir / "responses.json").write_text(json.dumps(self.responses))

        def clock():
            return self.now

        def sleep(s):
            self.sleeps.append(s)
            self.now += s
        return rg.decide(REPO, 7, workflows or WORKFLOWS, str(self.tmp / "gate"), remote=str(self.remote),
                         clock=clock, sleep=sleep)

    def pr_sha_from_dev(self):
        body = self.responses[f"repos/{REPO}/pulls/7"][0].get("body")
        if body and body["head"]["sha"] == "0" * 40:
            body["head"]["sha"] = self.head()

    def promotion(self):
        """dev one commit ahead of release; pushed."""
        self.write("app.txt", "one\ntwo\n")
        h = self.commit("feature on dev")
        self.push()
        return h


class Passes(Fixture):
    def test_tree_equal_to_a_certified_green_dev_commit_passes(self):
        h = self.promotion()
        self.green(h)
        d = self.decide()
        self.assertTrue(d.ok, d.message)
        self.assertIn(f"certified commit: `{h}`", "\n".join(d.summary))
        self.assertIn(f"head H: `{h}`", "\n".join(d.summary))
        self.assertEqual(self.sleeps, [])

    def test_merge_commits_on_release_with_equal_trees_pass(self):
        # release carries a promotion merge commit dev lacks; their trees match, so it still passes (trees, not SHAs).
        self.write("app.txt", "one\nzero\n")
        self.commit("earlier feature")
        self.git(self.work, "checkout", "-q", "release")
        self.git(self.work, "merge", "-q", "--no-ff", "-m", "promote", "dev")
        self.git(self.work, "checkout", "-q", "dev")
        self.assertNotEqual(self.git(self.work, "rev-parse", "release^{tree}"), "")
        self.assertEqual(len(self.git(self.work, "rev-list", "--merges", "dev..release").split()), 1)
        h = self.promotion()
        self.green(h)
        d = self.decide()
        self.assertTrue(d.ok, d.message)

    def test_two_dev_commits_with_the_same_tree_pass_on_the_older_certified_one(self):
        older = self.promotion()
        h = self.commit("empty back-merge", allow_empty=True)
        self.push()
        self.set_runs("checks.yml", h, [self.wrun(300, h, conclusion="failure")])
        self.set_runs("image-smoke.yml", h, [self.wrun(301, h)])
        self.green(older)
        d = self.decide()
        self.assertTrue(d.ok, d.message)
        self.assertIn(f"certified commit: `{older}`", "\n".join(d.summary))

    def test_an_older_green_same_tree_commit_passes_at_once_while_the_head_still_runs(self):
        older = self.promotion()
        h = self.commit("empty", allow_empty=True)
        self.push()
        self.set_runs("checks.yml", h, [self.wrun(300, h, status="in_progress")])
        self.set_runs("image-smoke.yml", h, [self.wrun(301, h)])
        self.green(older)
        d = self.decide()
        self.assertTrue(d.ok, d.message)
        self.assertEqual(self.sleeps, [])

    def test_queued_then_green_within_the_wait_passes(self):
        h = self.promotion()
        self.set_runs("checks.yml", h, [self.wrun(100, h, status="queued")], [self.wrun(100, h)])
        self.set_jobs(100)
        self.set_runs("image-smoke.yml", h, [self.wrun(101, h)])
        d = self.decide()
        self.assertTrue(d.ok, d.message)
        self.assertEqual(self.sleeps, [30])

    def test_a_workflow_dispatch_run_on_dev_certifies(self):
        h = self.promotion()
        self.green(h)
        self.set_runs("checks.yml", h, [self.wrun(100, h, event="workflow_dispatch")])
        self.assertTrue(self.decide().ok)

    def test_a_hotfix_head_passes_as_not_a_promotion(self):
        self.pr(head_ref="hotfix/login")
        d = self.decide()
        self.assertTrue(d.ok)
        self.assertEqual(d.message, "not a promotion: every check runs")


class Refuses(Fixture):
    def test_a_tree_that_differs_is_refused(self):
        # release holds an unmerged hotfix: the merge tree differs from dev's, and the message says what to do.
        self.git(self.work, "checkout", "-q", "release")
        self.write("hotfix.txt", "fix\n")
        self.commit("fix: the hotfix")
        self.git(self.work, "checkout", "-q", "dev")
        h = self.promotion()
        self.green(h)
        d = self.decide()
        self.assertFalse(d.ok)
        self.assertIn("fix: the hotfix", d.message)
        self.assertIn("merge `release` back into `dev` first (playbook §5.6)", d.message)

    def test_a_merge_conflict_is_refused(self):
        self.git(self.work, "checkout", "-q", "release")
        self.write("app.txt", "hotfix line\n")
        self.commit("hotfix")
        self.git(self.work, "checkout", "-q", "dev")
        h = self.promotion()
        self.green(h)
        d = self.decide()
        self.assertFalse(d.ok)
        self.assertEqual(d.message, "the promotion conflicts with `release`")

    def test_no_run_is_refused(self):
        h = self.promotion()
        self.set_runs("checks.yml", h, [])
        self.set_runs("image-smoke.yml", h, [self.wrun(101, h)])
        d = self.decide()
        self.assertFalse(d.ok)
        self.assertIn(f"dev {h[:7]} has no green certification", d.message)
        self.assertIn("fix dev or re-run its checks", d.message)

    def test_certified_skipped_failed_missing_or_renamed_is_refused(self):
        h = self.promotion()
        for certified, name in (("skipped", "certified"), ("failure", "certified"), (None, "certified"),
                                ("success", "Certified")):
            with self.subTest(certified=certified, name=name):
                self.green(h)
                self.set_jobs(100, certified=certified, name=name)
                (self.ghdir / "counts.json").unlink(missing_ok=True)
                d = self.decide()
                self.assertFalse(d.ok)
                self.assertIn("certified", d.message)

    def test_a_red_image_workflow_is_refused(self):
        h = self.promotion()
        self.green(h)
        self.set_runs("image-smoke.yml", h, [self.wrun(101, h, conclusion="failure")])
        d = self.decide()
        self.assertFalse(d.ok)
        self.assertIn("runs/101 failure", d.message)

    def test_red_wins_while_another_workflow_still_runs(self):
        h = self.promotion()
        self.set_runs("checks.yml", h, [self.wrun(100, h, status="in_progress")])
        self.set_runs("image-smoke.yml", h, [self.wrun(101, h, conclusion="failure")])
        d = self.decide()  # checks.yml (still running) is read first
        self.assertFalse(d.ok)
        self.assertEqual(self.sleeps, [])

    def test_the_newest_run_wins_over_an_older_green_one(self):
        h = self.promotion()
        self.green(h)
        self.set_runs("checks.yml", h, [self.wrun(100, h, created="2026-11-06T10:00:00Z"),
                                         self.wrun(90, h, conclusion="failure", created="2026-11-06T11:00:00Z")])
        self.assertFalse(self.decide().ok)

    def test_a_tie_on_created_at_goes_to_the_highest_id(self):
        h = self.promotion()
        self.green(h)
        self.set_runs("checks.yml", h, [self.wrun(100, h), self.wrun(120, h, conclusion="cancelled")])
        d = self.decide()
        self.assertFalse(d.ok)
        self.assertIn("runs/120 cancelled", d.message)

    def test_schedule_or_pull_request_runs_only_are_refused(self):
        h = self.promotion()
        for event in ("schedule", "pull_request"):
            with self.subTest(event=event):
                self.green(h)
                self.set_runs("checks.yml", h, [self.wrun(100, h, event=event)])
                self.assertFalse(self.decide().ok)

    def test_still_running_at_25_minutes_fails_with_the_rerun_message(self):
        h = self.promotion()
        self.set_runs("checks.yml", h, [self.wrun(100, h, status="in_progress")])
        self.set_runs("image-smoke.yml", h, [self.wrun(101, h)])
        d = self.decide()
        self.assertFalse(d.ok)
        self.assertEqual(d.message, f"certification of {h[:7]} still running; re-run")
        self.assertEqual(sum(self.sleeps), 25 * 60)
        self.assertEqual(set(self.sleeps), {30})

    def test_heads_and_bases_other_than_dev_or_hotfix_into_release_are_refused(self):
        for kwargs in ({"head_ref": "feat/x"}, {"head_ref": "hotfix/x", "head_repo": "someone/backend-service"},
                       {"head_ref": "dev", "head_repo": "someone/backend-service"}, {"head_ref": "dev", "head_repo": None},
                       {"head_ref": "hotfix/x", "base": "dev"}, {"head_ref": "release-dev"}):
            with self.subTest(**kwargs):
                self.pr(**kwargs)
                self.assertFalse(self.decide().ok)

    def test_hostile_inputs_are_refused(self):
        for wfs in (["../checks.yml"], ["checks.yml;rm"], [], ["checks.yml", "a b.yml"], ["$(x).yml"], ["x.yml\n"], ["checks.txt"]):
            with self.subTest(workflows=wfs):
                self.assertFalse(rg.decide(REPO, 7, wfs, str(self.tmp / "g")).ok)
        self.assertFalse(rg.decide("OnePeek/x y", 7, WORKFLOWS, str(self.tmp / "g")).ok)
        self.assertFalse(rg.decide("OnePeek/x\nevil", 7, WORKFLOWS, str(self.tmp / "g")).ok)

    def test_a_head_sha_that_is_not_a_commit_sha_fails_closed(self):
        self.promotion()
        self.pr(sha="--upload-pack=touch /tmp/pwned")
        with self.assertRaisesRegex(rg.GateError, "is not a commit SHA"):
            self.decide()


class FailsClosed(Fixture):
    def main(self, *extra):
        self.pr_sha_from_dev()
        (self.ghdir / "responses.json").write_text(json.dumps(self.responses))
        summary = self.tmp / "summary.md"
        with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}), \
                contextlib.redirect_stdout(io.StringIO()):
            code = rg.main(["--repo", REPO, "--pr", "7", "--workflows", " ".join(WORKFLOWS),
                            "--git-dir", str(self.tmp / "gate"), *extra])
        return code, summary.read_text() if summary.exists() else ""

    def local_remote(self):
        """main() fetches from GitHub; here the fetch goes to the fixture's remote instead."""
        real = rg.fetch
        return mock.patch.object(rg, "fetch", side_effect=lambda d, r, sha: real(d, str(self.remote), sha))

    def test_api_errors_fail_closed(self):
        h = self.promotion()
        cases = {
            "pull request": lambda: self.answer(f"repos/{REPO}/pulls/7", {"exit": 1}),
            "runs": lambda: self.answer(self.runs_path("checks.yml", h), {"exit": 1}),
            "jobs": lambda: self.answer(self.jobs_path(100), {"exit": 1}),
            "exit 1 with a valid body": lambda: self.answer(self.runs_path("image-smoke.yml", h),
                                                            {"exit": 1, "body": {"workflow_runs": [self.wrun(101, h)]}}),
            "unparsable": lambda: self.answer(self.runs_path("image-smoke.yml", h), {"raw": "<html>"}),
            "no list": lambda: self.answer(self.runs_path("image-smoke.yml", h), {"body": {"message": "x"}}),
            "run without id": lambda: self.answer(self.runs_path("image-smoke.yml", h),
                                                  {"body": {"workflow_runs": [{"head_sha": h, "head_branch": "dev",
                                                                                "event": "push"}]}}),
        }
        for name, breaker in cases.items():
            with self.subTest(name):
                self.responses = {}
                self.pr()
                self.green(h)
                breaker()
                with self.local_remote():
                    code, summary = self.main()
                self.assertEqual(code, 1)
                self.assertIn("refused", summary)
                shutil.rmtree(self.tmp / "gate", ignore_errors=True)

    def test_a_missing_or_bad_pull_request_number_is_refused(self):
        for pr in ("", "0", "7;x", "-1", "07"):
            with self.subTest(pr=pr):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = rg.main(["--repo", REPO, "--pr", pr, "--workflows", "checks.yml", "--git-dir", str(self.tmp)])
                self.assertEqual(code, 1)
                self.assertIn("not a pull request number", out.getvalue())

    def test_a_pass_through_main_writes_the_summary(self):
        h = self.promotion()
        self.green(h)
        with self.local_remote():
            code, summary = self.main()
        self.assertEqual(code, 0, summary)
        self.assertIn("passed", summary)
        self.assertIn(h, summary)

    def test_an_unreachable_remote_fails_closed(self):
        h = self.promotion()
        self.green(h)
        (self.ghdir / "responses.json").write_text(json.dumps(self.responses))
        self.pr_sha_from_dev()
        (self.ghdir / "responses.json").write_text(json.dumps(self.responses))
        with self.assertRaisesRegex(rg.GateError, "git fetch"):
            rg.decide(REPO, 7, WORKFLOWS, str(self.tmp / "gate"), remote=str(self.tmp / "missing.git"))


if __name__ == "__main__":
    unittest.main()
