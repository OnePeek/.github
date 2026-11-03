#!/usr/bin/env python3
"""Declared dependencies between pull requests (onepeek-docs foundation-playbook.md §8a P1, lesson L26).

A pull request whose CI needs another repo's unmerged change says so in its body, one line each:

    Depends on: OnePeek/<repo>#<n>

    python3 check_depends_on.py --repos        # reads BODY; prints the named repos, comma-separated (for the App token)
    python3 check_depends_on.py --check        # reads BODY and GH_TOKEN; exit 1 "waiting for <repo>#<n>" while one is unmerged

Used by depends-on.yml (every repo's first pull-request job), preflight.py and ship_change_set.py. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

ORG = "OnePeek"
# A repo name is ASCII letters, digits, `.`, `_` and `-`, never only dots (`.` and `..` are not repos).
LINE = re.compile(r"^[ \t]*Depends on:[ \t]*OnePeek/(?!\.+#)([A-Za-z0-9._-]+)#(\d+)[ \t]*$", re.M | re.I | re.ASCII)


def parse(body: str | None) -> list[tuple[str, int]]:
    """The declared dependencies, in order, each once."""
    out: list[tuple[str, int]] = []
    for m in LINE.finditer(body or ""):
        dep = (m.group(1), int(m.group(2)))
        if dep not in out:
            out.append(dep)
    return out


def api_state(repo: str, n: int, token: str) -> tuple[str, bool]:
    """(state, merged) of OnePeek/<repo>#<n> from the API."""
    req = urllib.request.Request(f"{os.environ.get('GITHUB_API_URL', 'https://api.github.com')}/repos/{ORG}/{repo}/pulls/{n}",
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            pr = json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"::error::cannot read {repo}#{n}: HTTP {e.code}")
    return pr["state"], bool(pr.get("merged"))


def waiting(deps: list[tuple[str, int]], state) -> list[str]:
    """One message per dependency that is not merged yet; `state(repo, n)` returns (state, merged)."""
    out = []
    for repo, n in deps:
        st, merged = state(repo, n)
        if merged:
            continue
        out.append(f"waiting for {repo}#{n}" if st == "open" else f"{repo}#{n} was closed without merging; remove the line or reopen it")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--repos", action="store_true")
    g.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    deps = parse(os.environ.get("BODY", ""))
    if a.repos:
        print(",".join(dict.fromkeys(r for r, _ in deps)))
        return 0
    if not deps:
        print("No `Depends on:` lines: nothing to wait for.")
        return 0
    token = os.environ.get("GH_TOKEN", "")
    problems = waiting(deps, lambda r, n: api_state(r, n, token))
    for p in problems:
        print(f"::error::{p}")
    if not problems:
        print("Every declared dependency is merged: " + ", ".join(f"{r}#{n}" for r, n in deps))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
