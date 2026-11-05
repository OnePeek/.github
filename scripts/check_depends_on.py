#!/usr/bin/env python3
"""Declared dependencies between pull requests (onepeek-docs foundation-playbook.md §8a P1, lesson L26).

A pull request whose CI needs another repo's unmerged change says so in its body, one line each:

    Depends on: OnePeek/<repo>#<n>

A repo's `docs-version.txt` is an implicit dependency (lesson L27, item a): when it names a onepeek-docs commit that is
not on docs `main` but is a commit of an open onepeek-docs pull request, that is the normal state of a change set until
the merge (`ship_change_set.py` merges docs first), so `--check` prints a GitHub Actions notice and passes. With
`--strict` (callers that gate a merge) that wait fails as a declared one does. When no open docs pull request contains
the pin, the check fails in both modes; so does a declared `Depends on:` line that is unmerged or closed unmerged.

    python3 check_depends_on.py --repos        # reads the body (BODY_FILE, else BODY); prints the named repos, comma-separated (for the App token)
    python3 check_depends_on.py --docs-pin     # prints the docs commit docs-version.txt names, or nothing
    python3 check_depends_on.py --check        # reads the body, GH_TOKEN and DOCS_TOKEN; exit 1 "waiting for <repo>#<n>"
                                               # while a declared one is unmerged (DOCS_TOKEN: Contents and Pull requests
                                               # read on onepeek-docs, used only when docs-version.txt names a commit)
    IS_DRAFT=false python3 check_depends_on.py --check   # also a notice while an open dependency's pull request is not a draft
    python3 check_depends_on.py --check --strict   # the docs-pin wait fails too

The body is the pull request's live body, fetched by depends-on.yml through the API into BODY_FILE (the event payload
holds the body as of the trigger, so a body edited after it would be ignored); BODY is the fallback for other callers.

Used by depends-on.yml (every pull-request workflow's first job), preflight.py and ship_change_set.py. Standard library only.
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


def docs_waiting(sha: str | None, status, prs, strict: bool = False) -> tuple[list[str], list[str]]:
    """The implicit docs dependency (lesson L27 a) as (problems, notes). Nothing when there is no pin or it is on docs
    main. A pin in an open docs pull request is a note ("waiting for onepeek-docs#N ..."), the normal state of a change
    set, or a problem when `strict`; a pin in no open docs pull request is always a problem.
    `status(sha)` is the compare status of main against sha, `prs(sha)` the open docs pull requests containing it."""
    if not sha or status(sha) in ("ahead", "identical"):
        return [], []
    found = prs(sha)
    if found and strict:
        return [f"waiting for {DOCS}#{found[0]} (docs-version.txt pins {sha[:7]}, a commit of that open pull request)"], []
    if found:
        return [], [f"waiting for {DOCS}#{found[0]} (docs-version.txt pins {sha[:7]}); ship_change_set.py merges it first"]
    return [f"docs pin {sha[:7]} not on main and in no open docs PR; move docs-version.txt to a commit on main "
            "or of the change set's docs pull request"], []


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
    g.add_argument("--check", action="store_true")
    ap.add_argument("--strict", action="store_true", help="with --check: waiting for the docs pull request fails")
    ap.add_argument("--repo-dir", default=".", help="the caller repo's checkout (its docs-version.txt)")
    a = ap.parse_args(argv)
    deps = parse(read_body())
    if a.repos:
        print(",".join(dict.fromkeys(r for r, _ in deps)))
        return 0
    _, pin = check_pins.docs_pin(pathlib.Path(a.repo_dir))
    if a.docs_pin:
        print(pin or "")
        return 0
    problems, waiting_docs = [], False
    if pin:
        docs_token = os.environ.get("DOCS_TOKEN", "")
        if not docs_token:
            problems.append("no token to read onepeek-docs (pass the agents' App secrets, playbook §8a S6)")
        else:
            found, notes = docs_waiting(pin, lambda s: check_pins.compare_status(s, docs_token),
                                        lambda s: open_docs_prs(s, docs_token), a.strict)
            problems += found
            for note in notes:
                print(f"::notice title=depends-on::{note}")
            waiting_docs = bool(notes)
            if not problems and not notes:
                print(f"docs-version.txt pins {pin[:7]}, on onepeek-docs main.")
    if deps:
        token = os.environ.get("GH_TOKEN", "")
        problems += waiting(deps, lambda r, n: api_state(r, n, token))
    elif not problems and not waiting_docs:
        print("No `Depends on:` lines: nothing to wait for.")
    for p in problems:
        print(f"::error::{p}")
    if deps and os.environ.get("IS_DRAFT", "") in ("true", "false"):  # the pull request's draft flag, from depends-on.yml
        for note in draft_gate(deps, os.environ["IS_DRAFT"] == "true", lambda r, n: api_state(r, n, os.environ.get("GH_TOKEN", ""))):
            print(f"::notice title=depends-on::{note}")
    if deps and not problems:
        print("Every declared dependency is merged: " + ", ".join(f"{r}#{n}" for r, n in deps))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
