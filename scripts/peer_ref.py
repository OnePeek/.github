"""The other repos' ref for a cross-repo check: the one place the rule lives (delivery-pipelines §6.5, decision 153).

Usage: peer_ref.py --base <base_ref> --head <head_ref> --owner <owner> --repos onepeek-infra,backend-service

Prints one `<repo>=<ref>` line per repo and nothing else on stdout. With --commits each line is `<repo>=<ref> <sha>`,
the 40-hex commit the ref names now: the callers check out that commit and record `peer: <repo>@<sha>` from it, and
ship_change_set.py re-checks that line before a promotion merges. Errors go to stderr as `::error::` lines, exit 1.
Reads `GET /repos/<owner>/<repo>/pulls?state=open&base=release&head=<owner>:dev` and, for --commits with a branch ref,
`GET /repos/<owner>/<repo>/commits/<ref>`, through `gh api` (GH_TOKEN). Standard library only.

  pull request into dev                 -> dev
  hotfix/* head into release            -> release (a hotfix ships against what is live)
  any other head into release           -> the head commit of the repo's one open dev -> release pull request, else release
Fails closed: pull requests unreadable; more than one open promotion; a head that is not a 40-hex SHA; a base other
than dev or release; a repo that is not one of the four.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys

REPOS = ("backend-service", "onepeek-web", "onepeek-infra", "mcp-athena")
SHA = re.compile(r"[0-9a-f]{40}")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")


class Refused(Exception):
    pass


def candidate(owner: str, repo: str) -> str:
    """The release candidate of repo: its one open dev -> release pull request's head commit, else `release`."""
    path = f"repos/{owner}/{repo}/pulls?state=open&base=release&head={owner}:dev"
    try:
        out = subprocess.run(["gh", "api", path, "--jq", ".[].head.sha"], capture_output=True, text=True, check=False)
    except OSError as e:
        raise Refused(f"Cannot read {repo}'s open pull requests into release ({e}), so its release candidate is unknown.")
    if out.returncode != 0:
        raise Refused(f"Cannot read {repo}'s open pull requests into release, so its release candidate is unknown.")
    heads = out.stdout.split()
    if not heads:
        return "release"
    if len(heads) > 1 or not SHA.fullmatch(heads[0]):
        raise Refused(f"{repo}: expected at most one open dev -> release pull request with a commit SHA, got: {' '.join(heads)}")
    return heads[0]


def commit(owner: str, repo: str, ref: str) -> str:
    """The commit ref is at (a SHA is its own commit, with no read)."""
    if SHA.fullmatch(ref):
        return ref
    try:
        out = subprocess.run(["gh", "api", f"repos/{owner}/{repo}/commits/{ref}", "--jq", ".sha"], capture_output=True, text=True, check=False)
    except OSError as e:
        raise Refused(f"Cannot read {repo}'s commit at {ref} ({e}), so the version it is checked at is unknown.")
    sha = out.stdout.strip()
    if out.returncode != 0 or not SHA.fullmatch(sha):
        raise Refused(f"Cannot read {repo}'s commit at {ref}, so the version it is checked at is unknown.")
    return sha


def resolve(base: str, head: str, owner: str, repos: list[str]) -> list[tuple[str, str]]:
    if base not in ("dev", "release"):
        raise Refused(f"base must be dev or release, not '{base}'.")
    if not NAME.fullmatch(owner):
        raise Refused(f"owner '{owner}' is not a valid owner name.")
    for repo in repos:
        if repo not in REPOS:
            raise Refused(f"repo must be one of {', '.join(REPOS)}, not '{repo}'.")
    if not repos:
        raise Refused("repos is empty.")
    out = []
    for repo in repos:
        if base == "dev":
            ref = "dev"
        elif head.startswith("hotfix/"):
            ref = "release"
        else:
            ref = candidate(owner, repo)
        out.append((repo, ref))
    return out


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for a in ("base", "head", "owner", "repos"):
        p.add_argument(f"--{a}", required=True)
    p.add_argument("--commits", action="store_true", help="print `<repo>=<ref> <sha>`")
    args = p.parse_args(argv)
    try:
        pairs = resolve(args.base, args.head, args.owner, [r for r in args.repos.split(",") if r])
        lines = [f"{repo}={ref}" + (f" {commit(args.owner, repo, ref)}" if args.commits else "") for repo, ref in pairs]
    except Refused as e:
        print(f"::error::{e}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
