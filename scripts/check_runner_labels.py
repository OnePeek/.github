#!/usr/bin/env python3
"""Workflow-lint rules for runner labels (onepeek-docs architecture/lld/infra/ci-runners.md section 5.4).

  1. Public repos: a workflow that is not `on: workflow_call` never names `self-hosted` or an `onepeek-*` label; a
     `workflow_call` workflow names `onepeek-*` labels only inside the exact `inputs.runner` mapping of section 5.3;
     no public workflow reads `vars.RUNNER`.
  2. Private repos: `runs-on` is one of the forms of section 5.3 (no other labels, no `ghr-*`); jobs listed in
     .github/workflow-lint/github-only-jobs.yaml use the `github-only` form (plain `ubuntu-latest`). Exempt by name:
     onepeek-infra's ci-runner-canary.yml and ci-runner-image.yml, which name `onepeek-arm64` and
     `onepeek-arm64-canary` directly.
  3. Every job that can run on `onepeek-*` sets `timeout-minutes`: at most 60 (fast, arm-build), 150 (stateful).
  4. No `pull_request_target`; no `workflow_run` job on `onepeek-*`.

Usage: check_runner_labels.py --kind public|private --repo OnePeek/<name> [--list FILE] WORKFLOW_DIR
Exit status 1 when any rule is broken.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

EXEMPT = {("OnePeek/onepeek-infra", "ci-runner-canary.yml"), ("OnePeek/onepeek-infra", "ci-runner-image.yml")}
EXEMPT_LABELS = {"onepeek-arm64", "onepeek-arm64-canary"}
MAPPING_REPO = "OnePeek/.github"  # the only public repo whose workflow_call workflows may hold the mapping
PROD_GUARD = "inputs.environment == 'prod' && 'ubuntu-latest' || "
LIMITS = {"light": 30, "fast": 60, "arm-build": 60, "stateful": 150}


def _forms(var: str) -> dict[str, str]:
    """The exact runs-on expressions of section 5.3, by job class, for `var` (inputs.runner or vars.RUNNER).

    In the shared workflows the arm-build class falls back to ubuntu-latest (with QEMU) until hosted ARM is proven
    on a private repo, so there it equals the fast form."""
    arm_hosted = "ubuntu-latest" if var == "inputs.runner" else "ubuntu-24.04-arm"
    def three(onepeek: str, canary: str, hosted: str) -> str:
        return f"{var} == 'onepeek' && '{onepeek}' || {var} == 'canary' && '{canary}' || '{hosted}'"
    return {
        "light": three("onepeek-arm64-small", "onepeek-arm64-canary", "ubuntu-latest"),
        "fast": three("onepeek-arm64", "onepeek-arm64-canary", "ubuntu-latest"),
        "arm-build": three("onepeek-arm64", "onepeek-arm64-canary", arm_hosted),
        "stateful": three("onepeek-arm64-od", "onepeek-arm64-od", "ubuntu-latest"),
        "github-only": "ubuntu-latest",
    }


def _expr(value: object) -> str | None:
    """The expression inside `${{ ... }}` with single spaces, or None when runs-on is not exactly one expression."""
    if not isinstance(value, str):
        return None
    m = re.fullmatch(r"\$\{\{(.*)\}\}", value.strip(), re.S)
    return " ".join(m.group(1).split()) if m else None


def classify(runs_on: object, var: str, allow_prod_guard: bool) -> str | None:
    """The job class when runs-on is one of the allowed forms, else None."""
    if runs_on == "ubuntu-latest":
        return "github-only"
    expr = _expr(runs_on)
    if expr is None:
        return None
    if allow_prod_guard and expr.startswith(PROD_GUARD):
        expr = expr[len(PROD_GUARD):]
    for cls, form in _forms(var).items():
        if cls != "github-only" and expr == form:
            return cls
    return None


def _strip_runs_on(node: object) -> object:
    if isinstance(node, dict):
        return {k: _strip_runs_on(v) for k, v in node.items() if k != "runs-on"}
    if isinstance(node, list):
        return [_strip_runs_on(v) for v in node]
    return node


def _triggers(doc: dict) -> dict:
    on = doc.get("on", doc.get(True, {}))  # YAML 1.1 reads the key `on` as True
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return {k: None for k in on}
    return on or {}


def load_listed(path: Path | None) -> set[tuple[str, str, str]]:
    if path is None:
        return set()
    doc = yaml.safe_load(path.read_text()) or {}
    return {(e["repo"], e["workflow"], e["job"]) for e in doc.get("jobs") or []}


def check_file(path: Path, kind: str, repo: str, listed: set[tuple[str, str, str]]) -> list[str]:
    errors: list[str] = []
    name = path.name
    where = f"{repo}:{name}"
    doc = yaml.safe_load(path.read_text()) or {}
    triggers = _triggers(doc)
    callable_ = "workflow_call" in triggers
    if "pull_request_target" in triggers:
        errors.append(f"{where}: rule 4: pull_request_target is not allowed.")
    jobs = doc.get("jobs") or {}
    if kind == "public":
        for job_id, job in jobs.items():
            given = (job.get("with") or {}).get("runner")
            if given is not None and given != "github":
                errors.append(f"{where}: job {job_id}: rule 1: a public repo passes no runner other than github, got {given!r}.")
        errors += _public(where, name, doc, jobs, callable_)
    for job_id, job in jobs.items():
        if "runs-on" not in job:
            continue
        ro = job["runs-on"]
        label = f"{where}: job {job_id}"
        if kind == "public":
            cls = classify(ro, "inputs.runner", True) if callable_ and repo == MAPPING_REPO else None
            if callable_ and cls is None and ro != "ubuntu-latest":
                errors.append(f"{label}: rule 1: runs-on in a public workflow_call workflow must be ubuntu-latest"
                              + (" or a mapping form of section 5.3." if repo == MAPPING_REPO else "."))
                continue
        else:
            cls = classify(ro, "vars.RUNNER", False)
            if cls is None and (repo, name) in EXEMPT and ro in EXEMPT_LABELS:
                cls = "fast"
            if cls is None:
                errors.append(f"{label}: rule 2: runs-on {ro!r} is not one of the forms of ci-runners.md section 5.3.")
                continue
        if (repo, name, job_id) in listed and ro != "ubuntu-latest":
            errors.append(f"{label}: rule 2: listed in github-only-jobs.yaml, so runs-on must be ubuntu-latest.")
        if cls is not None and cls != "github-only" and _names_runner(ro):
            limit = LIMITS[cls]
            tm = job.get("timeout-minutes")
            if not isinstance(tm, int) or isinstance(tm, bool) or tm > limit:
                errors.append(f"{label}: rule 3: timeout-minutes must be a number of at most {limit}, got {tm!r}.")
            if "workflow_run" in triggers:
                errors.append(f"{label}: rule 4: a workflow_run workflow must not run jobs on onepeek-*.")
    return errors


def _names_runner(ro: object) -> bool:
    return bool(re.search(r"self-hosted|onepeek-|ghr-|vars\.RUNNER", json.dumps(ro)))


def _public(where: str, name: str, doc: dict, jobs: dict, callable_: bool) -> list[str]:
    errors: list[str] = []
    for job_id, job in jobs.items():
        ro = job.get("runs-on")
        if ro is None or not _names_runner(ro):
            continue
        if not callable_:
            errors.append(f"{where}: job {job_id}: rule 1: a workflow that is not workflow_call never names self-hosted or onepeek-* labels.")
        elif "vars.RUNNER" in json.dumps(ro) or classify(ro, "inputs.runner", True) is None:
            errors.append(f"{where}: job {job_id}: rule 1: runs-on may name onepeek-* labels only inside the exact inputs.runner mapping.")
    rest = json.dumps(_strip_runs_on(doc), default=str)
    if "vars.RUNNER" in rest:
        errors.append(f"{where}: rule 1: no public workflow reads vars.RUNNER.")
    if re.search(r"self-hosted|onepeek-arm64", rest):
        errors.append(f"{where}: rule 1: onepeek-* and self-hosted labels may appear only in runs-on.")
    return errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kind", choices=["public", "private"], required=True)
    ap.add_argument("--repo", required=True, help="OnePeek/<name>")
    ap.add_argument("--list", type=Path, help="github-only-jobs.yaml")
    ap.add_argument("dir", type=Path)
    a = ap.parse_args(argv)
    listed = load_listed(a.list)
    errors: list[str] = []
    for p in sorted([*a.dir.glob("*.yml"), *a.dir.glob("*.yaml")]):
        errors += check_file(p, a.kind, a.repo, listed)
    for e in errors:
        print(f"::error::{e}")
    if not errors:
        print("runner labels: ok")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
