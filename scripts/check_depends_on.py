#!/usr/bin/env python3
"""Declared dependencies between pull requests (onepeek-docs foundation-playbook.md §8a P1, lesson L26).

DEPRECATED (PIPE-SIMPLE v2 DOC-3): callers stop calling depends-on.yml; the merge tool (vanguard ship) alone refuses a
pull request whose `Depends on:` pull request is open. This file and depends-on.yml stay working because callers pinned
to older tags still call depends-on.yml, which runs this script at that same tag, and Vanguard's merge tool imports
parse, draft_gate, docs_waiting and open_docs_prs from this repo's dev. Delete both in a later sweep once no caller
pins a tag that uses them and Vanguard no longer imports them (VG-1).
The docs-pin branch (lesson L27: docs-version.txt read as an implicit dependency) is gone from `--check` (DOC-1):
`--docs-pin` and `--docs-read` print nothing and `--check` reads no docs pin. docs_waiting and open_docs_prs stay as a
library for Vanguard only.

A pull request whose CI needs another repo's unmerged change says so in its body, one line each:

    Depends on: OnePeek/<repo>#<n>

    python3 check_depends_on.py --repos        # reads the body (BODY_FILE, else BODY); prints the named repos, comma-separated (for the App token)
    python3 check_depends_on.py --check        # reads the body and GH_TOKEN; exit 1 "waiting for <repo>#<n>" while a
                                               # declared one is unmerged, or names one closed without merging
    IS_DRAFT=false python3 check_depends_on.py --check   # also a notice while an open dependency's pull request is not a draft
    python3 check_depends_on.py --docs-pin | --docs-read  # deprecated: print nothing (DOC-1); --strict and --repo-dir are ignored

The body is the pull request's live body, fetched by depends-on.yml through the API into BODY_FILE (the event payload
holds the body as of the trigger, so a body edited after it would be ignored); BODY is the fallback for other callers.

Used by depends-on.yml, Vanguard's preflight.py and ship_change_set.py. Standard library only.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

ORG = "OnePeek"
DOCS = "onepeek-docs"
# A repo name is ASCII letters, digits, `.`, `_` and `-`, never only dots (`.` and `..` are not repos).
LINE = re.compile(r"^[ \t]*Depends on:[ \t]*OnePeek/(?!\.+#)([A-Za-z0-9._-]+)#(\d+)[ \t]*$", re.M | re.I | re.ASCII)


def _load_check_pins():
    """check_pins.py beside this file (its docs pin reader and compare), loaded by path so callers that load this
    module by path (ship_change_set.py, preflight.py) need nothing on sys.path."""
    spec = importlib.util.spec_from_file_location("check_pins", pathlib.Path(__file__).resolve().parent / "check_pins.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


check_pins = _load_check_pins()


def read_body() -> str:
    """The pull request body: the file BODY_FILE names (the live body from the API), else the BODY variable."""
    path = os.environ.get("BODY_FILE", "")
    return pathlib.Path(path).read_text(encoding="utf-8") if path else os.environ.get("BODY", "")


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


def open_docs_prs(sha: str, token: str) -> list[int]:
    """The open onepeek-docs pull requests whose commits include sha (GitHub's commits/<sha>/pulls), lowest first."""
    req = urllib.request.Request(f"{os.environ.get('GITHUB_API_URL', 'https://api.github.com')}/repos/{ORG}/{DOCS}/commits/{sha}/pulls",
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            prs = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (404, 422):  # the commit is not in onepeek-docs
            return []
        raise SystemExit(f"::error::cannot list the docs pull requests of {sha[:7]}: HTTP {e.code}")
    return sorted(pr["number"] for pr in prs if pr.get("state") == "open")


def docs_waiting(sha: str | None, status, prs, strict: bool = False, trunk: str | None = None) -> tuple[list[str], list[str]]:
    """The implicit docs dependency (lesson L27 a) as (problems, notes). Nothing when there is no pin or it is on docs'
    trunk. A pin in an open docs pull request is a note ("waiting for onepeek-docs#N ..."), the normal state of a change
    set, or a problem when `strict`; a pin in no open docs pull request is always a problem.
    `status(sha)` is the compare status of docs' trunk against sha, `prs(sha)` the open docs pull requests containing it.
    `trunk` is the branch `status` compared against, named in the message (main passes the one docs_trunk read);
    without it the message names no branch, so it never points people at a branch the check did not read."""
    if not sha or status(sha) in ("ahead", "identical"):
        return [], []
    found = prs(sha)
    on = f"docs {trunk}" if trunk else "the docs trunk"
    if found and strict:
        return [f"waiting for {DOCS}#{found[0]} (docs-version.txt pins {sha[:7]}, not yet on {on}, a commit of that open "
                f"pull request)"], []
    if found:
        return [], [f"waiting for {DOCS}#{found[0]} (docs-version.txt pins {sha[:7]}, not yet on {on}); "
                    f"ship_change_set.py merges it first"]
    return [f"docs pin {sha[:7]} not on {on} and in no open docs PR; move docs-version.txt to a commit on "
            f"{trunk or 'the docs trunk'} or of the change set's docs pull request"], []


def waiting(deps: list[tuple[str, int]], state) -> list[str]:
    """One message per dependency that is not merged yet; `state(repo, n)` returns (state, merged)."""
    out = []
    for repo, n in deps:
        st, merged = state(repo, n)
        if merged:
            continue
        out.append(f"waiting for {repo}#{n}" if st == "open" else f"{repo}#{n} was closed without merging; remove the line or reopen it")
    return out


def draft_gate(deps: list[tuple[str, int]], is_draft: bool | None, state) -> list[str]:
    """development-cycle §2c step 2: a pull request with an open dependency is a draft. One message per open dependency
    when it is not (empty when `is_draft` is unknown, or the pull request is a draft); `state(repo, n)` returns
    (state, merged). A notice for the author; the merge tool (ship_change_set.py) enforces it."""
    if is_draft is None or is_draft:
        return []
    return [f"{repo}#{n} is still open: raise this pull request as a draft marked `depends on {repo}#{n}` until it merges"
            for repo, n in deps if not state(repo, n)[1]]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--repos", action="store_true")
    g.add_argument("--docs-pin", action="store_true")
    g.add_argument("--docs-read", action="store_true")
    g.add_argument("--check", action="store_true")
    ap.add_argument("--strict", action="store_true", help="deprecated, no effect (it applied to the docs pin only, DOC-1)")
    ap.add_argument("--repo-dir", default=".", help="deprecated, ignored (the docs pin is no dependency, DOC-1)")
    a = ap.parse_args(argv)
    deps = parse(read_body())
    if a.repos:
        print(",".join(dict.fromkeys(r for r, _ in deps)))
        return 0
    if a.docs_pin or a.docs_read:  # deprecated (DOC-1): the docs pin is no dependency any more
        print("")
        return 0
    problems: list[str] = []
    if deps:
        token = os.environ.get("GH_TOKEN", "")
        problems += waiting(deps, lambda r, n: api_state(r, n, token))
    else:
        print("No `Depends on:` lines: nothing to wait for.")
    for p in problems:
        print(f"::error::{p}")
    if deps and os.environ.get("IS_DRAFT", "") in ("true", "false"):  # the pull request's draft flag, from depends-on.yml
        try:  # a notice only: an API error here never fails the job (the dependency wait above reports it)
            for note in draft_gate(deps, os.environ["IS_DRAFT"] == "true", lambda r, n: api_state(r, n, os.environ.get("GH_TOKEN", ""))):
                print(f"::notice title=depends-on::{note}")
        except SystemExit as e:
            print(f"::notice title=depends-on::draft check skipped: {e}")
    if deps and not problems:
        print("Every declared dependency is merged: " + ", ".join(f"{r}#{n}" for r, n in deps))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
