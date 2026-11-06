#!/usr/bin/env python3
"""The done-gate a builder runs before it reports (workspace pitfalls.md 3; AGENTS.md "Running agents").

    python3 .github/workspace/done_check.py <worktree> [--pr <repo>#<n>] [--branch <name>]

It fails when the work that was reported is not the work that was pushed:
  1. the worktree has uncommitted or untracked files (`.context/` is ignored: packets are never committed);
  2. HEAD is not on the remote branch (`--branch`, else the pull request's head branch, else the upstream);
  3. with `--pr`: the pull request's files are not exactly the files HEAD changes against the pull request's base,
     or the pull request's head commit is not HEAD.
It prints one line per problem and ends `DONE CHECK OK` or `DONE CHECK FAILED`. Exit 0 only when OK.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import time

IGNORED_PREFIXES = (".context/",)


def run(cmd: list[str], cwd: pathlib.Path) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout


def dirty_files(status: str) -> list[str]:
    """Paths from `git status --porcelain=v1 -uall`, minus the ignored prefixes."""
    out = []
    for line in status.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].split(" -> ")[-1].strip().strip('"')
        if not path.startswith(IGNORED_PREFIXES):
            out.append(path)
    return out


def compare_files(pr_files: list[str], changed: list[str]) -> list[str]:
    """Problems when the pull request's file list differs from the files HEAD changes."""
    problems = []
    missing = sorted(set(changed) - set(pr_files))
    extra = sorted(set(pr_files) - set(changed))
    if missing:
        problems.append("changed locally but not in the pull request: " + ", ".join(missing))
    if extra:
        problems.append("in the pull request but not in this worktree's HEAD: " + ", ".join(extra))
    return problems


def parse_pr(ref: str) -> tuple[str, str]:
    repo, sep, number = ref.partition("#")
    if not sep or not repo or not number.isdigit():
        raise ValueError(f"--pr must be <repo>#<n>, got {ref!r}")
    return (repo if "/" in repo else f"OnePeek/{repo}"), number


PR_READ_TRIES, PR_READ_WAIT = 4, 5  # GitHub's API can show the old head for a few seconds after a push


def read_pr(repo: str, number: str, head: str, worktree: pathlib.Path, sleep=time.sleep) -> dict:
    """The pull request as GitHub reports it, re-read briefly while its head still lags a fresh push."""
    for attempt in range(PR_READ_TRIES):
        info = json.loads(run(["gh", "pr", "view", number, "-R", repo, "--json", "headRefName,headRefOid,baseRefName,files"],
                              worktree))
        if info.get("headRefOid") == head or attempt == PR_READ_TRIES - 1:
            return info
        sleep(PR_READ_WAIT)
    return info


def check(worktree: pathlib.Path, pr: str | None, branch: str | None) -> list[str]:
    problems: list[str] = []
    dirty = dirty_files(run(["git", "status", "--porcelain=v1", "-uall"], worktree))
    if dirty:
        problems.append("uncommitted or untracked: " + ", ".join(dirty))
    head = run(["git", "rev-parse", "HEAD"], worktree).strip()
    base_ref = None
    if pr:
        repo, number = parse_pr(pr)
        info = read_pr(repo, number, head, worktree)
        branch = branch or info["headRefName"]
        base_ref = info["baseRefName"]
        if info["headRefOid"] != head:
            problems.append(f"the pull request's head is {info['headRefOid'][:7]}, not HEAD {head[:7]}: push HEAD")
        run(["git", "fetch", "-q", "origin", base_ref], worktree)
        changed = [p for p in run(["git", "diff", "--name-only", f"origin/{base_ref}...HEAD"], worktree).split() if p]
        problems += compare_files([f["path"] for f in info["files"]], changed)
    if branch:
        remote = run(["git", "ls-remote", "origin", f"refs/heads/{branch}"], worktree).split()
        if not remote:
            problems.append(f"branch {branch} is not on origin: push it")
        elif remote[0] != head:
            problems.append(f"origin/{branch} is {remote[0][:7]}, not HEAD {head[:7]}: push HEAD")
    else:
        try:
            upstream = run(["git", "rev-parse", "@{u}"], worktree).strip()
        except subprocess.CalledProcessError:
            problems.append("no upstream and no --branch or --pr: say which branch was pushed")
        else:
            if upstream != head:
                problems.append(f"the upstream is {upstream[:7]}, not HEAD {head[:7]}: push HEAD")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("worktree", type=pathlib.Path)
    ap.add_argument("--pr", help="<repo>#<n>, for example onepeek-infra#177")
    ap.add_argument("--branch", help="the remote branch HEAD was pushed to")
    args = ap.parse_args(argv)
    try:
        problems = check(args.worktree.resolve(), args.pr, args.branch)
    except (subprocess.CalledProcessError, ValueError, KeyError, json.JSONDecodeError) as e:
        problems = [f"could not check: {e}"]  # fail closed (pitfalls.md 14)
    for p in problems:
        print(f"FAIL  {p}")
    print("DONE CHECK FAILED" if problems else "DONE CHECK OK")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
