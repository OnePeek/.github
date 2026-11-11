#!/usr/bin/env python3
"""The `ready` summary of a CI workflow (PIPE-SIMPLE v2 D6, charter principle 4: one required summary check per repo).

ready.yml runs this as the caller's last job, `ci / ready`, the one row the merge tool reads. It passes when every job
the caller's workflow holds passed, and fails otherwise, with a table: job | result | how to reproduce locally.

    NEEDS='<toJSON(needs)>' WORKFLOW_REF=<github.workflow_ref> SKIPPABLE='<job ids>' REPRODUCE='<job: command lines>' \
        python3 ready.py

Rules (fail closed: anything not known good is a failure):
1. NEEDS is the caller's `needs` context as JSON: a non-empty object of job id -> {"result": ...}. Unparsable, empty or
   another shape fails.
2. The caller's workflow file (WORKFLOW_REF, `<owner>/<repo>/.github/workflows/<file>@<ref>`, read from the checkout of
   the caller at the run's commit in the working directory) has exactly one job that calls ready.yml; that job's `if:`
   holds `always()` (so it runs after a failure) and its `needs:` names every other job of the file, and NEEDS holds
   exactly those jobs. A job the summary does not wait on would be missing from the one row the merge tool reads.
3. Each needed job's result: `success` passes. `skipped` passes only for a job listed in SKIPPABLE (a job its own `if:`
   skips, such as a path filter); a job skipped because a job it needs failed or was cancelled fails anyway, since that
   job is needed here too (rule 2). `failure`, `cancelled`, a missing or any other result fails. A SKIPPABLE name that is
   not a needed job fails (a typo would otherwise excuse nothing and hide the mistake).
REPRODUCE lines `<job>: <command>` fill the third column; a job without one reads DEFAULT_REPRODUCE.
Needs PyYAML (the workflow installs pyyaml==6.0.3 in a venv, as actions/pins does).
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys

import yaml

RESULTS = ("success", "failure", "cancelled", "skipped")
JOB_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,99}", re.ASCII)
WORKFLOW_REF = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/(\.github/workflows/[A-Za-z0-9._-]+\.ya?ml)@[^\s]+", re.ASCII)
READY_USES = re.compile(r"(?:^\./|^onepeek/\.github/)\.github/workflows/ready\.yml(?:@|$)", re.I)
DEFAULT_REPRODUCE = "vanguard local-ci <worktree> --jobs <job>"


class NotReady(Exception):
    """A rule the summary cannot check, or a broken one: the summary fails with this message."""


def parse_needs(text: str) -> dict[str, str]:
    """job id -> result from the `needs` context JSON (rule 1)."""
    try:
        data = json.loads(text or "")
    except ValueError as e:
        raise NotReady(f"the needs context is not JSON ({e}); pass needs: ${{{{ toJSON(needs) }}}}") from None
    if not isinstance(data, dict) or not data:
        raise NotReady("the needs context is empty or not an object: the ready job must need every other job")
    out = {}
    for job, value in data.items():
        if not JOB_ID.fullmatch(str(job)):
            raise NotReady(f"the needs context names {str(job)[:60]!r}, which is not a job id")
        out[job] = str(value.get("result", "")) if isinstance(value, dict) else ""
    return out


def needs_list(job: dict) -> list[str]:
    raw = job.get("needs", [])
    raw = [raw] if isinstance(raw, str) else raw
    if not isinstance(raw, list) or not all(isinstance(n, str) for n in raw):
        raise NotReady("the ready job's needs: is not a job id or a list of them")
    return raw


def check_workflow(ref: str, root: pathlib.Path, needed: set[str]) -> None:
    """Rule 2: the caller's ready job runs always and waits on every other job of its workflow."""
    m = WORKFLOW_REF.fullmatch(ref or "")
    if not m:
        raise NotReady(f"cannot tell the calling workflow from github.workflow_ref {str(ref)[:120]!r}")
    path = root / m.group(1)
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        raise NotReady(f"cannot read the calling workflow {m.group(1)}: {type(e).__name__}") from None
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    if not isinstance(jobs, dict) or not all(isinstance(j, dict) for j in jobs.values()):
        raise NotReady(f"{m.group(1)} has no jobs: mapping of jobs")
    ready = [k for k, j in jobs.items() if READY_USES.search(str(j.get("uses", "")).strip())]
    if len(ready) != 1:
        raise NotReady(f"{m.group(1)} must call ready.yml from exactly one job; found {len(ready)}")
    job = jobs[ready[0]]
    if "always()" not in str(job.get("if", "")):
        raise NotReady(f"{m.group(1)} job {ready[0]} needs `if: ${{{{ always() }}}}`: without it a failure skips the summary")
    others = set(jobs) - {ready[0]}
    declared = set(needs_list(job))
    if declared != others:
        missing, extra = sorted(others - declared), sorted(declared - others)
        raise NotReady(f"{m.group(1)} job {ready[0]} must need every other job: missing {missing or 'none'}, "
                       f"not a job {extra or 'none'}")
    if needed != others:
        raise NotReady(f"the needs context ({sorted(needed)}) is not the jobs of {m.group(1)} ({sorted(others)}): "
                       "pass needs: ${{ toJSON(needs) }}")


def reproduce_map(text: str) -> dict[str, str]:
    out = {}
    for line in (text or "").splitlines():
        job, sep, cmd = line.partition(":")
        if sep and JOB_ID.fullmatch(job.strip()) and cmd.strip():
            out[job.strip()] = cmd.strip()
    return out


def cell(text: str) -> str:
    """Text safe inside one markdown table cell."""
    return re.sub(r"[\r\n|`]", " ", text)[:200]


def verdict(needs: dict[str, str], skippable: set[str]) -> list[tuple[str, str, bool]]:
    """(job, shown result, ok) per needed job, rule 3."""
    unknown = sorted(skippable - set(needs))
    if unknown:
        raise NotReady(f"skippable names {unknown}, which the ready job does not need")
    rows = []
    for job in sorted(needs):
        result = needs[job]
        if result == "success":
            rows.append((job, "success", True))
        elif result == "skipped" and job in skippable:
            rows.append((job, "skipped by its own if (listed skippable)", True))
        elif result == "skipped":
            rows.append((job, "skipped, and not listed skippable", False))
        elif result in RESULTS:
            rows.append((job, result, False))
        else:
            rows.append((job, f"unknown result {cell(result)!r}", False))
    return rows


def run(env: dict, root: pathlib.Path) -> tuple[int, str]:
    """(exit code, table text)."""
    try:
        needs = parse_needs(env.get("NEEDS", ""))
        check_workflow(env.get("WORKFLOW_REF", ""), root, set(needs))
        skippable = set(env.get("SKIPPABLE", "").split())
        bad = [s for s in skippable if not JOB_ID.fullmatch(s)]
        if bad:
            raise NotReady(f"skippable holds {cell(bad[0])!r}, which is not a job id")
        rows = verdict(needs, skippable)
    except NotReady as e:
        table = "| job | result | how to reproduce locally |\n|---|---|---|\n" \
                f"| (ready) | failed: {cell(str(e))} | read the caller's workflow and ready.yml's header |"
        return 1, table + f"\n::error::{e}"
    how = reproduce_map(env.get("REPRODUCE", ""))
    lines = ["| job | result | how to reproduce locally |", "|---|---|---|"]
    for job, shown, ok in rows:
        cmd = "" if ok else cell(how.get(job, DEFAULT_REPRODUCE.replace("<job>", job)))
        lines.append(f"| {job} | {shown} | {'`' + cmd + '`' if cmd else ''} |")
    failed = [job for job, _, ok in rows if not ok]
    text = "\n".join(lines)
    if failed:
        text += f"\n::error::not ready: {', '.join(failed)} did not pass; see the table above."
    return (1 if failed else 0), text


def main() -> int:
    code, text = run(dict(os.environ), pathlib.Path.cwd())
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as f:
                f.write("### ready\n\n" + "\n".join(ln for ln in text.splitlines() if not ln.startswith("::")) + "\n")
        except OSError:
            print("::warning::Could not write the job summary; the table above and the exit status decide.")
    return code


if __name__ == "__main__":
    sys.exit(main())
