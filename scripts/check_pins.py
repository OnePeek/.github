#!/usr/bin/env python3
"""The pin check (onepeek-docs architecture/lld/infra/foundation-playbook.md §8a P2 and S4).

    python3 check_pins.py <repo-dir> [--docs auto|always|never]
    python3 check_pins.py --docs-needed     # prints true or false for this CI event (auto)

1. Every `uses:` in the repo's workflows that names `OnePeek/.github` (in any letter case) is a pin: its ref is a
   40-character commit SHA (a branch or a tag name fails), the commit of a `v*` tag of OnePeek/.github (S4), and all of
   them are the commit of **one** tag (P2).
2. `docs-version.txt` (`onepeek-docs@<sha>`) is an ancestor of onepeek-docs `main`. With `--docs auto` (CI) this runs
   only on a push to `dev` or `release` and on a pull request into `release`; a pull request into `dev` pins the open
   docs pull request's head during a change set (development-cycle §2a item 2). It reads the private docs repo through
   the API with GH_TOKEN (the App token in CI, `gh auth token` on a laptop).

A repo with no `docs-version.txt` (onepeek-docs, .github) has no docs pin to check. Exit 1 when a rule is broken.
Standard library only: our runner image has no pip.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.request

ORG = "OnePeek"
# Every `uses:` naming OnePeek/.github, in any letter case, with whatever ref it has (P2): `path` is the part after
# `OnePeek/.github/` (empty for the repo root), `ref` the text after `@` (empty when there is none), `comment` an
# optional trailing `# ...`.
USES = re.compile(r"""(?P<lead>\buses:[ \t]*["']?)(?P<name>OnePeek/\.github)(?=[/@\s"']|$)(?:/(?P<path>[^@\s"'#]*))?"""
                  r"""(?:@(?P<ref>[^\s"'#]*))?(?P<quote>["']?)(?P<comment>[ \t]+#[^\n]*)?""", re.I | re.M)
SHA = re.compile(r"[0-9a-f]{40}")
TAG = re.compile(r"v\d+\.\d+\.\d+")
DOCS_PIN = re.compile(r"onepeek-docs@([0-9a-f]{40})")


def pins(repo: pathlib.Path) -> list[tuple[str, int, str, str]]:
    """(file, line, what it calls, ref) for every `uses:` naming OnePeek/.github in the repo's workflows."""
    out = []
    for f in sorted((repo / ".github" / "workflows").glob("*.y*ml")):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            for m in USES.finditer(line):
                out.append((f.name, n, pathlib.PurePosixPath(m.group("path") or m.group("name")).name, m.group("ref") or ""))
    return out


def parse_tags(ls_remote: str) -> dict[str, list[str]]:
    """commit sha -> the `v*` tags on it, from `git ls-remote --tags` output (an annotated tag's peeled `^{}` line
    names its commit; a lightweight tag's line already does)."""
    commit: dict[str, str] = {}
    for line in ls_remote.splitlines():
        sha, _, ref = line.partition("\t")
        name = ref.removeprefix("refs/tags/")
        peeled = name.endswith("^{}")
        name = name.removesuffix("^{}")
        if not TAG.fullmatch(name):
            continue
        if peeled or name not in commit:
            commit[name] = sha
    by_sha: dict[str, list[str]] = {}
    for name, sha in commit.items():
        by_sha.setdefault(sha, []).append(name)
    return by_sha


def remote_tags() -> dict[str, list[str]]:
    r = subprocess.run(["git", "ls-remote", "--tags", f"https://github.com/{ORG}/.github", "refs/tags/v*"],
                       capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"cannot list the tags of {ORG}/.github: {r.stderr.strip()[:300]}")
    return parse_tags(r.stdout)


def pin_problems(found: list[tuple[str, int, str, str]], tags: dict[str, list[str]]) -> list[str]:
    problems = []
    for f, n, wf, ref in found:
        if not SHA.fullmatch(ref):
            problems.append(f"{f}:{n} pins {wf} at {ref!r}, which is not a 40-character commit SHA; pin the commit of a v* tag")
        elif ref not in tags:
            problems.append(f"{f}:{n} pins {wf} at {ref[:7]}, which is not the commit of a v* tag of {ORG}/.github")
    shas = sorted({ref for *_, ref in found})
    if len(shas) > 1:
        named = ", ".join(f"{sha[:7] if SHA.fullmatch(sha) else repr(sha)} ({'/'.join(sorted(tags.get(sha, ['untagged'])))})"
                          for sha in shas)
        problems.append(f"the {ORG}/.github pins are not one version: {named}; move them all with one pin-move")
    return problems


def docs_pin(repo: pathlib.Path) -> tuple[bool, str | None]:
    """(has the file, the pinned docs commit or None). Comment lines (#) are ignored."""
    f = repo / "docs-version.txt"
    if not f.exists():
        return False, None
    for line in f.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("#"):
            continue
        m = DOCS_PIN.search(line)
        if m:
            return True, m.group(1)
    return True, None


def docs_needed(env: dict[str, str]) -> bool:
    """The docs ancestor rule applies on a push to dev or release and on a pull request into release (P2)."""
    event = env.get("GITHUB_EVENT_NAME", "")
    if event == "push":
        return env.get("GITHUB_REF", "") in ("refs/heads/dev", "refs/heads/release")
    if event == "pull_request":
        return env.get("GITHUB_BASE_REF", "") == "release"
    return False


def compare_status(sha: str, token: str) -> str:
    """GitHub's compare status of docs `main` against the pinned commit: `ahead` or `identical` means the commit is
    an ancestor of main."""
    req = urllib.request.Request(
        f"{os.environ.get('GITHUB_API_URL', 'https://api.github.com')}/repos/{ORG}/onepeek-docs/compare/{sha}...main",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)["status"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return "missing"
        raise SystemExit(f"cannot compare {sha[:7]} with onepeek-docs main: HTTP {e.code}")


def docs_problems(repo: pathlib.Path, token: str, status=compare_status) -> list[str]:
    has, sha = docs_pin(repo)
    if not has:
        return []
    if not sha:
        return ["docs-version.txt holds no onepeek-docs@<40-character sha> line"]
    if not token:
        return ["no token to read onepeek-docs (pass the agents' App secrets, playbook §8a S6)"]
    st = status(sha, token)
    if st in ("ahead", "identical"):
        return []
    if st == "missing":
        return [f"docs-version.txt pins {sha[:7]}, which onepeek-docs does not have"]
    return [f"docs-version.txt pins {sha[:7]}, which is not an ancestor of onepeek-docs main ({st}); "
            "merge the docs pull request first, or move the pin to a commit on main"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("repo", nargs="?")
    ap.add_argument("--docs", choices=("auto", "always", "never"), default="never")
    ap.add_argument("--docs-needed", action="store_true", help="print whether this CI event runs the docs rule")
    a = ap.parse_args(argv)
    if a.docs_needed:
        print("true" if docs_needed(dict(os.environ)) else "false")
        return 0
    if not a.repo:
        ap.error("repo is required")
    repo = pathlib.Path(a.repo)
    found = pins(repo)
    problems = pin_problems(found, remote_tags()) if found else []
    if found and not problems:
        print(f"OK  {len(found)} {ORG}/.github pin(s), all at {found[0][3][:7]}")
    elif not found:
        print(f"OK  no {ORG}/.github pins")
    run_docs = a.docs == "always" or (a.docs == "auto" and docs_needed(dict(os.environ)))
    if run_docs:
        docs = docs_problems(repo, os.environ.get("GH_TOKEN", ""))
        problems += docs
        if not docs:
            print("OK  docs-version.txt is an ancestor of onepeek-docs main (or the repo has none)")
    else:
        print("--  docs-version.txt ancestor rule not run for this event (pull requests into dev pin the docs PR head)")
    for p in problems:
        print(f"::error::{p}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
