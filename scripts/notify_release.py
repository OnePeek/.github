#!/usr/bin/env python3
"""One Slack message per release, at its end, to #deploys (founder decision 2026-11-15; W1-SLACK-GH rulings 1-4).

The one shared notifier: release-ecs.yml's `notify` job and release-notify.yml (the infra release's last job) run this
exact file, checked out from OnePeek/.github at the workflow's own commit. It posts one plain-text message:

    [<repo>] <version> -> <env>: <RESULT>
    commit <sha7> | <duration> | <readiness path>: <outcome>
    failed step: <job> / <step>                          (FAILED and ROLLED BACK only, when the jobs API says)
    <run> | <PR #n> | <commit>                           (Slack links)

RESULT is ROLLED BACK (ROLLBACK=success), else CANCELLED (a needed job was cancelled), else FAILED (a needed job failed,
or WATCH is set and is not success), else SUCCESS. Nothing is posted at the start of a release (the channel stays quiet).

It never fails or slows a release: an unset SLACK_WEBHOOK is one ::warning:: and no post (F680: visible); a Slack error or timeout
(curl --max-time 10, one retry) or any error of this script is one ::warning::; the exit status is always 0. The webhook
URL is never printed or put on a command line: curl reads it from its standard input (--config -), and any output of
curl is scrubbed of it before it is shown.

Environment: SLACK_WEBHOOK, ENVIRONMENT, VERSION, RESULTS (a JSON list of the needed jobs' results, the workflow's
`toJSON(needs.*.result)`), ROLLBACK, WATCH (optional), STATUS and STATUS_PATH (optional), STARTED (epoch seconds,
optional), HEAD_MESSAGE (optional), GH_TOKEN (optional, for the failed step and a missing start), and the runner's
GITHUB_SERVER_URL, GITHUB_API_URL, GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT, GITHUB_SHA.
Standard library only.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

LIMIT = 2900  # Slack's text limit is 3,000 characters; the message stays under it
CURL = ["curl", "--silent", "--show-error", "--fail", "--max-time", "10", "--retry", "1",
        "--header", "Content-Type: application/json", "--config", "-"]
CURL_TIMEOUT = 30  # seconds: two tries of at most 10 each, plus the retry's wait; a hung curl is killed
API_TIMEOUT = 10
PR = re.compile(r"Merge pull request #(\d+)|\(#(\d+)\)\s*$")


def result_of(env: dict) -> str:
    try:
        results = json.loads(env.get("RESULTS") or "[]")
    except ValueError:
        results = ["unknown"]
    if not isinstance(results, list):
        results = ["unknown"]
    if env.get("ROLLBACK") == "success":
        return "ROLLED BACK"
    if "cancelled" in results:
        return "CANCELLED"
    if any(r not in ("success", "skipped") for r in results):
        return "FAILED"
    if "WATCH" in env and env["WATCH"] != "success":
        return "FAILED"
    return "SUCCESS"


def escape(text: str) -> str:
    """Slack mrkdwn control characters (&, <, >) in text we did not write."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def minutes(seconds: int) -> str:
    return "%dm %02ds" % divmod(max(seconds, 0), 60)


def pr_number(message: str) -> str:
    first = (message or "").splitlines()[0] if message else ""
    m = PR.search(first)
    return (m.group(1) or m.group(2)) if m else ""


def api_jobs(env: dict) -> list:
    """This run attempt's jobs from the GitHub API, or [] on any error (best effort, never fails)."""
    token = env.get("GH_TOKEN")
    if not token:
        return []
    url = "%s/repos/%s/actions/runs/%s/attempts/%s/jobs?per_page=100" % (
        env.get("GITHUB_API_URL", "https://api.github.com"), env["GITHUB_REPOSITORY"], env["GITHUB_RUN_ID"],
        env.get("GITHUB_RUN_ATTEMPT", "1"))
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token,
                                                   "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=API_TIMEOUT) as answer:
            jobs = json.load(answer).get("jobs")
        return jobs if isinstance(jobs, list) else []
    except Exception:  # noqa: BLE001 - a notice is best effort
        return []


def failed_step(jobs: list) -> str:
    for job in jobs:
        if isinstance(job, dict) and job.get("conclusion") == "failure":
            for step in job.get("steps") or []:
                if isinstance(step, dict) and step.get("conclusion") == "failure":
                    return "%s / %s" % (job.get("name", "?"), step.get("name", "?"))
            return str(job.get("name", "?"))
    return ""


def first_start(jobs: list) -> int | None:
    starts = []
    for job in jobs:
        try:
            starts.append(datetime.datetime.fromisoformat(str(job["started_at"]).replace("Z", "+00:00")).timestamp())
        except (KeyError, TypeError, ValueError):
            continue
    return int(min(starts)) if starts else None


def build_message(env: dict, jobs: list | None = None, now: int | None = None) -> str:
    result = result_of(env)
    if jobs is None:
        need_api = result in ("FAILED", "ROLLED BACK") or not env.get("STARTED")
        jobs = api_jobs(env) if need_api else []
    now = int(time.time()) if now is None else now
    repo_full = env["GITHUB_REPOSITORY"]
    repo = repo_full.split("/")[-1]
    sha = env.get("GITHUB_SHA", "")
    server = env.get("GITHUB_SERVER_URL", "https://github.com")
    run = "%s/%s/actions/runs/%s" % (server, repo_full, env.get("GITHUB_RUN_ID", ""))
    if env.get("GITHUB_RUN_ATTEMPT", "1") not in ("", "1"):
        run += "/attempts/%s" % env["GITHUB_RUN_ATTEMPT"]
    version = env.get("VERSION") or sha[:7]
    lines = ["[%s] %s -> %s: %s" % (escape(repo), escape(version[:80]), escape(env.get("ENVIRONMENT", "?")[:40]), result)]

    facts = ["commit %s" % sha[:7]]
    started = env.get("STARTED", "")
    start = int(started) if started.isdigit() else first_start(jobs)
    if start is not None:
        facts.append("took %s" % minutes(now - start))
    if env.get("STATUS"):
        facts.append("%s: %s" % (escape(env.get("STATUS_PATH") or "status")[:40], escape(env["STATUS"])[:20]))
    lines.append(" | ".join(facts))

    if result in ("FAILED", "ROLLED BACK"):
        step = failed_step(jobs)
        lines.append("failed step: %s" % (escape(step)[:300] if step else "see the run"))

    links = ["<%s|run>" % run]
    number = pr_number(env.get("HEAD_MESSAGE", ""))
    if number:
        links.append("<%s/%s/pull/%s|PR #%s>" % (server, repo_full, number, number))
    links.append("<%s/%s/commit/%s|commit>" % (server, repo_full, sha))
    lines.append(" | ".join(links))
    return "\n".join(lines)[:LIMIT]


def post(webhook: str, text: str) -> tuple[bool, str]:
    """Posts the message; (ok, what to show). The webhook goes to curl on standard input, never on its command line."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as body:
        json.dump({"text": text}, body)
    config = 'url = "%s"\ndata-binary = "@%s"\n' % (webhook.replace("\\", "\\\\").replace('"', '\\"'), body.name)
    try:
        done = subprocess.run(CURL + ["--output", os.devnull], input=config, capture_output=True, text=True,
                              timeout=CURL_TIMEOUT)
        said = (done.stdout + done.stderr).replace(webhook, "***").strip()
        return done.returncode == 0, "curl exit %d%s" % (done.returncode, (": " + said[:300]) if said else "")
    except subprocess.TimeoutExpired:
        return False, "curl timed out after %d s" % CURL_TIMEOUT
    except OSError as e:
        return False, "curl could not run (%s)" % e.__class__.__name__
    finally:
        os.unlink(body.name)


def main(env: dict | None = None) -> int:
    env = dict(os.environ) if env is None else env
    webhook = env.get("SLACK_WEBHOOK", "").strip()
    try:
        if not webhook:
            print("::warning::SLACK_DEPLOYS_WEBHOOK is not set: no release notice is posted to Slack (F680: set the secret, or pass it from the caller).")
            return 0
        text = build_message(env)
        ok, said = post(webhook, text)
        if ok:
            print("Posted the release notice to Slack:\n" + text)
        else:
            print("::warning::The release notice was not posted to Slack (%s); the release is not affected." % said)
    except Exception as e:  # noqa: BLE001 - a notice never fails a release
        print("::warning::The release notice failed (%s); the release is not affected." % e.__class__.__name__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
