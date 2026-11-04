#!/usr/bin/env python3
"""Run a repo's pull-request CI locally, from its own workflow files, before pushing.

    python3 .github/workspace/local_ci.py <repo dir> [--jobs layout,checks] [--skip-docker] [--list]

For every workflow that runs on `pull_request`, every job's `run:` steps run in order in the repo directory, with the
job's and step's `env` (github.* and runner.* values filled in for a pull request into dev here, other expressions left out), stopping at the first failure. `uses:` steps (checkout, setup-*) are
skipped: the laptop already has the tools; reusable-workflow jobs are listed as skipped, and the shared checks they
stand for run here directly (actionlint with shellcheck, the runner-label lint). Steps that need GitHub's API or
AWS (they read GH_TOKEN, assume a role, or call `gh api`) are skipped and listed, never run.

Exit 0 when every step ran and passed; 1 at the first failure (its output is shown); 2 bad input.
"""
from __future__ import annotations

import argparse
import atexit
import contextlib
import os
import platform
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

import yaml

WORKSPACE = pathlib.Path(os.environ.get("ONEPEEK_WORKSPACE") or pathlib.Path(__file__).resolve().parents[2])
EXPR = re.compile(r"\$\{\{.*?\}\}")
CONTEXT_REF = re.compile(r"\$\{\{\s*((?:github|runner)\.[A-Za-z0-9_.]+)\s*\}\}")
# The sibling repositories the contract job expects checked out inside the repo, by the folder name CI gives them.
SIBLINGS = {"_backend": "backend-service", "_web": "onepeek-web", "_infra": "onepeek-infra"}
USES_SIBLINGS = re.compile(r"(?<![A-Za-z0-9_])_(backend|web|infra)(?![A-Za-z0-9_])")
# Linters CI installs with a setup action; when the laptop lacks one, its step is skipped with a reason, not failed.
OPTIONAL_TOOLS = ("tflint", "trivy")
# The versions CI pins (foundation-playbook §8a P3; scripts/test_local_ci.py keeps the shared workflows at these).
# A different uv on the laptop fails the run with the fix; a different tflint skips its steps with the reason.
UV_VERSION = "0.12.0"
TFLINT_VERSION = "0.65.0"
# One provider cache for every local run and worktree (§8a P6), unless the laptop sets TF_PLUGIN_CACHE_DIR itself.
SHARED_PLUGIN_CACHE = pathlib.Path.home() / ".cache" / "onepeek" / "terraform-plugins"
REMOTE = re.compile(r"\bgh api\b|\bgh pr\b|\baws \b|GH_TOKEN|GITHUB_TOKEN|configure-aws-credentials|terraform (plan|apply)|\bgit push\b|(?m:terraform\b[^\n]*\binit\b(?![^\n]*-backend=false))|\bgit clone\b[^\n]*github\.com")


def tool_version(tool: str) -> str | None:
    """The installed version of uv or tflint (`uv 0.12.0 (...)`, `TFLint version 0.65.0`), or None when missing."""
    if not shutil.which(tool):
        return None
    r = subprocess.run([tool, "--version"], capture_output=True, text=True)
    m = re.search(r"(\d+\.\d+\.\d+)", r.stdout + r.stderr)
    return m.group(1) if m else "unknown"


def uv_problem() -> str:
    """Why the laptop's uv cannot stand in for CI's, or "" (a missing uv is handled where it is needed)."""
    v = tool_version("uv")
    if v is None or v == UV_VERSION:
        return ""
    return f"uv {v} is installed; CI pins uv {UV_VERSION}. Install it (`uv self update {UV_VERSION}`, or your package manager), then run again."


def packages_token() -> str:
    """The laptop's gh login token when it can read packages (the CI install's NODE_AUTH_TOKEN), else ""."""
    if not shutil.which("gh"):
        return ""
    status = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    if "read:packages" not in status.stdout + status.stderr:
        return ""
    tok = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
    return tok.stdout.strip() if tok.returncode == 0 else ""


def tflint_problem() -> str:
    """Why tflint steps are skipped here (missing, or not CI's version), or ""."""
    v = tool_version("tflint")
    if v is None:
        return "tflint is not installed on this laptop"
    if v != TFLINT_VERSION:
        return f"tflint {v} is installed; CI pins {TFLINT_VERSION}"
    return ""


def plugin_cache() -> pathlib.Path:
    """The laptop's TF_PLUGIN_CACHE_DIR when set, else the one shared cache (§8a P6); it exists afterwards."""
    d = pathlib.Path(os.environ.get("TF_PLUGIN_CACHE_DIR") or SHARED_PLUGIN_CACHE)
    d.mkdir(parents=True, exist_ok=True)
    return d


def pr_workflows(repo: pathlib.Path):
    for path in sorted((repo / ".github/workflows").glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        on = doc.get("on", doc.get(True)) or {}
        triggers = [on] if isinstance(on, str) else list(on)
        if "pull_request" in triggers:
            yield path, doc


_ROOT: pathlib.Path | None = None
_VENV: pathlib.Path | None = None


def run_root() -> pathlib.Path:
    """The run's one throwaway directory (runner.temp, the virtual environment); removed when the process exits."""
    global _ROOT
    if _ROOT is None:
        _ROOT = pathlib.Path(tempfile.mkdtemp(prefix="local-ci-"))
        atexit.register(shutil.rmtree, _ROOT, ignore_errors=True)  # nothing is left behind on the laptop
    return _ROOT


def contexts(repo: pathlib.Path, name: str) -> dict[str, str]:
    """The runner contexts a workflow reads, as they are for a pull request into dev checked here (runner.temp is created lazily)."""
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True).stdout.strip()
    def rev(*args: str) -> str:
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""
    sha = rev("rev-parse", "HEAD")
    base = rev("merge-base", "HEAD", "origin/dev") or sha
    ctx = {
        "github.sha": sha,
        "github.event.pull_request.head.sha": sha,
        "github.event.pull_request.base.sha": base,  # what the pull request branched from, so only its own changes are checked
        "github.workspace": str(repo),
        "github.repository": f"OnePeek/{name}",
        "github.repository_owner": "OnePeek",
        "github.event_name": "pull_request",
        "github.base_ref": "dev",
        "github.head_ref": head if head and head != "HEAD" else "local",
        "runner.os": {"Darwin": "macOS", "Windows": "Windows"}.get(platform.system(), "Linux"),
        "runner.temp": None,  # filled on first use
    }
    return {k: v for k, v in ctx.items() if v != ""}


def expand(text: str, ctx: dict[str, str | None] | None) -> str:
    """Replace the well-known github.* and runner.* expressions; anything else is left as it is."""
    def one(m):
        key = m.group(1)
        if not ctx or key not in ctx:
            return m.group(0)
        if ctx[key] is None and key == "runner.temp":
            tmp = run_root() / "temp"
            tmp.mkdir(exist_ok=True)
            ctx[key] = str(tmp)
        return ctx[key] if ctx[key] is not None else m.group(0)
    return CONTEXT_REF.sub(one, text)


def clean_env(env: dict | None, ctx: dict | None = None) -> dict[str, str]:
    out = {}
    for k, v in (env or {}).items():
        v = expand(str(v), ctx)
        if not EXPR.search(v):
            out[str(k)] = v
    return out


def unresolved_env(*envs: dict | None, ctx: dict | None = None) -> set[str]:
    """Names of env entries whose value is an expression local_ci cannot know (a matrix value, a step output, a secret)."""
    return {str(k) for env in envs for k, v in (env or {}).items() if EXPR.search(expand(str(v), ctx))}


def venv_bin() -> pathlib.Path:
    """One throwaway virtual environment per run, first on PATH, so a step's `pip install` never touches the laptop's Python."""
    global _VENV
    if _VENV is None:
        _VENV = run_root() / "venv"
        # --system-site-packages: a step sees the packages local_ci itself runs with (PyYAML), as CI's runner Python has them;
        # a step's own `pip install` still lands in the venv.
        subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", str(_VENV)], check=True, capture_output=True)
    return _VENV / "bin"


def run_step(cmd: str, cwd: pathlib.Path, env: dict[str, str], shell: str) -> tuple[int, str, float]:
    t = time.time()
    env = {**env, "PATH": f"{venv_bin()}:{os.environ['PATH']}", "VIRTUAL_ENV": str(venv_bin().parent)}
    argv = ["bash", "-eo", "pipefail", "-c", cmd] if shell in ("", "bash") or shell.startswith("bash") else ["sh", "-ec", cmd]
    # The steps see the laptop's environment minus local_ci's own settings (ONEPEEK_WORKSPACE leaked into a repo's tests),
    # and an empty TF_PLUGIN_CACHE_DIR is dropped (a step's `mkdir -p "$TF_PLUGIN_CACHE_DIR"` fails on it).
    base = {k: v for k, v in os.environ.items() if k != "ONEPEEK_WORKSPACE" and not (k == "TF_PLUGIN_CACHE_DIR" and not v)}
    # The files a step appends to (GITHUB_ENV and friends), in the run's throwaway directory.
    files = {k: str(run_root() / k.lower()) for k in ("GITHUB_ENV", "GITHUB_OUTPUT", "GITHUB_PATH", "GITHUB_STEP_SUMMARY")}
    for f in files.values():
        pathlib.Path(f).touch()
    # No cloud credentials or settings from the laptop reach a step (as the contract job does in CI).
    base = {k: v for k, v in base.items() if not k.startswith("AWS_")}
    base.update({"AWS_SHARED_CREDENTIALS_FILE": "/dev/null", "AWS_CONFIG_FILE": "/dev/null"})
    # Terraform providers go to one shared cache, not a copy per repo or worktree (a workflow's own value is CI's).
    env = {**env, "TF_PLUGIN_CACHE_DIR": str(plugin_cache())}
    r = subprocess.run(argv, cwd=cwd, env={**base, "CI": "true", **files, **env}, capture_output=True, text=True)
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


def git(*args: str, cwd: pathlib.Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


@contextlib.contextmanager
def sibling_checkouts(repo: pathlib.Path, own: str, workspace: pathlib.Path = None):
    """Provide _backend, _web and _infra inside the repo, as the contract job's checkouts do in CI.

    The repo itself stands for its own side (a link); the others are detached read-only worktrees of the sibling repo
    at origin/dev (a pull request into dev), added and removed here, so the sibling's own working tree is never touched.
    Yields (provided, reason): reason is set (and nothing is left behind) when a sibling or tool is missing.
    """
    workspace = workspace or WORKSPACE
    made: list[tuple[str, pathlib.Path, pathlib.Path | None]] = []  # (folder, path, sibling repo if a worktree)
    reason = ""
    try:
        if not shutil.which("uv"):
            reason = "uv is not installed"
        for folder, sibling_name in SIBLINGS.items():
            if reason:
                break
            target = repo / folder
            if target.exists() or target.is_symlink():
                continue  # the repo already has it: never overwrite
            if sibling_name == own:
                target.symlink_to(repo, target_is_directory=True)
                made.append((folder, target, None))
                continue
            src = workspace / sibling_name
            if not (src / ".git").exists():
                reason = f"{sibling_name} is not checked out at {src}"
                break
            git("fetch", "--quiet", "origin", "dev", cwd=src)  # best effort; offline keeps the last known dev
            if git("rev-parse", "--verify", "--quiet", "origin/dev", cwd=src).returncode:
                reason = f"{sibling_name} has no origin/dev"
                break
            r = git("worktree", "add", "--detach", "--quiet", str(target), "origin/dev", cwd=src)
            if r.returncode:
                reason = f"cannot check out {sibling_name} at origin/dev: {r.stderr.strip()[-200:]}"
                break
            made.append((folder, target, src))
        yield (not reason, reason)
    finally:
        for folder, target, src in made:
            if src is None:
                target.unlink(missing_ok=True)
            else:
                git("worktree", "remove", "--force", str(target), cwd=src)
                if target.exists():
                    shutil.rmtree(target, ignore_errors=True)
                git("worktree", "prune", cwd=src)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("repo")
    ap.add_argument("--jobs", default="", help="comma-separated job ids to run (default: all)")
    ap.add_argument("--skip-docker", action="store_true", help="skip steps that call docker")
    ap.add_argument("--list", action="store_true", help="print the steps, run nothing")
    a = ap.parse_args(argv)
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))  # so a stopped run still removes its checkouts and temp files
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
    ctx = contexts(repo, name)
    a.ctx, a.own = ctx, name
    if not a.list and (problem := uv_problem()):
        print(f"FAIL  {problem}")
        return 1

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
        wf_env = clean_env(doc.get("env"), ctx)
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
                inputs = {**defaults, **{k: expand(str(v), ctx) for k, v in (job.get("with") or {}).items() if not EXPR.search(expand(str(v), ctx))}}
                inputs.setdefault("runner", "github")
                for sub_id, sub in (called.get("jobs") or {}).items():
                    if wanted and job_id not in wanted:
                        continue
                    rc = run_job(f"{path.name}:{job_id}/{sub_id}", sub, clean_env(called.get("env"), ctx), inputs, repo, a, skipped)
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


def calls_docker_script(cmd: str, cwd: pathlib.Path) -> bool:
    """True when the step runs a script in the repo (`scripts/x.sh args`) whose text calls docker."""
    first = (cmd.strip().split() or [""])[0]
    path = cwd / first
    return bool(first) and not first.startswith("/") and path.is_file() and bool(re.search(r"\bdocker\b", path.read_text(encoding="utf-8", errors="ignore")))


def run_job(label0: str, job: dict, wf_env: dict, inputs: dict, repo: pathlib.Path, a, skipped: list) -> int:
    runs = "\n".join(str(st.get("run") or "") + " " + str(st.get("working-directory") or "") for st in job.get("steps") or [])
    if not a.list and USES_SIBLINGS.search(runs):
        with sibling_checkouts(repo, a.own) as (ok, reason):
            if not ok:
                skipped.append(f"{label0} (SKIP: needs the other repos checked out as _backend/_web/_infra: {reason})")
                return 0
            return run_job_steps(label0, job, wf_env, inputs, repo, a, skipped)
    return run_job_steps(label0, job, wf_env, inputs, repo, a, skipped)


def run_job_steps(label0: str, job: dict, wf_env: dict, inputs: dict, repo: pathlib.Path, a, skipped: list) -> int:
    if True:
        if True:
            job_env = {"RUNNER_TEMP": expand("${{ runner.temp }}", a.ctx), "GITHUB_WORKSPACE": str(repo),
                       **wf_env, **clean_env(job.get("env"), a.ctx)}
            # Folders a skipped `actions/checkout` would have filled (`with: path:`); a step that moves one is CI-only.
            checkout_paths = [str((st.get("with") or {}).get("path")) for st in job.get("steps") or []
                              if str(st.get("uses") or "").startswith("actions/checkout") and (st.get("with") or {}).get("path")]
            workdir = ((job.get("defaults") or {}).get("run") or {}).get("working-directory", ".")
            deps_missing = False
            docker_skipped = False
            aws_role = False
            for i, step in enumerate(job.get("steps") or []):
                if "configure-aws-credentials" in str(step.get("uses") or ""):
                    aws_role = True  # every later step of the job may call AWS through a script, not only by its own text
                cmd = step.get("run")
                if not cmd:
                    continue
                cmd = expand(substitute(cmd, inputs), a.ctx)
                label = f"{label0}: {step.get('name') or cmd.strip().splitlines()[0][:70]}"
                env = {**job_env, **{k: expand(substitute(str(v), inputs), a.ctx) for k, v in (step.get("env") or {}).items()
                                     if not EXPR.search(expand(substitute(str(v), inputs), a.ctx))}}
                if aws_role:
                    skipped.append(f"{label} (needs AWS: runs after the job assumes a role)")
                    continue
                if REMOTE.search(cmd) or any(k in (step.get("env") or {}) for k in ("GH_TOKEN", "GITHUB_TOKEN")):
                    skipped.append(f"{label} (needs GitHub or AWS)")
                    continue
                secret_keys = {k for k, v in {**(job.get("env") or {}), **(step.get("env") or {})}.items() if "secrets." in str(v)}
                if secret_keys == {"NODE_AUTH_TOKEN"} and re.search(r"\b(pnpm|npm|yarn) (install|ci)\b", cmd) \
                        and (pkg_token := packages_token()):
                    # The only secret is the package-registry read token: the laptop's gh login (read:packages) stands in,
                    # so the Node checks after it run instead of being skipped. The value is passed to the step, never printed.
                    env["NODE_AUTH_TOKEN"] = pkg_token
                elif secret_keys:
                    skipped.append(f"{label} (needs a repository secret)")
                    if re.search(r"\b(pnpm|npm|yarn) (install|ci)\b", cmd):
                        deps_missing = True
                    continue
                if deps_missing and not (repo / step.get("working-directory", workdir) / "node_modules").exists() \
                        and re.search(r"\b(pnpm|npm|npx|yarn|node|eslint|tsc|vitest|spectral|prettier)\b", cmd):
                    skipped.append(f"{label} (SKIP: Node dependencies are not installed; their Install step needs a package-registry token)")
                    continue
                if re.search(r"\bgitleaks (git|detect)\b", cmd):
                    skipped.append(f"{label} (history scan: CI scans the pull request's commits; the pre-commit hook scans each commit)")
                    continue
                if re.match(r"(Install|Set up|Download) ", str(step.get("name") or "")):
                    skipped.append(f"{label} (tool install; the laptop has its own)")
                    continue
                missing = [t for t in OPTIONAL_TOOLS if re.search(rf"(?m)^\s*{t}\b", cmd) and not shutil.which(t)]
                if missing:
                    skipped.append(f"{label} (SKIP: {missing[0]} is not installed on this laptop)")
                    continue
                if re.search(r"(?m)^\s*tflint\b", cmd) and (why := tflint_problem()):
                    skipped.append(f"{label} (SKIP: {why})")
                    continue
                if any(re.search(rf"\bmv\s+{re.escape(c)}\b", cmd) for c in checkout_paths if not c.startswith("${{")):
                    skipped.append(f"{label} (CI-only: places a checkout the laptop does not make)")
                    continue
                # A value from the other jobs' results (`needs`) only a CI run has; a script may read it without the step's
                # text naming it, so such a step is always skipped.
                from_needs = {str(k) for k, v in {**(job.get("env") or {}), **(step.get("env") or {})}.items()
                              if re.search(r"\$\{\{[^}]*\bneeds\b", str(v))}
                unknown = [k for k in sorted(unresolved_env(job.get("env"), step.get("env"), ctx=a.ctx))
                           if k in from_needs or re.search(r"\$\{?" + re.escape(k) + r"\b", cmd)]
                if EXPR.search(str(step.get("working-directory", workdir))):
                    unknown.append("its working directory")
                if unknown:
                    skipped.append(f"{label} (SKIP: needs {unknown[0]}, a value only a CI run has)")
                    continue
                if a.skip_docker and (re.search(r"\bdocker\b", cmd) or calls_docker_script(cmd, repo / step.get("working-directory", workdir))):
                    skipped.append(f"{label} (docker)")
                    docker_skipped = True
                    continue
                if docker_skipped and re.search(r"(127\.0\.0\.1|localhost):\d+", cmd + " " + " ".join(map(str, env.values()))):
                    skipped.append(f"{label} (SKIP: needs a service an earlier skipped docker step starts)")
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
                envfile = run_root() / "github_env"
                for line in envfile.read_text(encoding="utf-8").splitlines() if envfile.exists() else []:
                    if "=" in line:  # what `>> "$GITHUB_ENV"` gives the job's later steps
                        k, _, v = line.partition("=")
                        job_env[k] = v
                if envfile.exists():
                    envfile.write_text("", encoding="utf-8")
                print(f"{'PASS' if rc == 0 else 'FAIL'}  {label}  ({secs:.0f}s)")
                if rc:
                    print(out[-6000:])
                    return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
