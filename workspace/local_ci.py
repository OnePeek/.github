#!/usr/bin/env python3
"""Run a repo's pull-request CI locally, from its own workflow files, before pushing.

    python3 .github/workspace/local_ci.py <repo dir> [--jobs layout,checks] [--skip-docker] [--list]

For every workflow that runs on `pull_request`, every job's `run:` steps run in order in the repo directory, with the
job's and step's `env` (expressions left out), stopping at the first failure. `uses:` steps (checkout, setup-*) are
skipped: the laptop already has the tools; reusable-workflow jobs are listed as skipped, and the shared checks they
stand for run here directly (actionlint with shellcheck, the runner-label lint). Steps that need GitHub's API or
AWS (they read GH_TOKEN, assume a role, or call `gh api`) are skipped and listed, never run.

Exit 0 when every step ran and passed; 1 at the first failure (its output is shown); 2 bad input.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

import yaml

WORKSPACE = pathlib.Path(os.environ.get("ONEPEEK_WORKSPACE") or pathlib.Path(__file__).resolve().parents[2])
EXPR = re.compile(r"\$\{\{.*?\}\}")
REMOTE = re.compile(r"\bgh api\b|\bgh pr\b|\baws \b|GH_TOKEN|GITHUB_TOKEN|configure-aws-credentials|terraform (plan|apply)|\bgit push\b")


def pr_workflows(repo: pathlib.Path):
    for path in sorted((repo / ".github/workflows").glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        on = doc.get("on", doc.get(True)) or {}
        triggers = [on] if isinstance(on, str) else list(on)
        if "pull_request" in triggers:
            yield path, doc


def clean_env(env: dict | None) -> dict[str, str]:
    out = {}
    for k, v in (env or {}).items():
        v = str(v)
        if not EXPR.search(v):
            out[str(k)] = v
    return out


_VENV: pathlib.Path | None = None


def venv_bin() -> pathlib.Path:
    """One throwaway virtual environment per run, first on PATH, so a step's `pip install` never touches the laptop's Python."""
    global _VENV
    if _VENV is None:
        import tempfile
        import atexit
        root = pathlib.Path(tempfile.mkdtemp(prefix="local-ci-"))
        atexit.register(shutil.rmtree, root, ignore_errors=True)  # nothing is left behind on the laptop
        _VENV = root / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(_VENV)], check=True, capture_output=True)
    return _VENV / "bin"


def run_step(cmd: str, cwd: pathlib.Path, env: dict[str, str], shell: str) -> tuple[int, str, float]:
    t = time.time()
    env = {**env, "PATH": f"{venv_bin()}:{os.environ['PATH']}", "VIRTUAL_ENV": str(venv_bin().parent)}
    argv = ["bash", "-eo", "pipefail", "-c", cmd] if shell in ("", "bash") or shell.startswith("bash") else ["sh", "-ec", cmd]
    # The steps see the laptop's environment minus local_ci's own settings (ONEPEEK_WORKSPACE leaked into a repo's tests),
    # and an empty TF_PLUGIN_CACHE_DIR is dropped (a step's `mkdir -p "$TF_PLUGIN_CACHE_DIR"` fails on it).
    base = {k: v for k, v in os.environ.items() if k != "ONEPEEK_WORKSPACE" and not (k == "TF_PLUGIN_CACHE_DIR" and not v)}
    r = subprocess.run(argv, cwd=cwd, env={**base, "CI": "true", **env}, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr), time.time() - t


def shared_checks(repo: pathlib.Path, kind: str, name: str) -> list[tuple[str, list[str]]]:
    checks = []
    if shutil.which("actionlint"):
        checks.append(("actionlint (with shellcheck)", ["actionlint"]))
    lint = WORKSPACE / ".github/scripts/check_runner_labels.py"
    if lint.exists():
        checks.append(("runner-label lint", [sys.executable, str(lint), "--kind", kind,
                                             "--repo", f"OnePeek/{name}", str(repo / ".github/workflows")]))
    return checks


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("repo")
    ap.add_argument("--jobs", default="", help="comma-separated job ids to run (default: all)")
    ap.add_argument("--skip-docker", action="store_true", help="skip steps that call docker")
    ap.add_argument("--list", action="store_true", help="print the steps, run nothing")
    a = ap.parse_args(argv)
    repo = pathlib.Path(a.repo).resolve()
    if not (repo / ".github/workflows").is_dir():
        print(f"{repo}: no .github/workflows", file=sys.stderr)
        return 2
    # The repository's name from its remote, not the folder (worktrees have other names).
    url = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()
    name = url.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1] or repo.name
    kind = "public" if name in (".github", "plugin-owl") else "private"
    wanted = {j for j in a.jobs.split(",") if j}
    skipped: list[str] = []
    started = time.time()

    for check, cmd in shared_checks(repo, kind, name):
        if a.list:
            print(f"[shared] {check}")
            continue
        r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
        print(f"{'PASS' if r.returncode == 0 else 'FAIL'}  [shared] {check}")
        if r.returncode:
            print((r.stdout + r.stderr)[-4000:])
            return 1

    for path, doc in pr_workflows(repo):
        wf_env = clean_env(doc.get("env"))
        for job_id, job in (doc.get("jobs") or {}).items():
            if wanted and job_id not in wanted:
                continue
            inputs: dict[str, str] = {}
            if "uses" in job:
                ref = job["uses"].split("@")[0]
                local = WORKSPACE / ".github" / ref.split("OnePeek/.github/", 1)[-1] if ref.startswith("OnePeek/.github/") else None
                if not local or not local.exists():
                    skipped.append(f"{path.name}:{job_id} (reusable workflow {ref})")
                    continue
                # Run the shared workflow's steps from the local .github checkout, with this caller's literal inputs.
                called = yaml.safe_load(local.read_text(encoding="utf-8")) or {}
                on = called.get("on", called.get(True)) or {}
                defaults = {k: str(v.get("default", "")) for k, v in ((on.get("workflow_call") or {}).get("inputs") or {}).items()}
                inputs = {**defaults, **{k: str(v) for k, v in (job.get("with") or {}).items() if not EXPR.search(str(v))}}
                inputs.setdefault("runner", "github")
                for sub_id, sub in (called.get("jobs") or {}).items():
                    if wanted and job_id not in wanted:
                        continue
                    rc = run_job(f"{path.name}:{job_id}/{sub_id}", sub, clean_env(called.get("env")), inputs, repo, a, skipped)
                    if rc:
                        return rc
                continue
            wf_dir = ((doc.get("defaults") or {}).get("run") or {}).get("working-directory")
            if wf_dir and not ((job.get("defaults") or {}).get("run") or {}).get("working-directory"):
                job = {**job, "defaults": {"run": {"working-directory": wf_dir}}}
            rc = run_job(f"{path.name}:{job_id}", job, wf_env, inputs, repo, a, skipped)
            if rc:
                return rc

    if skipped:
        print("\nSkipped (CI runs these):")
        for s in skipped:
            print(f"  - {s}")
    if not a.list:
        print(f"\nLOCAL CI OK ({time.time() - started:.0f}s)")
    return 0


def substitute(text: str, inputs: dict[str, str]) -> str:
    return re.sub(r"\$\{\{\s*inputs\.([A-Za-z0-9_-]+)\s*\}\}", lambda m: inputs.get(m.group(1), m.group(0)), text)


def run_job(label0: str, job: dict, wf_env: dict, inputs: dict, repo: pathlib.Path, a, skipped: list) -> int:
    if True:
        if True:
            job_env = {**wf_env, **clean_env(job.get("env"))}
            workdir = ((job.get("defaults") or {}).get("run") or {}).get("working-directory", ".")
            for i, step in enumerate(job.get("steps") or []):
                cmd = step.get("run")
                if not cmd:
                    continue
                cmd = substitute(cmd, inputs)
                label = f"{label0}: {step.get('name') or cmd.strip().splitlines()[0][:70]}"
                env = {**job_env, **{k: substitute(str(v), inputs) for k, v in (step.get("env") or {}).items() if not EXPR.search(substitute(str(v), inputs))}}
                if REMOTE.search(cmd) or any(k in (step.get("env") or {}) for k in ("GH_TOKEN", "GITHUB_TOKEN")):
                    skipped.append(f"{label} (needs GitHub or AWS)")
                    continue
                if any("secrets." in str(v) for v in {**(job.get("env") or {}), **(step.get("env") or {})}.values()):
                    skipped.append(f"{label} (needs a repository secret)")
                    continue
                if re.search(r"\bgitleaks (git|detect)\b", cmd):
                    skipped.append(f"{label} (history scan: CI scans the pull request's commits; the pre-commit hook scans each commit)")
                    continue
                if re.match(r"(Install|Set up|Download) ", str(step.get("name") or "")):
                    skipped.append(f"{label} (tool install; the laptop has its own)")
                    continue
                if a.skip_docker and re.search(r"\bdocker\b", cmd):
                    skipped.append(f"{label} (docker)")
                    continue
                if a.list:
                    print(label)
                    continue
                cwd = repo / step.get("working-directory", workdir)
                cmd = EXPR.sub("", cmd)
                # A tool a skipped install step would have put in the working directory: use the laptop's.
                cmd = re.sub(r"(?m)(^|\s)\./([A-Za-z0-9_-]+)\b",
                             lambda m: m.group(0) if (cwd / m.group(2)).exists() or not shutil.which(m.group(2)) else m.group(1) + m.group(2), cmd)
                rc, out, secs = run_step(cmd, cwd, env, str(step.get("shell") or job.get("shell") or ""))
                print(f"{'PASS' if rc == 0 else 'FAIL'}  {label}  ({secs:.0f}s)")
                if rc:
                    print(out[-6000:])
                    return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
