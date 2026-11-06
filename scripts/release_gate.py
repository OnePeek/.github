#!/usr/bin/env python3
"""The release gate (onepeek-docs architecture/lld/infra/delivery-pipelines.md §8.1, decision 168 point 2).

A `dev` -> `release` pull request (a promotion) skips the tree-only checks. This gate makes that safe: it passes only
when the tree the promotion's merge commit will hold equals `dev`'s head tree, and a first-parent `dev` commit with
that tree has a green certification (every certifying workflow's newest `push` or `workflow_dispatch` run on `dev`
succeeded, and for `checks.yml` its `certified` job too). Everything it cannot read refuses (fail closed).

    python3 release_gate.py --repo OnePeek/<repo> --pr <n> --workflows "checks.yml image.yml" --git-dir <empty dir>

Exit 0 on pass, 1 on refusal or any error. GitHub is read through `gh api` (GH_TOKEN); git fetches only `release` and
the head commit from https://github.com/<repo>.git into --git-dir (release-gate.yml gives git the token through the
`!gh auth git-credential` helper). `ship_change_set.py` calls decide() at merge time. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from typing import Callable

CHECKS = "checks.yml"            # the workflow whose `certified` job must also succeed (§8.1 step 4)
CERTIFIED_JOB = "certified"      # matched exactly on the job name (build ruling G3)
EVENTS = ("push", "workflow_dispatch")  # the certification events (§8.1 step 4, edge cases)
MAX_COMMITS = 20                 # first-parent commits of dev walked from the head (§8.1 step 4)
POLL_SECONDS = 30                # §8.1 step 5
WAIT_SECONDS = 25 * 60           # §8.1 step 5
PER_PAGE = 100                   # the API's page size maximum

REPO_RE = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", re.ASCII)
WORKFLOW_RE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*\.ya?ml", re.ASCII)
SHA_RE = re.compile(r"[0-9a-f]{40}", re.ASCII)


class GateError(Exception):
    """A read that failed or returned something unexpected: the gate refuses."""


class Decision:
    """The verdict (a plain class, so a caller that loads this file by path needs no sys.modules entry)."""

    def __init__(self, ok: bool, message: str, summary: list[str] | None = None):
        self.ok, self.message, self.summary = ok, message, list(summary or [])

    def __repr__(self) -> str:
        return f"Decision(ok={self.ok!r}, message={self.message!r})"


def _clean_env() -> dict[str, str]:
    """The environment without Git's repository-local variables, so an inherited GIT_DIR never redirects git."""
    env = dict(os.environ)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                 "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_PREFIX", "GIT_CEILING_DIRECTORIES"):
        env.pop(name, None)
    return env


# ---- GitHub ----------------------------------------------------------------------------------------------------------

def gh_get(path: str) -> object:
    """GET one API path through `gh api`; any failure (exit code, unparsable body) raises GateError."""
    try:
        proc = subprocess.run(["gh", "api", "-X", "GET", path], stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=60, env=_clean_env())
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GateError(f"gh api {path}: {e}") from e
    if proc.returncode != 0:
        raise GateError(f"gh api {path} failed (exit {proc.returncode}): {proc.stderr.strip()[:300]}")
    try:
        return json.loads(proc.stdout)
    except ValueError as e:
        raise GateError(f"gh api {path}: unparsable response") from e


def gh_list(path: str, key: str) -> list[dict]:
    """Every item of a paginated list response (`{key: [...]}`), page by page until a short page."""
    items: list[dict] = []
    page = 1
    while True:
        body = gh_get(f"{path}&per_page={PER_PAGE}&page={page}")
        if not isinstance(body, dict) or not isinstance(body.get(key), list):
            raise GateError(f"gh api {path}: no `{key}` list in the response")
        chunk = body[key]
        if not all(isinstance(i, dict) for i in chunk):
            raise GateError(f"gh api {path}: malformed `{key}` item")
        items.extend(chunk)
        if len(chunk) < PER_PAGE:
            return items
        page += 1


def newest(runs: list[dict]) -> dict:
    """The newest run: highest created_at, then highest id on a tie (build ruling G5)."""
    try:
        return max(runs, key=lambda r: (str(r["created_at"]), int(r["id"])))
    except (KeyError, TypeError, ValueError) as e:
        raise GateError(f"a workflow run without a usable created_at or id: {e}") from e


def workflow_state(repo: str, workflow: str, sha: str) -> tuple[str, str, dict | None]:
    """(green | pending | red, a short reason, the newest run or None) for one certifying workflow at one commit."""
    runs = gh_list(f"repos/{repo}/actions/workflows/{workflow}/runs?head_sha={sha}&branch=dev", "workflow_runs")
    runs = [r for r in runs if r.get("event") in EVENTS and r.get("head_branch") == "dev" and r.get("head_sha") == sha]
    if not runs:
        return "red", f"{workflow}: no push or workflow_dispatch run on dev", None
    run = newest(runs)
    link = str(run.get("html_url") or f"run {run['id']}")
    if run.get("status") != "completed":
        return "pending", f"{link} {run.get('status')}", run
    if run.get("conclusion") != "success":
        return "red", f"{link} {run.get('conclusion')}", run
    if workflow == CHECKS:
        jobs = gh_list(f"repos/{repo}/actions/runs/{int(run['id'])}/jobs?filter=latest", "jobs")
        cert = [j for j in jobs if j.get("name") == CERTIFIED_JOB]
        if len(cert) != 1:
            return "red", f"{link} has {len(cert)} jobs named `{CERTIFIED_JOB}` (exactly one must succeed)", run
        if cert[0].get("conclusion") != "success":
            return "red", f"{link} `{CERTIFIED_JOB}` {cert[0].get('conclusion')}", run
    return "green", f"{link} success", run


def commit_state(repo: str, workflows: list[str], sha: str) -> tuple[str, str, list[dict]]:
    """A commit is green when every certifying workflow is green; one red workflow makes it red at once, even while
    another still runs (build ruling G4b); otherwise pending."""
    runs: list[dict] = []
    pending = ""
    for wf in workflows:
        state, reason, run = workflow_state(repo, wf, sha)
        if state == "red":
            return "red", reason, runs
        if state == "pending" and not pending:
            pending = reason
        if run is not None:
            runs.append(run)
    return ("pending", pending, runs) if pending else ("green", "", runs)


# ---- Git -------------------------------------------------------------------------------------------------------------

def git(git_dir: str, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", git_dir, *args], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=600, env=_clean_env())
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GateError(f"git {args[0]}: {e}") from e


def git_ok(git_dir: str, *args: str) -> str:
    proc = git(git_dir, *args)
    if proc.returncode != 0:
        raise GateError(f"git {' '.join(args[:2])} failed (exit {proc.returncode}): {proc.stderr.strip()[:300]}")
    return proc.stdout


def fetch(git_dir: str, remote: str, head: str) -> None:
    """Only `release` and the head commit, with their history, into a fresh repository (build ruling: two refs)."""
    os.makedirs(git_dir, exist_ok=True)
    git_ok(git_dir, "init", "-q")
    git_ok(git_dir, "fetch", "--no-tags", "--quiet", "--", remote, "+refs/heads/release:refs/remotes/origin/release")
    git_ok(git_dir, "fetch", "--no-tags", "--quiet", "--", remote, head)
    git_ok(git_dir, "rev-parse", "--verify", "--quiet", f"{head}^{{commit}}")


# ---- The gate --------------------------------------------------------------------------------------------------------

def _refuse(message: str, summary: list[str] | None = None) -> Decision:
    return Decision(False, message, summary)


def decide(repo: str, number: int, workflows: list[str], git_dir: str, *, remote: str | None = None,
           clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> Decision:
    """The gate's verdict for pull request <number> of <repo> (§8.1 steps 1 to 6)."""
    if not REPO_RE.fullmatch(repo or ""):
        return _refuse(f"not a repository name: {repo!r}")
    if not workflows or not all(WORKFLOW_RE.fullmatch(w) for w in workflows):
        return _refuse(f"the certifying workflows must be workflow file names: {workflows!r}")

    # 1. Shape.
    pr = gh_get(f"repos/{repo}/pulls/{int(number)}")
    try:
        base, head_ref = pr["base"]["ref"], pr["head"]["ref"]
        head_repo = (pr["head"].get("repo") or {}).get("full_name") or ""
        head = pr["head"]["sha"]
    except (KeyError, TypeError, AttributeError) as e:
        raise GateError(f"pull request #{number}: malformed response") from e
    if base != "release":
        return _refuse(f"#{number} targets {base!r}, not release: the release gate judges only pull requests into release")
    if str(head_repo).casefold() != repo.casefold():
        return _refuse(f"#{number} comes from another repository ({head_repo or 'deleted'}): only this repo's dev "
                       "or hotfix/* may target release")
    if head_ref != "dev":
        if isinstance(head_ref, str) and head_ref.startswith("hotfix/"):
            return Decision(True, "not a promotion: every check runs")
        return _refuse(f"#{number} has head {head_ref!r}: only dev and hotfix/* may target release "
                       "(branching-and-delivery.md §1)")
    if not isinstance(head, str) or not SHA_RE.fullmatch(head):
        raise GateError(f"pull request #{number}: head sha {head!r} is not a commit SHA")

    # 2. What ships.
    fetch(git_dir, remote or f"https://github.com/{repo}.git", head)
    merged = git(git_dir, "merge-tree", "--write-tree", "refs/remotes/origin/release", head)
    if merged.returncode == 1:
        return _refuse("the promotion conflicts with `release`")
    if merged.returncode != 0:
        raise GateError(f"git merge-tree failed (exit {merged.returncode}): {merged.stderr.strip()[:300]}")
    tree = merged.stdout.split("\n", 1)[0].strip()
    head_tree = git_ok(git_dir, "rev-parse", f"{head}^{{tree}}").strip()
    summary = [f"- head H: `{head}`", f"- merge tree T: `{tree}`"]

    # 3. Tree equals dev.
    if tree != head_tree:
        log = git_ok(git_dir, "log", "--no-merges", "--format=%h %s", f"{head}..refs/remotes/origin/release").strip()
        return _refuse("release holds changes dev lacks (a hotfix not merged back):\n"
                       f"{log or '(only merge commits)'}\n"
                       "merge `release` back into `dev` first (playbook §5.6)", summary)

    # 4. The first-parent dev commits with that tree, newest first.
    walk = git_ok(git_dir, "log", "--first-parent", f"--max-count={MAX_COMMITS}", "--format=%H %T", head)
    candidates = [c for c, t in (line.split() for line in walk.splitlines() if line.strip()) if t == tree]
    if not candidates or candidates[0] != head:
        raise GateError("the head is not the newest commit of its own first-parent walk")

    # 5. Waiting: pass on the newest green; wait only while none is green and one still runs (build ruling G4a).
    deadline = clock() + WAIT_SECONDS
    while True:
        running = ""
        head_reason = ""
        for sha in candidates:
            state, reason, runs = commit_state(repo, workflows, sha)
            if sha == head:
                head_reason = reason
            if state == "green":
                # 6. Summary.
                summary.append(f"- certified commit: `{sha}`")
                summary += [f"- run: {r.get('html_url') or r.get('id')} ({r.get('event')}, {r.get('conclusion')})"
                            for r in runs]
                return Decision(True, f"tree {tree[:7]} of dev {head[:7]} is certified by {sha[:7]}", summary)
            if state == "pending" and not running:
                running = sha
        if not running:
            return _refuse(f"dev {head[:7]} has no green certification ({head_reason}): fix dev or re-run its checks",
                           summary)
        remaining = deadline - clock()
        if remaining <= 0:
            return _refuse(f"certification of {running[:7]} still running; re-run", summary)
        sleep(min(POLL_SECONDS, remaining))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", required=True)
    ap.add_argument("--workflows", required=True, help="the certifying workflow files, separated by spaces")
    ap.add_argument("--git-dir", required=True, help="an empty directory for the two-ref fetch")
    args = ap.parse_args(argv)
    try:
        if not re.fullmatch(r"[1-9][0-9]*", args.pr, re.ASCII):
            decision = _refuse(f"not a pull request number: {args.pr!r} (the gate runs only on pull_request events)")
        else:
            decision = decide(args.repo, int(args.pr), args.workflows.split(), args.git_dir)
    except Exception as e:  # fail closed on every error (pitfall 14)
        decision = _refuse(f"release gate could not decide: {e}")
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write("## release-gate\n\n" + ("passed: " if decision.ok else "refused: ") + decision.message + "\n\n")
            f.write("\n".join(decision.summary) + ("\n" if decision.summary else ""))
    if decision.ok:
        print(f"release-gate: {decision.message}")
        return 0
    first, _, rest = decision.message.partition("\n")
    print(f"::error::release-gate: {first}")
    if rest:
        print(rest)
    return 1


if __name__ == "__main__":
    sys.exit(main())
