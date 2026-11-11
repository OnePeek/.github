#!/usr/bin/env python3
"""Automatic re-run after a spot reclaim (onepeek-docs architecture/lld/infra/ci-runners.md section 4, row R1).

A caller runs spot-rerun.yml on `workflow_run: completed` with conclusion `failure`. This script reads the failed jobs
of that run, the log of each and the check-run annotations of each. When either holds the runner's shutdown line ("The
runner has received a shutdown signal", in the log) or GitHub's "lost communication with the server" (usually an
annotation with no log), and the run is on its first attempt, it re-runs
the failed jobs once (`gh run rerun <id> --failed`) and writes the reason (the job names) to the step summary.
Otherwise it does nothing: a second reclaim, or an ordinary failure, stays failed and visible.

It also never re-runs a superseded run (ci-runners.md section 4.1, W2.6): a re-run joins its concurrency group again and
would cancel the newer, current run. superseded() is checked before any log is read and again just before the re-run;
a read that fails re-runs nothing (exit 1).

    RUN_ID=<github.event.workflow_run.id> REPO=<github.repository> python3 spot_rerun.py
    WORKFLOWS='checks release' REPO=<github.repository> python3 spot_rerun.py     # scan mode (no RUN_ID)

Scan mode (PIPE-SIMPLE v2 GH-1, plan item 3): a caller on a 10-minute `schedule` (or `workflow_dispatch`) has no
workflow_run event, so RUN_ID is empty. The script lists the repo's failed runs created in the last
SCAN_LIST_HOURS, keeps those of the named workflows (WORKFLOWS, space-separated `name:`s) on attempt 1 that finished
(updated_at) in the last SCAN_DECIDE_MINUTES, and decides each exactly as above (run()), oldest finish first, while
SCAN_BUDGET_SECONDS allows; runs left over are named and fail the job (the next scan takes them). It is idempotent: run() re-reads each run first, and a run already re-run is on attempt
2 or later (or no longer failed), so it is never re-run twice; the caller's concurrency group keeps two scans apart.
A failed list read fails the job; a failed read of one run is reported, the others are still decided, and the job
fails at the end (exit 1). An empty RUN_ID with no WORKFLOWS is an input error.

RUN_ID and REPO are checked strictly (digits only; owner/name). The token is GH_TOKEN (actions: read, checks: read, and
actions: write to re-run). Standard library only; the `gh` CLI does the API calls.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

# A reclaimed runner's log can reach the API minutes after the job ends: the log is read up to LOG_TRIES times.
LOG_TRIES = 4
LOG_WAIT_SECONDS = 30  # 3 waits: 90 seconds at most
MARKERS = ("The runner has received a shutdown signal", "lost communication with the server")
RUN_ID = re.compile(r"[1-9][0-9]{0,19}", re.ASCII)
REPO = re.compile(r"(?!\.+/)[A-Za-z0-9._-]+/(?!\.+$)[A-Za-z0-9._-]+", re.ASCII)


# Scan mode (GH-1 round 2, B1/B2; provisional): runs CREATED in the last SCAN_LIST_HOURS are listed (a sandbox or
# release run lasts hours), and only those that FINISHED (updated_at) in the last SCAN_DECIDE_MINUTES are decided, so a
# run is decided by at most two 10-minute scans. SCAN_BUDGET_SECONDS keeps the scan inside the job's 5-minute limit: a
# run is started only while the worst case of one decision (RUN_WORST_SECONDS: every log wait plus reads) still fits.
SCAN_LIST_HOURS = 24
SCAN_DECIDE_MINUTES = 20
SCAN_BUDGET_SECONDS = 240
RUN_WORST_SECONDS = 120

HTTP_STATUS = re.compile(r"\(HTTP ([0-9]{3})\)")


class GhError(Exception):
    """A gh call failed. `status` is the HTTP status gh printed at the end of its error ("gh: Not Found (HTTP 404)"), or 0."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def gh(*args: str) -> str:
    """Run the gh CLI and return stdout; tests replace this."""
    done = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        found = HTTP_STATUS.findall(done.stderr)
        raise GhError(f"gh {' '.join(args[:2])} failed: {done.stderr.strip()[:300]}", int(found[-1]) if found else 0)
    return done.stdout


def validate(run_id: str, repo: str) -> None:
    if not RUN_ID.fullmatch(run_id):
        raise ValueError(f"RUN_ID must be a run number (digits only), got {run_id!r}.")
    if not REPO.fullmatch(repo):
        raise ValueError(f"REPO must be <owner>/<name>, got {repo!r}.")


def failed_jobs(repo: str, run_id: str) -> list[dict]:
    out = gh("api", "--paginate", f"repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100",
             "--jq", ".jobs[] | {id, name, conclusion}")
    jobs = [json.loads(line) for line in out.splitlines() if line.strip()]
    return [j for j in jobs if j.get("conclusion") == "failure"]


def reclaimed(text: str) -> bool:
    return any(m in text for m in MARKERS)


def annotations_text(repo: str, job_id: str) -> str:
    """The messages of the job's check-run annotations. "The operation was canceled." is not a marker (too generic)."""
    out = gh("api", "--paginate", f"repos/{repo}/check-runs/{job_id}/annotations?per_page=100", "--jq", ".[].message")
    return out


def job_log(repo: str, job_id: str) -> str:
    """The job's log. A job log holds terminal colour codes, which newer gh refuses to print without
    --allow-escape-sequences (L32: every CI log read failed on the runners' gh); an older gh does not know the flag."""
    try:
        return gh("api", "--allow-escape-sequences", f"repos/{repo}/actions/jobs/{job_id}/logs")
    except GhError as e:
        if "allow-escape-sequences" not in str(e) or "unknown flag" not in str(e):
            raise
    return gh("api", f"repos/{repo}/actions/jobs/{job_id}/logs")


def reclaimed_jobs(repo: str, jobs: list[dict]) -> list[str]:
    """Names of the failed jobs whose log or annotations show a reclaim. A source that cannot be read counts as no
    match for that source only, and the reason goes to stderr. Logs are read in up to LOG_TRIES rounds, LOG_WAIT_SECONDS
    apart (a reclaimed runner's log can reach the API late); the total wait stays inside the job's 5-minute limit.
    A job that prints a marker itself can force at most one extra re-run (attempt 1 only)."""
    ids = {str(j.get("id", "")): str(j.get("name", j.get("id", ""))) for j in jobs if RUN_ID.fullmatch(str(j.get("id", "")))}
    found: set[str] = set()
    for job_id in ids:
        try:
            if reclaimed(annotations_text(repo, job_id)):
                found.add(job_id)
        except GhError as e:
            print(f"::warning::annotations of job {job_id} could not be read: {e}", file=sys.stderr)
    why: dict[str, str] = {}
    for attempt in range(LOG_TRIES):
        for job_id in [j for j in ids if j not in found]:
            try:
                if reclaimed(job_log(repo, job_id)):
                    found.add(job_id)
                else:
                    why[job_id] = "no marker in the log"
            except GhError as e:
                why[job_id] = str(e)
        if len(found) == len(ids) or attempt + 1 == LOG_TRIES:
            break
        time.sleep(LOG_WAIT_SECONDS)
    for job_id in ids:
        if job_id not in found:
            print(f"::warning::job {job_id}: no reclaim seen after {LOG_TRIES} log reads ({why.get(job_id, '')})",
                  file=sys.stderr)
    return [ids[j] for j in ids if j in found]


def superseded(repo: str, run_id: str, info: dict) -> str:
    """Why the run is superseded, or "" when it is not (ci-runners.md section 4.1). Raises GhError when a read fails
    (fail closed: the caller re-runs nothing). A newer-or-equal run (same workflow_id, event and head_branch, any
    status, an equal created_at counts as newer) supersedes it; for push and pull_request events so does a branch head
    that is no longer the run's head_sha, or a branch that no longer exists."""
    wf, event, branch = str(info.get("workflow_id") or ""), str(info.get("event") or ""), str(info.get("head_branch") or "")
    sha, created = str(info.get("head_sha") or ""), str(info.get("created_at") or "")
    if not (RUN_ID.fullmatch(wf) and event and branch and sha and created):
        err = GhError(f"run {run_id} has no workflow_id, event, head_branch, head_sha or created_at to compare")
        print(f"::warning::{err}", file=sys.stderr)
        raise err
    name = clean(str(info.get("name") or wf))
    query = "&".join(f"{k}={urllib.parse.quote(v, safe='')}" for k, v in
                     (("branch", branch), ("event", event), ("created", ">=" + created)))
    try:
        out = gh("api", "--paginate", f"repos/{repo}/actions/workflows/{wf}/runs?{query}&per_page=100",
                 "--jq", ".workflow_runs[] | {id, created_at, html_url}")
        runs = [json.loads(line) for line in out.splitlines() if line.strip()]
    except (GhError, json.JSONDecodeError) as e:
        print(f"::warning::the runs of workflow {wf} on {clean(branch)} could not be read: {e}", file=sys.stderr)
        raise GhError(f"runs of workflow {wf} could not be read: {e}") from e
    for other in runs:
        if str(other.get("id")) != str(run_id) and str(other.get("created_at", "")) >= created:
            oid = str(other.get("id", ""))
            link = f"https://github.com/{repo}/actions/runs/{oid}" if RUN_ID.fullmatch(oid) else "a newer run"
            return (f"run {run_id} is superseded by {link} (newer run of {name} on {clean(branch)})")
    if event in ("push", "pull_request"):
        try:
            head = gh("api", f"repos/{repo}/branches/{urllib.parse.quote(branch, safe='/')}", "--jq", ".commit.sha").strip()
        except GhError as e:
            if e.status == 404:   # the status gh reported, never a "404" inside the message (a branch can be named fix/404-page)
                return f"run {run_id} is superseded: the branch {clean(branch)} no longer exists"
            print(f"::warning::the head of branch {clean(branch)} could not be read: {e}", file=sys.stderr)
            raise
        if head != sha:
            return (f"run {run_id} is superseded: the head of {clean(branch)} is now {head[:7]}, not {sha[:7]}")
    return ""


def summary(text: str) -> None:
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")


def clean(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9 ._/()-]", "?", name)[:100]


WORKFLOW_NAME = re.compile(r"[A-Za-z0-9 ._-]{1,100}", re.ASCII)


def run(run_id: str, repo: str, workflow: str = "", rerun_all: str = "") -> int:
    """0 when nothing was needed or the re-run was requested; 1 only for an input or API error."""
    validate(run_id, repo)
    info = json.loads(gh("api", f"repos/{repo}/actions/runs/{run_id}", "--jq", "{run_attempt, conclusion, workflow_id, event, head_branch, head_sha, created_at, name}"))
    if info.get("conclusion") != "failure":
        summary(f"No re-run: run {run_id} did not fail (conclusion {info.get('conclusion')!r}).")
        return 0
    if info.get("run_attempt") != 1:
        summary(f"No re-run: run {run_id} is on attempt {info.get('run_attempt')}; a second failure stays failed.")
        return 0
    why = superseded(repo, run_id, info)  # before any log read: a superseded run costs no log reads
    if why:
        summary(f"No re-run: {why}.")
        return 0
    names = reclaimed_jobs(repo, failed_jobs(repo, run_id))
    if not names:
        summary(f"No re-run: no failed job in run {run_id} shows a spot reclaim.")
        return 0
    why = superseded(repo, run_id, info)  # the log reads took up to 90 seconds: a push may have landed meanwhile
    if why:
        summary(f"No re-run: {why}.")
        return 0
    whole = bool(workflow) and WORKFLOW_NAME.fullmatch(workflow) is not None and workflow in rerun_all.split()
    if whole:  # its jobs share a lock or state per attempt: re-run every job (sandbox.md section 7c item 10)
        gh("run", "rerun", run_id, "--repo", repo)
        summary(f"Re-ran the whole of run {run_id} once ({clean(workflow)} is re-run whole): spot reclaim in "
                + ", ".join(clean(n) for n in names) + ".")
        return 0
    gh("run", "rerun", run_id, "--failed", "--repo", repo)
    summary(f"Re-ran the failed jobs of run {run_id} once: spot reclaim in " + ", ".join(clean(n) for n in names) + ".")
    return 0


def scan(repo: str, workflows: str, rerun_all: str = "", now: float | None = None, clock=time.monotonic) -> int:
    """Scan mode: decide every failed first-attempt run of the named workflows that finished in the last
    SCAN_DECIDE_MINUTES (listed among the runs created in the last SCAN_LIST_HOURS), oldest finish first, within
    SCAN_BUDGET_SECONDS; runs left when the budget runs out are named and the scan fails (exit 1)."""
    names = workflows.split()
    if not names or not all(WORKFLOW_NAME.fullmatch(n) for n in names):
        raise ValueError(f"WORKFLOWS must be space-separated workflow names, got {workflows!r}.")
    if not REPO.fullmatch(repo):
        raise ValueError(f"REPO must be <owner>/<name>, got {repo!r}.")
    start = clock()
    now = time.time() if now is None else now
    stamp = lambda seconds_ago: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - seconds_ago))  # noqa: E731
    created, since = stamp(SCAN_LIST_HOURS * 3600), stamp(SCAN_DECIDE_MINUTES * 60)
    out = gh("api", "--paginate", f"repos/{repo}/actions/runs?status=failure&created=%3E%3D{created}&per_page=100",
             "--jq", ".workflow_runs[] | {id, name, run_attempt, updated_at}")
    runs = [json.loads(line) for line in out.splitlines() if line.strip()]
    found = sorted({(str(r.get("updated_at")), str(r.get("id")), str(r.get("name"))) for r in runs
                    if r.get("name") in names and r.get("run_attempt") == 1 and RUN_ID.fullmatch(str(r.get("id")))
                    and str(r.get("updated_at") or "") > since})
    if not found:
        summary(f"No re-run: no failed first-attempt run of {', '.join(clean(n) for n in names)} finished since {since}.")
        return 0
    errors = 0
    for i, (_, run_id, name) in enumerate(found):
        if clock() - start + RUN_WORST_SECONDS > SCAN_BUDGET_SECONDS:
            left = ", ".join(r for _, r, _ in found[i:])
            print(f"::error::the scan's time budget ({SCAN_BUDGET_SECONDS} s) ran out; not decided: run(s) {left} "
                  "(the next scan decides them while they finished in the last "
                  f"{SCAN_DECIDE_MINUTES} minutes)")
            return 1
        try:
            run(run_id, repo, name, rerun_all)
        except (ValueError, GhError, json.JSONDecodeError) as err:
            print(f"::error::run {run_id}: {err}")
            errors += 1
    return 1 if errors else 0


def main() -> int:
    try:
        if not os.environ.get("RUN_ID", "") and os.environ.get("WORKFLOWS", "").strip():
            return scan(os.environ.get("REPO", ""), os.environ.get("WORKFLOWS", ""), os.environ.get("RERUN_ALL", ""))
        return run(os.environ.get("RUN_ID", ""), os.environ.get("REPO", ""), os.environ.get("WORKFLOW", ""),
                   os.environ.get("RERUN_ALL", ""))
    except (ValueError, GhError, json.JSONDecodeError) as err:
        print(f"::error::{err}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
