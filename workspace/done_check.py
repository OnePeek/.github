#!/usr/bin/env python3
"""The done-gate a builder runs before it reports (workspace pitfalls.md 3; agent-operations.md §5.4 step 5).

    python3 "$ONEPEEK_WORKSPACE/.github/workspace/done_check.py" <worktree> [--pr <repo>#<n>] [--branch <name>]
        [--expect <file> ...] [--since <sha>]

It fails when the work that was reported is not the work that was pushed:
  1. the worktree is not a linked worktree (`git worktree add`, agent-operations.md §4.3);
  2. the worktree has uncommitted or untracked files (only untracked files under a top-level `.context/` are ignored:
     packets and status files are never committed; a tracked file there that changed still fails), or HEAD tracks any
     file under `.context/`, in every mode (deleting one that was committed earlier passes);
  3. HEAD is not on the remote branch (`--branch`, else the pull request's head branch, else the upstream); a pull
     request from a fork is checked in its head repository, not in `origin`;
  4. with `--pr`: the pull request's head commit is not HEAD, `--branch` is not its head branch, the base cannot be
     fetched, or the pull request's files are not exactly the files HEAD changes against the base (HEAD not
     containing the current base is only a WARN: the merge tool merges a branch that is behind its base);
  5. with `--expect`: the pull request's files are not exactly the files the builder reports;
  6. with `--since <sha>` (needs `--pr` and `--expect`; for a builder that adds commits to a longer pull request): the
     `--expect` files are compared with the files changed by the commits after <sha> instead of with the whole pull
     request's. <sha> is 7 to 40 hex characters, must name a commit that HEAD contains, and merge commits and commits
     already on the base do not count (merging the base in adds none of its files).
In a batch pull request (operating-model §8: each task on its own branch, merged into the batch branch), run it at the
batch head after your merge, with the batch head before your merge as <sha>:

    git -C <worktree> fetch origin <batch> && before=$(git -C <worktree> rev-parse origin/<batch>)
    # merge your task branch into the batch branch, push it, then:
    python3 "$ONEPEEK_WORKSPACE/.github/workspace/done_check.py" <worktree> --pr <repo>#<batch PR> \
        --since "$before" --expect <your files>

With `--pr` it always prints the pull request's files, and a WARN when the change is over the slice cap of
agent-operations.md §4.5 (generated files aside). It prints one line per problem and ends `DONE CHECK OK` or
`DONE CHECK FAILED`. Exit 0 only when OK.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
import time

CONTEXT_DIR = ".context"  # untracked files under it are never committed
SLICE_CAP = 400  # changed lines, generated files aside (agent-operations.md §4.5)
GENERATED_NAMES = ("pnpm-lock.yaml", "uv.lock")
GENERATED_SUFFIXES = (".lock",)
GENERATED_DIRS = ("rendered",)


def run(cmd: list[str], cwd: pathlib.Path) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, encoding="utf-8",
                          errors="surrogateescape").stdout


def nul_fields(out: str) -> list[str]:
    return [f for f in out.split("\0") if f]


def dirty_files(status: str) -> list[str]:
    """Paths from `git status --porcelain=v1 -z -uall`, minus untracked files under a top-level `.context/`."""
    out, fields, i = [], status.split("\0"), 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        if "R" in xy or "C" in xy:
            i += 1  # the next field is the rename's or copy's source path
        if xy == "??" and path.split("/", 1)[0] == CONTEXT_DIR:
            continue
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


def context_paths(tracked: list[str]) -> list[str]:
    """Problems when HEAD tracks a `.context/` file: packets and status files are never committed (agent-operations.md §5.5).
    `tracked` is every path in HEAD's tree, so a branch without a pull request is checked too, and a deleted one passes."""
    found = sorted(f for f in tracked if f.split("/", 1)[0] == CONTEXT_DIR)
    return ["HEAD commits .context/ files, which are never committed: " + ", ".join(found)] if found else []


def compare_expected(pr_files: list[str], expected: list[str]) -> list[str]:
    """Problems when the pull request's file list differs from the files the builder reports."""
    problems = []
    missing = sorted(set(expected) - set(pr_files))
    extra = sorted(set(pr_files) - set(expected))
    if missing:
        problems.append("reported but not in the pull request: " + ", ".join(missing))
    if extra:
        problems.append("in the pull request but not reported: " + ", ".join(extra))
    return problems


SHA = re.compile(r"[0-9a-f]{7,40}")


def files_since(since: str, base_ref: str, worktree: pathlib.Path) -> tuple[list[str], list[str]]:
    """(files, problems): every path the non-merge commits after `since` change, leaving out commits already on
    origin/<base_ref>, so merging the base in adds none of its files. A rename lists both paths, as `pr_files` does."""
    if not SHA.fullmatch(since):
        return [], [f"--since must be a commit SHA (7 to 40 hex characters), got {since!r}"]
    try:
        full = run(["git", "rev-parse", "--verify", "-q", f"{since}^{{commit}}"], worktree).strip()
    except subprocess.CalledProcessError:
        return [], [f"--since {since} is not a commit in this worktree"]
    try:
        run(["git", "merge-base", "--is-ancestor", full, "HEAD"], worktree)
    except subprocess.CalledProcessError as e:
        return [], [f"--since {since} is not an ancestor of HEAD" if e.returncode == 1
                    else f"could not compare --since {since} with HEAD: {(e.stderr or '').strip()}"]
    commits = run(["git", "rev-list", f"{full}..HEAD", "--not", f"origin/{base_ref}"], worktree).split()
    files: set[str] = set()
    for c in commits:
        # --cc: a plain commit lists its files; a merge lists only what it changed against every parent (a conflict
        # fix or a file added during the merge), never the base's files it brings in (review of .github #64).
        files.update(nul_fields(run(["git", "diff-tree", "-r", "-z", "--cc", "--no-commit-id", "--name-only",
                                     "--no-renames", c], worktree)))
    return sorted(files), []


def compare_since(since_files: list[str], expected: list[str], since: str) -> list[str]:
    """Problems when the files the commits after `since` change differ from the files the builder reports."""
    problems = []
    missing = sorted(set(expected) - set(since_files))
    extra = sorted(set(since_files) - set(expected))
    if missing:
        problems.append(f"reported but not changed after {since}: " + ", ".join(missing))
    if extra:
        problems.append(f"changed after {since} but not reported: " + ", ".join(extra))
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
        info = json.loads(run(["gh", "pr", "view", number, "-R", repo, "--json",
                               "headRefName,headRefOid,baseRefName,isCrossRepository,headRepository,headRepositoryOwner"],
                              worktree))
        if info.get("headRefOid") == head or attempt == PR_READ_TRIES - 1:
            return info
        sleep(PR_READ_WAIT)
    return info


def pr_files(repo: str, number: str, worktree: pathlib.Path) -> list[str]:
    """Every path the pull request changes, paginated (the `files` field of `gh pr view` stops at 100). A rename lists
    its old path too, to match `git diff --no-renames`. One JSON string per line, so no path can split a line."""
    out = run(["gh", "api", "--paginate", f"repos/{repo}/pulls/{number}/files?per_page=100",
               "--jq", ".[] | (.filename, (.previous_filename // empty)) | @json"], worktree)
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def generated(path: str) -> bool:
    parts = path.split("/")
    return parts[-1] in GENERATED_NAMES or parts[-1].endswith(GENERATED_SUFFIXES) or \
        any(p in GENERATED_DIRS for p in parts[:-1])


def changed_lines(numstat: str) -> int:
    """Added plus deleted lines from `git diff --numstat -z --no-renames`, generated and binary files aside."""
    total = 0
    for entry in nul_fields(numstat):
        added, deleted, path = entry.split("\t", 2)
        if added == "-" or generated(path):
            continue
        total += int(added) + int(deleted)
    return total


def linked_worktree_problem(worktree: pathlib.Path) -> str | None:
    dirs = run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir", "--git-dir"], worktree).splitlines()
    common, own = (pathlib.Path(d).resolve() for d in dirs[:2])
    if common == own:
        return "not a linked worktree: work in `git worktree add --detach <scratchpad>/<name> origin/<branch>`"
    return None


def check(worktree: pathlib.Path, pr: str | None, branch: str | None, expect: list[str] | None = None,
          since: str | None = None) -> list[str]:
    problems: list[str] = []
    why = linked_worktree_problem(worktree)
    if why:
        problems.append(why)
    dirty = dirty_files(run(["git", "status", "--porcelain=v1", "-z", "-uall"], worktree))
    if dirty:
        problems.append("uncommitted or untracked: " + ", ".join(dirty))
    head = run(["git", "rev-parse", "HEAD"], worktree).strip()
    problems += context_paths(nul_fields(run(["git", "ls-tree", "-r", "-z", "--name-only", "HEAD"], worktree)))
    remote_repo = "origin"
    if expect is not None and not pr:
        problems.append("--expect needs --pr: the expected files are compared with the pull request's")
    if since is not None and (not pr or expect is None):
        problems.append("--since needs --pr and --expect: it compares the reported files with the commits after it")
        since = None
    if pr:
        repo, number = parse_pr(pr)
        info = read_pr(repo, number, head, worktree)
        if branch and branch != info["headRefName"]:
            problems.append(f"--branch {branch} is not the pull request's head branch {info['headRefName']}")
        branch = branch or info["headRefName"]
        base_ref = info["baseRefName"]
        if info.get("isCrossRepository"):  # a fork: its head branch lives in the fork, not in origin
            remote_repo = f"https://github.com/{info['headRepositoryOwner']['login']}/{info['headRepository']['name']}.git"
        if info["headRefOid"] != head:
            problems.append(f"the pull request's head is {info['headRefOid'][:7]}, not HEAD {head[:7]}: push HEAD")
        files = pr_files(repo, number, worktree)
        print(f"pull request files ({len(files)}):")
        for f in files:
            print(f"  {f}")
        if expect is not None and since is None:
            problems += compare_expected(files, expect)
        try:
            run(["git", "fetch", "-q", "origin", base_ref], worktree)
        except subprocess.CalledProcessError as e:
            problems.append(f"could not fetch origin/{base_ref}: {(e.stderr or '').strip()}")
            return problems  # every later step would compare against a stale base
        try:
            run(["git", "merge-base", "--is-ancestor", f"origin/{base_ref}", "HEAD"], worktree)
        except subprocess.CalledProcessError as e:
            if e.returncode != 1:
                problems.append(f"could not compare HEAD with origin/{base_ref}: {(e.stderr or '').strip()}")
            else:  # a WARN (2026-11-07): the merge tool merges a branch that is behind its base
                print(f"WARN  HEAD does not contain the current origin/{base_ref}; the comparisons use the merge base")
        if since is not None and expect is not None:
            since_files, why = files_since(since, base_ref, worktree)
            problems += why or compare_since(since_files, expect, since)
            if not why:
                print(f"files changed after {since} ({len(since_files)}):")
                for f in since_files:
                    print(f"  {f}")
        diff = ["git", "diff", "-z", "--no-renames", f"origin/{base_ref}...HEAD"]
        changed = nul_fields(run([*diff[:2], "--name-only", *diff[2:]], worktree))
        problems += compare_files(files, changed)
        lines = changed_lines(run([*diff[:2], "--numstat", *diff[2:]], worktree))
        if lines > SLICE_CAP:
            print(f"WARN  {lines} changed lines (generated files aside), over the slice cap of {SLICE_CAP} "
                  "(agent-operations.md §4.5): split the slice")
    if branch:
        remote = run(["git", "ls-remote", remote_repo, f"refs/heads/{branch}"], worktree).split()
        if not remote:
            problems.append(f"branch {branch} is not on {remote_repo}: push it")
        elif remote[0] != head:
            where = f"origin/{branch}" if remote_repo == "origin" else f"{remote_repo} {branch}"
            problems.append(f"{where} is {remote[0][:7]}, not HEAD {head[:7]}: push HEAD")
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
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("worktree", type=pathlib.Path)
    ap.add_argument("--pr", help="<repo>#<n>, for example onepeek-infra#177")
    ap.add_argument("--branch", help="the remote branch HEAD was pushed to")
    ap.add_argument("--expect", nargs="+", metavar="FILE", help="the files the builder reports as changed")
    ap.add_argument("--since", metavar="SHA", help="compare --expect with the files of the commits after SHA only")
    args = ap.parse_args(argv)
    try:
        problems = check(args.worktree.resolve(), args.pr, args.branch, args.expect, args.since)
    except (subprocess.CalledProcessError, ValueError, KeyError, TypeError, OSError, json.JSONDecodeError) as e:
        problems = [f"could not check: {e}"]  # fail closed (pitfalls.md 14)
    for p in problems:
        print(f"FAIL  {p}")
    print("DONE CHECK FAILED" if problems else "DONE CHECK OK")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
