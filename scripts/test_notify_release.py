"""Tests for scripts/notify_release.py and the notify jobs of release-ecs.yml and release-notify.yml (W1-SLACK-GH)."""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
import notify_release as nr  # noqa: E402

SCRIPT = Path(__file__).parent / "notify_release.py"
WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
HOOK = "https://hooks.slack.example/services/T000/B000/s3cr3tW3bh00k"
# The fail-open policy: the only jobs of the release workflows that may carry continue-on-error (job or step) or swallow
# an error. Everything else in a release fails closed.
FAIL_OPEN = {("release-ecs.yml", "notify"), ("release-notify.yml", "notify")}
RELEASE_WORKFLOWS = ("release-ecs.yml", "release-notify.yml", "release-lock.yml", "operate.yml", "deploy-ecs.yml")
NOW = 1_800_000_000

# A stand-in curl: records its argv and standard input, then behaves as CURL_MODE says.
FAKE_CURL = r'''#!/usr/bin/env python3
import json, os, re, sys, time
config = sys.stdin.read()
log = os.environ["CURL_LOG"]
body = ""
m = re.search(r'data-binary = "@(.*)"', config)
if m and os.path.exists(m.group(1)):
    body = open(m.group(1)).read()
json.dump({"argv": sys.argv, "config": config, "body": body}, open(log, "w"))
mode = os.environ.get("CURL_MODE", "ok")
url = re.search(r'url = "(.*)"', config).group(1)
if mode == "fail":
    sys.stderr.write("curl: (22) The requested URL returned error: 404 for " + url + "\n")
    sys.exit(22)
if mode == "hang":
    time.sleep(30)
sys.exit(0)
'''


def base_env(**extra) -> dict:
    env = {"GITHUB_REPOSITORY": "OnePeek/backend-service", "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "1",
           "GITHUB_SHA": "abcdef1234567890abcdef1234567890abcdef12", "GITHUB_SERVER_URL": "https://github.com",
           "ENVIRONMENT": "preprod", "VERSION": "0.7.0", "RESULTS": '["success","skipped","success"]',
           "ROLLBACK": "skipped", "WATCH": "success", "STATUS": "success", "STATUS_PATH": "/api/status",
           "STARTED": str(NOW - 723), "HEAD_MESSAGE": "Merge pull request #142 from OnePeek/release-pr\n\nbody"}
    env.update(extra)
    return env


FAILED_JOBS = [
    {"name": "release / prepare", "conclusion": "success", "started_at": "2027-01-15T08:00:00Z", "steps": []},
    {"name": "release / verify", "conclusion": "failure", "started_at": "2027-01-15T08:05:00Z",
     "steps": [{"name": "Set up job", "conclusion": "success"},
               {"name": "10 minutes after stable, poll the rolled services' deploy alarms every 30 s",
                "conclusion": "failure"}]},
]


class Message(unittest.TestCase):
    def test_success(self):
        text = nr.build_message(base_env(), jobs=[], now=NOW)
        self.assertEqual(text.splitlines(), [
            "[backend-service] 0.7.0 -> preprod: SUCCESS",
            "commit abcdef1 | took 12m 03s | /api/status: success",
            "rolled out, watching: a deploy alarm that fires from now on posts to #alerts",
            "<https://github.com/OnePeek/backend-service/actions/runs/42|run> | "
            "<https://github.com/OnePeek/backend-service/pull/142|PR #142> | "
            "<https://github.com/OnePeek/backend-service/commit/abcdef1234567890abcdef1234567890abcdef12|commit>"])

    def test_only_a_success_with_a_watch_says_rolled_out_watching(self):
        # REL-FAST: release-ecs.yml (WATCH set) says it keeps watching; the infra release (no WATCH) and failures do not.
        env = base_env()
        del env["WATCH"]
        self.assertNotIn("watching", nr.build_message(env, jobs=[], now=NOW))
        failed = nr.build_message(base_env(RESULTS='["success","failure"]'), jobs=[], now=NOW)
        self.assertNotIn("watching", failed)

    def test_failed_names_the_failed_job_and_step(self):
        text = nr.build_message(base_env(RESULTS='["success","failure"]', WATCH="failure"), jobs=FAILED_JOBS, now=NOW)
        lines = text.splitlines()
        self.assertEqual(lines[0], "[backend-service] 0.7.0 -> preprod: FAILED")
        self.assertEqual(lines[2], "failed step: release / verify / 10 minutes after stable, poll the rolled services' "
                                   "deploy alarms every 30 s")

    def test_rolled_back(self):
        text = nr.build_message(base_env(RESULTS='["success","failure"]', ROLLBACK="success", WATCH="failure"),
                                jobs=FAILED_JOBS, now=NOW)
        self.assertTrue(text.startswith("[backend-service] 0.7.0 -> preprod: ROLLED BACK\n"), text)
        self.assertIn("failed step: release / verify / ", text)

    def test_cancelled(self):
        text = nr.build_message(base_env(RESULTS='["success","cancelled","skipped"]'), jobs=[], now=NOW)
        self.assertTrue(text.startswith("[backend-service] 0.7.0 -> preprod: CANCELLED\n"), text)
        self.assertNotIn("failed step", text)

    def test_a_watch_that_did_not_succeed_is_failed_even_with_no_failed_job(self):
        self.assertEqual(nr.result_of(base_env(WATCH="")), "FAILED")
        self.assertEqual(nr.result_of(base_env(WATCH="skipped")), "FAILED")

    def test_no_watch_key_and_all_green_is_success(self):
        env = base_env()
        del env["WATCH"]
        self.assertEqual(nr.result_of(env), "SUCCESS")

    def test_unreadable_results_are_failed(self):
        self.assertEqual(nr.result_of(base_env(RESULTS="not json")), "FAILED")
        self.assertEqual(nr.result_of(base_env(RESULTS='{"a": 1}')), "FAILED")

    def test_failed_with_no_step_known_points_at_the_run(self):
        text = nr.build_message(base_env(RESULTS='["failure"]'), jobs=[], now=NOW)
        self.assertIn("failed step: see the run", text)

    def test_infra_shape_without_version_status_or_start(self):
        env = base_env(GITHUB_REPOSITORY="OnePeek/onepeek-infra", VERSION="", STATUS="", STARTED="",
                       HEAD_MESSAGE="release: preprod (#301)")
        del env["WATCH"]
        text = nr.build_message(env, jobs=FAILED_JOBS[:1], now=NOW)
        lines = text.splitlines()
        self.assertEqual(lines[0], "[onepeek-infra] abcdef1 -> preprod: SUCCESS")
        self.assertTrue(lines[1].startswith("commit abcdef1 | took "), lines[1])
        self.assertNotIn("status", lines[1])
        self.assertIn("/pull/301|PR #301>", lines[2])

    def test_no_pr_in_the_message_links_run_and_commit_only(self):
        text = nr.build_message(base_env(HEAD_MESSAGE="chore: direct push"), jobs=[], now=NOW)
        self.assertNotIn("PR #", text)
        self.assertIn("|commit>", text)

    def test_a_rerun_links_its_attempt(self):
        text = nr.build_message(base_env(GITHUB_RUN_ATTEMPT="2"), jobs=[], now=NOW)
        self.assertIn("/actions/runs/42/attempts/2|run>", text)

    def test_stays_under_3000_characters_and_escapes_slack_controls(self):
        jobs = [{"name": "j<b>&", "conclusion": "failure", "steps": [{"name": "s" * 5000, "conclusion": "failure"}]}]
        text = nr.build_message(base_env(RESULTS='["failure"]', VERSION="v" * 5000), jobs=jobs, now=NOW)
        self.assertLess(len(text), 3000)
        self.assertIn("j&lt;b&gt;&amp;", text)
        # The overall cap holds whatever field grows (here the repository name, which no field cap covers).
        long_repo = nr.build_message(base_env(GITHUB_REPOSITORY="OnePeek/" + "r" * 5000), jobs=[], now=NOW)
        self.assertLess(len(long_repo), 3000)


class JobsApi(unittest.TestCase):
    def serve(self, payload: bytes, status: int = 200):
        class Handler(http.server.BaseHTTPRequestHandler):
            seen = []

            def do_GET(self):  # noqa: N802
                Handler.seen.append((self.path, self.headers.get("Authorization")))
                self.send_response(status)
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:%d" % server.server_port, Handler.seen

    def test_reads_this_attempts_jobs(self):
        url, seen = self.serve(json.dumps({"jobs": FAILED_JOBS}).encode())
        jobs = nr.api_jobs(base_env(GITHUB_API_URL=url, GH_TOKEN="t", GITHUB_RUN_ATTEMPT="3"))
        self.assertEqual(nr.failed_step(jobs), "release / verify / 10 minutes after stable, poll the rolled services' "
                                               "deploy alarms every 30 s")
        self.assertEqual(seen, [("/repos/OnePeek/backend-service/actions/runs/42/attempts/3/jobs?per_page=100", "Bearer t")])

    def test_an_api_error_or_no_token_gives_nothing(self):
        url, _ = self.serve(b"oops", status=500)
        self.assertEqual(nr.api_jobs(base_env(GITHUB_API_URL=url, GH_TOKEN="t")), [])
        self.assertEqual(nr.api_jobs(base_env(GITHUB_API_URL="http://127.0.0.1:9", GH_TOKEN="t")), [])
        self.assertEqual(nr.api_jobs(base_env(GITHUB_API_URL=url)), [])

    def test_the_first_job_start_is_the_start_when_none_is_given(self):
        self.assertEqual(nr.first_start(FAILED_JOBS), 1800000000)  # 2027-01-15T08:00:00Z, the earlier of the two


class Main(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)], check=False))
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        curl = bin_dir / "curl"
        curl.write_text("#!%s\n%s" % (sys.executable, FAKE_CURL.split("\n", 1)[1]))
        curl.chmod(0o755)
        self.log = self.tmp / "curl.json"
        self.path = "%s%s%s" % (bin_dir, os.pathsep, os.environ.get("PATH", ""))

    def run_main(self, mode: str = "ok", hook: str = HOOK, **extra):
        env = {**base_env(**extra), "PATH": self.path, "CURL_LOG": str(self.log), "CURL_MODE": mode,
               "SLACK_WEBHOOK": hook, "GITHUB_API_URL": "http://127.0.0.1:9"}
        return subprocess.run([sys.executable, "-B", "-I", str(SCRIPT)], env=env, capture_output=True, text=True,
                              timeout=60)

    def test_posts_one_message_with_the_url_only_on_standard_input(self):
        out = self.run_main()
        self.assertEqual(out.returncode, 0, out.stderr)
        call = json.loads(self.log.read_text())
        self.assertNotIn(HOOK, " ".join(call["argv"]))
        self.assertIn('url = "%s"' % HOOK, call["config"])
        for flag in ("--max-time", "--retry", "--fail", "--config"):
            self.assertIn(flag, call["argv"])
        self.assertEqual(call["argv"][call["argv"].index("--max-time") + 1], "10")
        self.assertEqual(call["argv"][call["argv"].index("--retry") + 1], "1")
        self.assertEqual(json.loads(call["body"])["text"].splitlines()[0], "[backend-service] 0.7.0 -> preprod: SUCCESS")
        self.assertNotIn(HOOK, out.stdout + out.stderr)

    def test_an_unset_secret_is_one_warning_and_no_post(self):
        # F680: a missing secret must be visible: a warning shows on the run's summary page, a notice is easy to miss.
        for hook in ("", "   "):
            out = self.run_main(hook=hook)
            self.assertEqual(out.returncode, 0)
            self.assertEqual(out.stdout.count("::warning::"), 1, out.stdout)
            self.assertNotIn("::notice::", out.stdout)
            self.assertIn("SLACK_DEPLOYS_WEBHOOK is not set", out.stdout)
            self.assertFalse(self.log.exists())

    def test_a_failing_curl_is_a_warning_exit_0_and_never_shows_the_url(self):
        out = self.run_main(mode="fail")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("::warning::The release notice was not posted to Slack (curl exit 22", out.stdout)
        self.assertNotIn(HOOK, out.stdout + out.stderr)
        self.assertNotIn("s3cr3tW3bh00k", out.stdout + out.stderr)

    def test_a_hung_curl_is_killed_and_is_a_warning(self):
        old = nr.CURL_TIMEOUT
        nr.CURL_TIMEOUT = 1
        self.addCleanup(setattr, nr, "CURL_TIMEOUT", old)
        os.environ["CURL_LOG"], os.environ["CURL_MODE"] = str(self.log), "hang"
        saved_path = os.environ.get("PATH", "")
        os.environ["PATH"] = self.path
        try:
            ok, said = nr.post(HOOK, "x")
        finally:
            os.environ["PATH"] = saved_path
            del os.environ["CURL_LOG"], os.environ["CURL_MODE"]
        self.assertEqual((ok, said), (False, "curl timed out after 1 s"))

    def test_no_curl_at_all_is_a_warning(self):
        env = {**base_env(), "PATH": str(self.tmp / "empty"), "SLACK_WEBHOOK": HOOK}
        out = subprocess.run([sys.executable, "-B", "-I", str(SCRIPT)], env=env, capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("::warning::", out.stdout)
        self.assertNotIn(HOOK, out.stdout + out.stderr)

    def test_any_error_in_the_script_is_a_warning_and_exit_0(self):
        env = {k: v for k, v in base_env().items() if k != "GITHUB_REPOSITORY"}
        env.update({"PATH": self.path, "CURL_LOG": str(self.log), "SLACK_WEBHOOK": HOOK})
        out = subprocess.run([sys.executable, "-B", "-I", str(SCRIPT)], env=env, capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("::warning::The release notice failed (KeyError)", out.stdout)
        self.assertNotIn(HOOK, out.stdout + out.stderr)


def load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def call_of(doc: dict) -> dict:
    return doc.get("on", doc.get(True))["workflow_call"]


class Workflows(unittest.TestCase):
    def test_both_notify_jobs_run_the_same_steps_but_their_env(self):
        ecs, own = load("release-ecs.yml")["jobs"]["notify"], load("release-notify.yml")["jobs"]["notify"]
        strip = lambda steps: [{k: v for k, v in s.items() if k != "env"} if i == len(steps) - 1 else s
                               for i, s in enumerate(steps)]
        self.assertEqual(strip(ecs["steps"]), strip(own["steps"]))
        for job in (ecs, own):
            self.assertEqual(job["steps"][-1]["run"], "python3 -I _gh/scripts/notify_release.py")
            self.assertEqual(job["steps"][-1]["env"]["SLACK_WEBHOOK"], "${{ secrets.SLACK_DEPLOYS_WEBHOOK }}")
            self.assertEqual(job["steps"][1]["with"]["ref"], "${{ job.workflow_sha }}")
            self.assertEqual(job["steps"][1]["with"]["sparse-checkout"], "scripts/notify_release.py")

    def test_the_notify_jobs_are_fail_open_short_and_on_the_callers_runner(self):
        # F680: on both releases of 2026-11-15 the notify job, pinned to ubuntu-latest, was never started ("recent account
        # payments have failed or your spending limit needs to be increased"): a job that never starts is red whatever
        # continue-on-error says. It runs where every other job of the release runs: our small class (F694: prod, an unset
        # runner and `github` too, so infra's call with no runner input needs no change), canary on canary.
        for name in ("release-ecs.yml", "release-notify.yml"):
            job = load(name)["jobs"]["notify"]
            self.assertIs(job["continue-on-error"], True, name)
            self.assertLessEqual(job["timeout-minutes"], 3, name)
            self.assertEqual(job["runs-on"], "${{ inputs.runner == 'canary' && 'onepeek-arm64-canary' || 'onepeek-arm64-small' }}",
                             name)
            self.assertEqual(job["permissions"], {"contents": "read", "actions": "read"}, name)
            for step in job["steps"]:
                self.assertIs(step.get("continue-on-error"), True, (name, step.get("name")))
                self.assertNotIn("set -x", str(step.get("run", "")), name)
                self.assertNotIn("SLACK", str(step.get("run", "")), name)  # the URL only ever reaches the script's env

    def test_the_secret_is_optional_in_both(self):
        for name in ("release-ecs.yml", "release-notify.yml"):
            self.assertIs(call_of(load(name))["secrets"]["SLACK_DEPLOYS_WEBHOOK"]["required"], False, name)

    def test_release_ecs_notify_is_last_after_every_job_and_quiet_for_dry_runs_and_skips(self):
        doc = load("release-ecs.yml")
        job = doc["jobs"]["notify"]
        self.assertEqual(set(job["needs"]), set(doc["jobs"]) - {"notify"})
        self.assertEqual(job["if"], "${{ always() && !inputs.dry-run && inputs.environment != 'prod' && "
                                    "needs.prepare.outputs.skip != 'true' }}")
        env = job["steps"][-1]["env"]
        self.assertEqual(env["RESULTS"], "${{ toJSON(needs.*.result) }}")
        self.assertEqual(env["ROLLBACK"], "${{ needs.verify.outputs.rollback }}")
        self.assertEqual(env["STATUS"], "${{ needs.verify.outputs.ready }}")
        self.assertEqual(doc["jobs"]["verify"]["outputs"]["ready"], "${{ steps.ready.outcome }}")
        ready = [s for s in doc["jobs"]["verify"]["steps"] if s.get("id") == "ready"]
        self.assertEqual(len(ready), 1)
        self.assertIn("/api/status", ready[0]["run"])
        self.assertNotIn("outputs.token", json.dumps(env))  # never the lock token (a step env is shown in the log)

    def test_release_notify_sends_nothing_for_prod_itself(self):
        # F657: the header and AGENTS.md say no notice for prod; the guard is in the shared workflow, not left to the caller.
        job = load("release-notify.yml")["jobs"]["notify"]
        self.assertEqual(job["if"], "${{ inputs.environment != 'prod' }}")

    def test_fail_open_policy_only_the_notify_jobs_continue_on_error_in_release_workflows(self):
        for name in RELEASE_WORKFLOWS:
            for job_id, job in load(name)["jobs"].items():
                found = "continue-on-error" in job or any("continue-on-error" in s for s in job.get("steps") or [])
                self.assertEqual(found, (name, job_id) in FAIL_OPEN, (name, job_id))


    def test_the_header_says_prod_has_no_notice_on_purpose(self):
        # F650: the spec did not exclude prod; the header says the exclusion is deliberate and why.
        header = (WORKFLOWS / "release-ecs.yml").read_text(encoding="utf-8").split("\nname: release-ecs\n")[0]
        self.assertIn("No notice for prod, on purpose", header)
        self.assertIn("promote.yml", header.split("No notice for prod, on purpose")[1][:300])

    def test_agents_md_lists_the_notify_workflow_and_the_fail_open_policy(self):
        # F647, F650: the layout names release-notify.yml, the notify job and FAIL_OPEN.
        agents = (WORKFLOWS.parent.parent / "AGENTS.md").read_text(encoding="utf-8")
        layout = agents.split("## Build kit")[0]
        for needle in ("release-notify.yml", "scripts/notify_release.py", "FAIL_OPEN", "`notify`"):
            self.assertIn(needle, layout, needle)


if __name__ == "__main__":
    unittest.main()
