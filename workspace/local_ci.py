#!/usr/bin/env python3
"""Run a repo's pull-request CI locally, from its own workflow files, before pushing.

    python3 .github/workspace/local_ci.py <repo dir> [--jobs layout,checks] [--skip-docker] [--list]

For every workflow that runs on `pull_request`, every job's `run:` steps run in order in the repo directory, with the
job's and step's `env` (github.* and runner.* values filled in for a pull request into dev here, other expressions left out), stopping at the first failure. `uses:` steps (checkout, setup-*) are
skipped: the laptop already has the tools; reusable-workflow jobs are listed as skipped, and the shared checks they
stand for run here directly (actionlint with shellcheck, the runner-label lint). A step that rewrites package.json (`npm pkg set`) has it restored afterwards. Steps that need GitHub's API or
AWS (they read GH_TOKEN, assume a role, or call `gh api`) are skipped and listed, never run.

Exit 0 when every step ran and passed; 1 at the first failure (its output is shown); 2 bad input.
"""
from __future__ import annotations

import argparse
import atexit
import contextlib
import hashlib
import os
import platform
import pathlib
import re
import shlex
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
OPTIONAL_TOOLS = ("trivy",)
# The versions CI pins (foundation-playbook §8a P3; scripts/test_local_ci.py keeps the shared workflows at these).
# A different uv on the laptop fails the run with the fix; tflint runs natively at CI's version, else in CI's Docker image, else it is skipped (WARN), or FAILS under --release.
UV_VERSION = "0.12.0"
CI_PYTHON = "3.14"  # the runner image's Python minor version; workspace/local-ci/Dockerfile's base must be python:<this>
TFLINT_VERSION = "0.65.0"
TFLINT_IMAGE = "ghcr.io/terraform-linters/tflint"
# One provider cache for every local run and worktree (§8a P6), unless the laptop sets TF_PLUGIN_CACHE_DIR itself.
SHARED_PLUGIN_CACHE = pathlib.Path.home() / ".cache" / "onepeek" / "terraform-plugins"
# Skip patterns are matched against the step with shell comments removed (§8c L1).
REMOTE = re.compile(r"\bgh api\b|\bgh pr\b|\baws \b|GH_TOKEN|GITHUB_TOKEN|configure-aws-credentials|terraform (plan|apply)|\bgit push\b|(?m:terraform\b[^\n]*\binit\b(?![^\n]*-backend=false))|\bgit clone\b[^\n]*github\.com")


def strip_comments(cmd: str) -> str:
    """The step's text with shell comments removed (§8c L1): a `#` starts a comment only at the start of a word (line
    start or after unquoted whitespace) and outside single and double quotes, and the comment runs to the end of the
    line. `url#fragment` is not a comment. A line whose quotes do not close is kept whole, so this never hides text."""
    out = []
    for line in cmd.splitlines():
        quote, i, cut = "", 0, None
        while i < len(line):
            ch = line[i]
            if quote:
                if ch == "\\" and quote in ('"', "$'"):   # a backslash escapes inside double quotes and ANSI-C quotes
                    i += 1
                elif ch == quote[-1]:
                    quote = ""
            elif ch == "\\":
                i += 1
            elif ch == "$" and line[i + 1:i + 2] == "'":   # $'...' (ANSI-C quoting), where \' is an escaped quote
                quote = "$'"
                i += 1
            elif ch in "'\"":
                quote = ch
            elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
                cut = i
                break
            i += 1
        out.append(line if quote else (line if cut is None else line[:cut]))
    return "\n".join(out)


TERRAFORM_TEST = re.compile(r"\bterraform\b[^\n;&|]*\btest\b")


# Script tests run in a Linux container on macOS (§8c L2).
LOCAL_CI_DIR = pathlib.Path(__file__).resolve().parent / "local-ci"
SCRIPT_TESTS = re.compile(r"(?:\bpython3?\s+-m\s+(?:unittest|pytest)\b|\bpytest\b)[^\n]*\bscripts\b|(?:^|[\s;&|(])(?:bash\s+\S+|\S+\.sh)(?![\w.-])", re.M)
NEEDS_NETWORK = re.compile(r"\b(pip3?|uv|npm|pnpm|yarn|npx|apt-get|curl|wget)\b|\bgit (clone|fetch|pull)\b|\bdocker pull\b")


def image_tag(dockerfile: pathlib.Path | None = None) -> str:
    """`onepeek-local-ci:<first 16 hex of the SHA-256 of the Dockerfile>`: a changed Dockerfile is a new tag."""
    f = dockerfile or LOCAL_CI_DIR / "Dockerfile"
    return "onepeek-local-ci:" + hashlib.sha256(f.read_bytes()).hexdigest()[:16]


def wants_linux(code: str) -> bool:
    """True when the step (comments removed) runs script tests: unittest or pytest over scripts/, or a shell script."""
    return bool(SCRIPT_TESTS.search(code))


def ensure_image(dockerfile: pathlib.Path | None = None) -> str:
    """Build the image when its tag is missing; returns the tag. RuntimeError when the build fails."""
    f = dockerfile or LOCAL_CI_DIR / "Dockerfile"
    tag = image_tag(f)
    if subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode:
        r = subprocess.run(["docker", "build", "-q", "-t", tag, "-f", str(f), str(f.parent)], capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError("cannot build the local CI image: " + (r.stdout + r.stderr)[-600:])
    return tag


def container_step(cmd: str, cwd: pathlib.Path, env: dict[str, str], repo: pathlib.Path, files: dict[str, str], network: bool,
                   tag: str) -> tuple[int, str, float]:
    """The step in the Linux container: the repo (and its git directory, for a worktree) and the run's temp folder at the
    same paths, the step's environment passed by name, `--network none` unless it reaches a registry, never
    Docker-in-Docker. Runs as the laptop user so files it writes stay yours."""
    t = time.time()
    names = sorted(k for k in {**env, **files} if k not in ("PATH", "HOME", "VIRTUAL_ENV", "TF_PLUGIN_CACHE_DIR") and not k.startswith("AWS_"))
    mounts = {str(repo.resolve()), str(run_root())}
    common = subprocess.run(["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"], capture_output=True, text=True)
    if common.returncode == 0 and common.stdout.strip():
        mounts.add(common.stdout.strip())
    argv = ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp", "-e", "CI=true",
            *([] if network else ["--network", "none"])]
    for m in sorted(mounts):
        argv += ["-v", f"{m}:{m}"]
    for k in names:
        argv += ["-e", k]
    argv += ["-w", str(cwd), tag, "bash", "-eo", "pipefail", "-c", cmd]
    r = subprocess.run(argv, env={**os.environ, **env, **files}, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr, time.time() - t


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


# Tracked files that deliberately hold conflict markers (none today); repo-relative paths.
CONFLICT_MARKER_ALLOW: tuple[str, ...] = ()
_MARKER = re.compile(r"^(?:<{7}|>{7})(?:[ \t\r]|$)", re.M)


def conflict_markers(repo: pathlib.Path) -> list[str]:
    """`path:line` of each unresolved merge conflict marker (`<<<<<<< ` or `>>>>>>> ` at a line start) in tracked text files.

    A bare `=======` is a Markdown heading underline, so it counts only as the middle of a conflict, never alone."""
    r = subprocess.run(["git", "-C", str(repo), "ls-files", "-z", "--deduplicate"], capture_output=True)
    if r.returncode:  # a git without --deduplicate (before 2.31): list every index stage and deduplicate here
        r = subprocess.run(["git", "-C", str(repo), "ls-files", "-z"], capture_output=True)
    found = []
    for rel in dict.fromkeys(filter(None, r.stdout.decode("utf-8", "replace").split("\0"))):
        f = repo / rel
        if rel in CONFLICT_MARKER_ALLOW or not f.is_file() or f.is_symlink():
            continue
        data = f.read_bytes()
        if b"\0" in data:  # binary
            continue
        text = data.decode("utf-8", "replace")
        m = _MARKER.search(text)
        if m:
            found.append(f"{rel}:{text.count(chr(10), 0, m.start()) + 1}")
    return found


_DOCKER: bool | None = None  # `docker info` answers once per run


def docker_ready() -> bool:
    """True when the docker CLI is installed and its daemon answers (asked once per run)."""
    global _DOCKER
    if _DOCKER is None:
        _DOCKER = bool(shutil.which("docker")) and subprocess.run(["docker", "info"], capture_output=True, text=True).returncode == 0
    return _DOCKER


# A tflint command anywhere in a step: at a line start or after `;`, `&&`, `||`, `|`, `(`, `if`, `then`, `do`; not `tflint-foo`.
TFLINT_CALL = re.compile(r"(?:^|[;&|(]|\b(?:if|then|do|while)\b)\s*tflint(?![\w.-])", re.M)


def tflint_mode(allow_docker: bool = True) -> str:
    """How tflint runs here: "native" (CI's version on PATH), "docker" (CI's image; Homebrew no longer ships tflint), or ""."""
    if tool_version("tflint") == TFLINT_VERSION:
        return "native"
    return "docker" if allow_docker and docker_ready() else ""


def tflint_problem(allow_docker: bool = True) -> str:
    """Why tflint steps cannot run here (neither CI's tflint on PATH nor Docker), or ""."""
    if tflint_mode(allow_docker):
        return ""
    v = tool_version("tflint")
    have = "tflint is not installed" if v is None else f"tflint {v} is installed; CI pins {TFLINT_VERSION}"
    return f"{have} on this laptop and Docker is {'not used (--skip-docker)' if not allow_docker else 'not available'} (CI runs {TFLINT_IMAGE}:v{TFLINT_VERSION})"


TFLINT_CACHE = pathlib.Path.home() / ".cache" / "onepeek" / "tflint.d"  # the plugins `tflint --init` installs, kept between runs


def tflint_commands(cmd: str, repo: pathlib.Path, cwd: pathlib.Path) -> list[tuple[pathlib.Path, list[str]]] | None:
    """The (directory, tflint arguments) of each command when the step is only tflint commands (each optionally after
    `cd <dir> &&` or a `cd <dir>` line), else None. ValueError when a directory is outside the repository."""
    out, here = [], cwd.resolve()
    here.relative_to(repo.resolve())
    for line in cmd.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for seg in re.split(r"\s*&&\s*", line):
            words = shlex.split(seg)
            if len(words) == 2 and words[0] == "cd":
                here = (here / words[1]).resolve()
                here.relative_to(repo.resolve())
            elif words and words[0] == "tflint":
                out.append((here, words[1:]))
            else:
                return None
    return out


def tflint_via_docker(commands: list[tuple[pathlib.Path, list[str]]], repo: pathlib.Path, env: dict[str, str]) -> str:
    """Each tflint command as its own `docker run` of CI's pinned image (its entrypoint is tflint; no shell), the repo at /data
    and one plugin cache at /root/.tflint.d so `--init` holds for `--recursive`. Only GITHUB_TOKEN and TFLINT_* are passed,
    by name, never by value."""
    TFLINT_CACHE.mkdir(parents=True, exist_ok=True)
    flags = " ".join(f"-e {shlex.quote(k)}" for k in sorted(env) if k == "GITHUB_TOKEN" or k.startswith("TFLINT_"))
    lines = []
    for here, args in commands:
        rel = here.resolve().relative_to(repo.resolve())
        workdir = "/data" if str(rel) == "." else f"/data/{rel}"
        lines.append(f"docker run --rm -v {shlex.quote(str(repo.resolve()))}:/data -w {shlex.quote(workdir)} "
                     f"-v {shlex.quote(str(TFLINT_CACHE))}:/root/.tflint.d {flags} {TFLINT_IMAGE}:v{TFLINT_VERSION} {shlex.join(args)}")
    return "\n".join(lines)


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


def run_step(cmd: str, cwd: pathlib.Path, env: dict[str, str], shell: str, linux: tuple | None = None) -> tuple[int, str, float]:
    """linux: (repo, image tag, network) runs the step in the Linux container instead (§8c L2)."""
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
    if linux:
        return container_step(cmd, cwd, env, linux[0], files, linux[2], linux[1])
    r = subprocess.run(argv, cwd=cwd, env={**base, "CI": "true", **files, **env}, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr), time.time() - t


SHARED_PIN = re.compile(r"OnePeek/\.github/\.github/workflows/[^@\s]+@([0-9A-Za-z._/-]+)")
_SHARED: dict[str, pathlib.Path] = {}


def shared_worktree(pin: str) -> pathlib.Path:
    """The shared `.github` at the caller's pin (§8c L4): a detached, temporary worktree of `<workspace>/.github`,
    fetched when the commit is missing, removed at exit (also after SIGTERM, which exits through atexit).
    RuntimeError when the pin cannot be found."""
    if pin in _SHARED:
        return _SHARED[pin]
    src = WORKSPACE / ".github"
    if not (src / ".git").exists():
        raise RuntimeError(f"no shared checkout at {src} to read the pin {pin[:12]} from")
    have = lambda: git("rev-parse", "--verify", "--quiet", f"{pin}^{{commit}}", cwd=src).returncode == 0  # noqa: E731
    if not have():
        git("fetch", "--quiet", "origin", pin, cwd=src)
    if not have():
        git("fetch", "--quiet", "origin", cwd=src)
    if not have():
        raise RuntimeError(f"the shared workflows' pin {pin[:12]} is not in {src} and could not be fetched from origin")
    target = run_root() / f"shared-{re.sub(r'[^A-Za-z0-9]', '_', pin)[:40]}"
    r = git("worktree", "add", "--detach", "--quiet", str(target), pin, cwd=src)
    if r.returncode:
        raise RuntimeError(f"cannot check out the pin {pin[:12]}: {r.stderr.strip()[-200:]}")

    _SHARED[pin] = target
    return target


def cleanup_shared() -> None:
    """Remove the pin worktrees this run made (at exit, and after SIGTERM, which exits through atexit)."""
    src = WORKSPACE / ".github"
    for pin, target in list(_SHARED.items()):
        if src.is_dir():
            git("worktree", "remove", "--force", str(target), cwd=src)
        shutil.rmtree(target, ignore_errors=True)
        if src.is_dir():
            git("worktree", "prune", cwd=src)
        del _SHARED[pin]


atexit.register(cleanup_shared)  # registered after run_root's cleanup would be, so it runs first


def shared_root(pin: str, shared: str | None) -> pathlib.Path:
    """Where this run reads the shared workflows and lint from: --shared, else the caller's pin."""
    if shared:
        return pathlib.Path(shared).resolve()
    return shared_worktree(pin) if pin else WORKSPACE / ".github"


def shared_notice(shared: str) -> str:
    path = pathlib.Path(shared).resolve()
    branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=path).stdout.strip() or "?"
    sha = git("rev-parse", "--short=7", "HEAD", cwd=path).stdout.strip() or "?"
    return f"shared workflows from {path} ({branch} {sha}), not the pin"


def shared_checks(repo: pathlib.Path, kind: str, name: str, shared: pathlib.Path | None = None) -> list[tuple[str, list[str]]]:
    checks = []
    if shutil.which("actionlint"):
        checks.append(("actionlint (with shellcheck)", ["actionlint"]))
    # OnePeek/.github lints itself with its own (changed) lint, as its workflow-lint job does; every other repo with the
    # shared checkout's.
    lint = (repo if name == ".github" else shared or WORKSPACE / ".github") / "scripts/check_runner_labels.py"
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
    ap.add_argument("--release", action="store_true", help="a release run: a step that needs an optional tool the laptop lacks (trivy; tflint without Docker) fails instead of being skipped, even with --skip-docker")
    ap.add_argument("--list", action="store_true", help="print the steps, run nothing")
    ap.add_argument("--native", action="store_true", help="on macOS, run script tests on the laptop, not in the Linux container (§8c L2)")
    ap.add_argument("--shared", metavar="PATH", default=os.environ.get("ONEPEEK_SHARED_GITHUB") or None,
                    help="read the shared workflows from this .github worktree, not from the caller's pins (§8c L4)")
    a = ap.parse_args(argv)
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))  # so a stopped run still removes its checkouts and temp files
    repo = pathlib.Path(a.repo).resolve()
    print(f"local CI on {repo}", flush=True)
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
    global _DOCKER
    _DOCKER = None
    if not a.list and (problem := uv_problem()):
        print(f"FAIL  {problem}")
        return 1

    if not a.list and (marks := conflict_markers(repo)):
        print("FAIL  unresolved merge conflict markers (resolve them first): " + ", ".join(marks[:20]))
        return 1

    if a.shared:
        if not pathlib.Path(a.shared).is_dir():
            print(f"FAIL  --shared {a.shared} is not a directory (check --shared and ONEPEEK_SHARED_GITHUB)")
            return 1
        print("NOTE  " + shared_notice(a.shared), flush=True)
    lint_root = None
    if name != ".github":
        pins = [m.group(1) for p_ in sorted((repo / ".github/workflows").glob("*.yml")) for m in SHARED_PIN.finditer(p_.read_text(encoding="utf-8"))]
        try:
            if a.shared or pins:
                lint_root = shared_root(max(set(pins), key=pins.count) if pins else "", a.shared)
        except RuntimeError as e:
            print(f"FAIL  {e}")
            return 1
    for check, cmd in shared_checks(repo, kind, name, lint_root):
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
                ref, _, pin = job["uses"].partition("@")
                pin = pin.split()[0] if pin.split() else ""
                local = None
                if ref.startswith("OnePeek/.github/"):
                    try:
                        local = shared_root(pin, a.shared) / ref.split("OnePeek/.github/", 1)[-1]
                    except RuntimeError as e:
                        print(f"FAIL  {path.name}:{job_id}: {e}")
                        return 1
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
            dead_tags: set[str] = set()  # image tags a skipped or untranslatable build would have made (§8c L3)
            for i, step in enumerate(job.get("steps") or []):
                if "configure-aws-credentials" in str(step.get("uses") or ""):
                    aws_role = True  # every later step of the job may call AWS through a script, not only by its own text
                if str(step.get("uses") or "").startswith("docker/build-push-action@"):
                    if rc := run_build(label0, step, inputs, repo, workdir, a, skipped, dead_tags):
                        return rc
                    continue
                cmd = step.get("run")
                if not cmd:
                    continue
                cmd = expand(substitute(cmd, inputs), a.ctx)
                code = strip_comments(cmd)  # skip patterns see the code, not its comments (§8c L1)
                label = f"{label0}: {step.get('name') or cmd.strip().splitlines()[0][:70]}"
                tf_test = bool(TERRAFORM_TEST.search(code))

                def skip(reason: str) -> bool:
                    """Records the skip; True when the run must fail (a skipped `terraform test` under --release)."""
                    if tf_test:
                        print(f"{'FAIL' if a.release and not a.list else 'WARN'}  {label}: terraform test skipped: {reason}",
                              file=sys.stderr if not a.release else sys.stdout)
                        if a.release and not a.list:
                            return True
                        skipped.insert(0, f"terraform test skipped: {label} ({reason})")
                    else:
                        skipped.append(f"{label} ({reason})")
                    return False
                env = {**job_env, **{k: expand(substitute(str(v), inputs), a.ctx) for k, v in (step.get("env") or {}).items()
                                     if not EXPR.search(expand(substitute(str(v), inputs), a.ctx))}}
                if aws_role:
                    if skip("needs AWS: runs after the job assumes a role"):
                        return 1
                    continue
                if REMOTE.search(code) or any(k in (step.get("env") or {}) for k in ("GH_TOKEN", "GITHUB_TOKEN")):
                    if skip("needs GitHub or AWS"):
                        return 1
                    continue
                secret_keys = {k for k, v in {**(job.get("env") or {}), **(step.get("env") or {})}.items() if "secrets." in str(v)}
                if dead := sorted(t for t in dead_tags if t in code):
                    if skip(f"SKIP: needs the image a skipped build makes ({dead[0]})"):
                        return 1
                    continue
                if secret_keys == {"NODE_AUTH_TOKEN"} and re.search(r"\b(pnpm|npm|yarn) (install|ci)\b", code) \
                        and (pkg_token := packages_token()):
                    # The only secret is the package-registry read token: the laptop's gh login (read:packages) stands in,
                    # so the Node checks after it run instead of being skipped. The value is passed to the step, never printed.
                    env["NODE_AUTH_TOKEN"] = pkg_token
                elif secret_keys:
                    if skip(f"needs a repository secret"):
                        return 1
                    if re.search(r"\b(pnpm|npm|yarn) (install|ci)\b", code):
                        deps_missing = True
                    continue
                if deps_missing and not (repo / step.get("working-directory", workdir) / "node_modules").exists() \
                        and re.search(r"\b(pnpm|npm|npx|yarn|node|eslint|tsc|vitest|spectral|prettier)\b", cmd):
                    if skip(f"SKIP: Node dependencies are not installed; their Install step needs a package-registry token"):
                        return 1
                    continue
                if re.search(r"\bgitleaks (git|detect)\b", code):
                    if skip(f"history scan: CI scans the pull request's commits; the pre-commit hook scans each commit"):
                        return 1
                    continue
                if re.match(r"(Install|Set up|Download) ", str(step.get("name") or "")):
                    if skip(f"tool install; the laptop has its own"):
                        return 1
                    continue
                missing = [t for t in OPTIONAL_TOOLS if re.search(rf"(?m)^\s*{t}\b", code) and not shutil.which(t)]
                if missing and a.release and not a.list:
                    print(f"FAIL  {label}: {missing[0]} is not installed on this laptop (--release does not skip it)")
                    return 1
                if missing:
                    if skip(f"SKIP: {missing[0]} is not installed on this laptop"):
                        return 1
                    continue
                tflint_docker = False
                if TFLINT_CALL.search(cmd):
                    try:
                        pure = tflint_commands(cmd, repo, repo / step.get("working-directory", workdir))
                    except ValueError:
                        print(f"FAIL  {label}: a directory is outside the repository")
                        return 1
                    if why := tflint_problem(not a.skip_docker):
                        if a.release and not a.list:
                            print(f"FAIL  {label}: {why}")
                            return 1
                        print(f"WARN  {label}: {why}; CI runs it", file=sys.stderr)
                        if skip(f"SKIP: {why}"):
                            return 1
                        continue
                    tflint_docker = tflint_mode(not a.skip_docker) == "docker"
                    if tflint_docker and pure is None:
                        print(f"FAIL  {label}: this step runs other commands besides tflint, so it needs tflint {TFLINT_VERSION} on PATH (Docker runs only plain tflint commands)")
                        return 1
                if any(re.search(rf"\bmv\s+{re.escape(c)}\b", code) for c in checkout_paths if not c.startswith("${{")):
                    if skip(f"CI-only: places a checkout the laptop does not make"):
                        return 1
                    continue
                # A value from the other jobs' results (`needs`) only a CI run has; a script may read it without the step's
                # text naming it, so such a step is always skipped.
                from_needs = {str(k) for k, v in {**(job.get("env") or {}), **(step.get("env") or {})}.items()
                              if re.search(r"\$\{\{[^}]*\bneeds\b", str(v))}
                unknown = [k for k in sorted(unresolved_env(job.get("env"), step.get("env"), ctx=a.ctx))
                           if k in from_needs or re.search(r"\$\{?" + re.escape(k) + r"\b", code)]
                if EXPR.search(str(step.get("working-directory", workdir))):
                    unknown.append("its working directory")
                if unknown:
                    if skip(f"SKIP: needs {unknown[0]}, a value only a CI run has"):
                        return 1
                    continue
                if a.skip_docker and (re.search(r"\bdocker\b", code) or calls_docker_script(cmd, repo / step.get("working-directory", workdir))):
                    if skip(f"docker"):
                        return 1
                    docker_skipped = True
                    continue
                if docker_skipped and re.search(r"(127\.0\.0\.1|localhost):\d+", code + " " + " ".join(map(str, env.values()))):
                    if skip(f"SKIP: needs a service an earlier skipped docker step starts"):
                        return 1
                    continue
                if a.list:
                    print(label)
                    continue
                cwd = repo / step.get("working-directory", workdir)
                cmd = EXPR.sub("", cmd)
                # A tool a skipped install step would have put in the working directory: use the laptop's.
                cmd = re.sub(r"(?m)(^|\s)\./([A-Za-z0-9_-]+)\b",
                             lambda m: m.group(0) if (cwd / m.group(2)).exists() or not shutil.which(m.group(2)) else m.group(1) + m.group(2), cmd)
                if tflint_docker:
                    cmd = tflint_via_docker(tflint_commands(cmd, repo, cwd) or [], repo, env)
                # `npm pkg set` (the api-client's "Version as next snapshot") rewrites package.json in the working tree: put it back.
                pkg = cwd / "package.json"
                keep = pkg.read_bytes() if re.search(r"\bnpm pkg (set|delete|fix)\b", cmd) and pkg.is_file() else None
                linux = None
                if platform.system() == "Darwin" and not a.native and wants_linux(code):
                    if a.skip_docker or not docker_ready():
                        why = "Docker is not used (--skip-docker)" if a.skip_docker else "Docker is not available"
                        if a.release:
                            print(f"FAIL  {label}: script tests run in a Linux container on macOS and {why} (--native runs them here)")
                            return 1
                        print(f"WARN  {label}: script tests run here on the laptop, not in CI's Linux: {why}", file=sys.stderr)
                    else:
                        try:
                            linux = (repo, ensure_image(), bool(NEEDS_NETWORK.search(code)))
                        except RuntimeError as e:
                            print(f"FAIL  {label}: {e}")
                            return 1
                try:
                    rc, out, secs = run_step(cmd, cwd, env, str(step.get("shell") or job.get("shell") or ""), linux)
                finally:
                    if keep is not None:
                        pkg.write_bytes(keep)
                envfile = run_root() / "github_env"
                for line in envfile.read_text(encoding="utf-8").splitlines() if envfile.exists() else []:
                    if "=" in line:  # what `>> "$GITHUB_ENV"` gives the job's later steps
                        k, _, v = line.partition("=")
                        job_env[k] = v
                if envfile.exists():
                    envfile.write_text("", encoding="utf-8")
                print(f"{'PASS' if rc == 0 else 'FAIL'}  {label}  ({secs:.0f}s){'  [linux container]' if linux else ''}")
                if rc:
                    print(out[-6000:])
                    return 1
    return 0


BUILD_INPUTS = {"context", "file", "platforms", "tags", "build-args", "secrets", "load", "push", "target", "cache-from", "cache-to"}


def build_command(step: dict, inputs: dict, ctx: dict, repo: pathlib.Path, workdir: str) -> tuple[list[str], dict[str, str], list[str]]:
    """`docker buildx build --load` for a docker/build-push-action step with `load: true` and `push` not true (§8c L3).
    Returns (argv, extra environment, tags). ValueError says why it cannot be translated: an unknown input, a push, an
    expression the run does not know, a secret the laptop cannot supply."""
    w = {k: expand(substitute(str(v), inputs), ctx) for k, v in (step.get("with") or {}).items()}
    if unknown := sorted(set(w) - BUILD_INPUTS):
        raise ValueError(f"unknown input {unknown[0]}")
    if w.get("push", "false").strip().lower() == "true" or w.get("load", "false").strip().lower() != "true":
        raise ValueError("it is not a load-only build (push or no load)")
    for k, v in w.items():
        if k not in ("cache-from", "cache-to") and EXPR.search(v) and not (k == "secrets" and "secrets." in v):
            raise ValueError(f"{k} holds an expression the laptop cannot fill")
    tags = [t.strip() for t in re.split(r"[\n,]", w.get("tags", "")) if t.strip()]
    if not tags:
        raise ValueError("no tags")
    argv = ["docker", "buildx", "build", "--load"]
    for t in tags:
        argv += ["-t", t]
    if w.get("file"):
        argv += ["-f", w["file"]]
    if w.get("platforms"):
        argv += ["--platform", w["platforms"]]
    if w.get("target"):
        argv += ["--target", w["target"]]
    for line in filter(None, (x.strip() for x in w.get("build-args", "").splitlines())):
        argv += ["--build-arg", line]
    env: dict[str, str] = {}
    for line in filter(None, (x.strip() for x in str((step.get("with") or {}).get("secrets", "")).splitlines())):
        sid, _, val = line.partition("=")
        if "secrets." in val:
            token = packages_token() if sid == "npm_token" else ""
            if not token:
                raise ValueError(f"secret {sid} needs a package-registry token (the laptop's gh login with read:packages)")
            val = token
        elif EXPR.search(expand(substitute(val, inputs), ctx)):
            raise ValueError(f"secret {sid} holds an expression the laptop cannot fill")
        else:
            val = expand(substitute(val, inputs), ctx)
        env_name = "LOCAL_CI_SECRET_" + re.sub(r"[^A-Za-z0-9]", "_", sid).upper()
        env[env_name] = val
        argv += ["--secret", f"id={sid},env={env_name}"]
    argv.append(w.get("context") or ".")
    return argv, env, tags


def run_build(label0: str, step: dict, inputs: dict, repo: pathlib.Path, workdir: str, a, skipped: list, dead_tags: set) -> int:
    """A docker/build-push-action step (§8c L3): built for real, or its tags are removed and the steps that use them skipped.
    No step ever runs on an image this run did not build."""
    label = f"{label0}: {step.get('name') or 'docker/build-push-action'}"
    raw = expand(substitute(str((step.get("with") or {}).get("tags", "")), inputs), a.ctx)
    tags = [t.strip() for t in re.split(r"[\n,]", raw) if t.strip() and not EXPR.search(t)]
    try:
        if a.skip_docker:
            raise ValueError("--skip-docker")
        argv, extra, tags = build_command(step, inputs, a.ctx, repo, ".")
    except ValueError as e:
        for t in tags:
            if not a.list and docker_ready():
                subprocess.run(["docker", "image", "rm", "-f", t], capture_output=True)
        dead_tags.update(tags)
        skipped.append(f"{label} (SKIP: build not run here: {e}; later steps that use its image are skipped)")
        return 0
    if a.list:
        print(label)
        return 0
    if not docker_ready():
        dead_tags.update(tags)
        skipped.append(f"{label} (SKIP: Docker is not available)")
        return 0
    for t in tags:  # a tag from an earlier run never stands in for this build
        subprocess.run(["docker", "image", "rm", "-f", t], capture_output=True)
    t0 = time.time()
    base = {k: v for k, v in os.environ.items() if k != "ONEPEEK_WORKSPACE" and not k.startswith("AWS_")}
    r = subprocess.run(argv, cwd=repo, env={**base, **extra}, capture_output=True, text=True)
    print(f"{'PASS' if r.returncode == 0 else 'FAIL'}  {label}  ({time.time() - t0:.0f}s)")
    if r.returncode:
        print((r.stdout + r.stderr)[-6000:])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
