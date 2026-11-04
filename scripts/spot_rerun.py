#!/usr/bin/env python3
"""Automatic re-run after a spot reclaim (onepeek-docs architecture/lld/infra/ci-runners.md section 4, row R1).

A caller runs spot-rerun.yml on `workflow_run: completed` with conclusion `failure`. This script reads the failed jobs
of that run, the log of each and the check-run annotations of each. When either holds the runner's shutdown line ("The
runner has received a shutdown signal", in the log) or GitHub's "lost communication with the server" (usually an
annotation with no log), and the run is on its first attempt, it re-runs
the failed jobs once (`gh run rerun <id> --failed`) and writes the reason (the job names) to the step summary.
Otherwise it does nothing: a second reclaim, or an ordinary failure, stays failed and visible.

    RUN_ID=<github.event.workflow_run.id> REPO=<github.repository> python3 spot_rerun.py

RUN_ID and REPO are checked strictly (digits only; owner/name). The token is GH_TOKEN (actions: read, checks: read, and
actions: write to re-run). Standard library only; the `gh` CLI does the API calls.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

MARKERS = ("The runner has received a shutdown signal", "lost communication with the server")
RUN_ID = re.compile(r"[1-9][0-9]{0,19}", re.ASCII)
REPO = re.compile(r"(?!\.+/)[A-Za-z0-9._-]+/(?!\.+$)[A-Za-z0-9._-]+", re.ASCII)


class GhError(Exception):
    """A gh call failed."""


def gh(*args: str) -> str:
    """Run the gh CLI and return stdout; tests replace this."""
    done = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise GhError(f"gh {' '.join(args[:2])} failed: {done.stderr.strip()[:300]}")
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


def reclaimed_jobs(repo: str, jobs: list[dict]) -> list[str]:
    """Names of the failed jobs whose log or annotations show a reclaim. A source that cannot be read counts as no
    match for that source only. A job that prints a marker itself can force at most one extra re-run (attempt 1 only)."""
    names: list[str] = []
    for job in jobs:
        job_id = str(job.get("id", ""))
        if not RUN_ID.fullmatch(job_id):
            continue
        found = False
        for read in (lambda: gh("api", f"repos/{repo}/actions/jobs/{job_id}/logs"),
                     lambda: annotations_text(repo, job_id)):
            try:
                found = found or reclaimed(read())
            except GhError:
                pass
        if found:
            names.append(str(job.get("name", job_id)))
    return names


def summary(text: str) -> None:
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")


def clean(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9 ._/()-]", "?", name)[:100]


def run(run_id: str, repo: str) -> int:
    """0 when nothing was needed or the re-run was requested; 1 only for an input or API error."""
    validate(run_id, repo)
    info = json.loads(gh("api", f"repos/{repo}/actions/runs/{run_id}", "--jq", "{run_attempt, conclusion}"))
    if info.get("conclusion") != "failure":
        summary(f"No re-run: run {run_id} did not fail (conclusion {info.get('conclusion')!r}).")
        return 0
    if info.get("run_attempt") != 1:
        summary(f"No re-run: run {run_id} is on attempt {info.get('run_attempt')}; a second failure stays failed.")
        return 0
    names = reclaimed_jobs(repo, failed_jobs(repo, run_id))
    if not names:
        summary(f"No re-run: no failed job in run {run_id} shows a spot reclaim.")
        return 0
    gh("run", "rerun", run_id, "--failed", "--repo", repo)
    summary(f"Re-ran the failed jobs of run {run_id} once: spot reclaim in " + ", ".join(clean(n) for n in names) + ".")
    return 0


def main() -> int:
    try:
        return run(os.environ.get("RUN_ID", ""), os.environ.get("REPO", ""))
    except (ValueError, GhError, json.JSONDecodeError) as err:
        print(f"::error::{err}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
