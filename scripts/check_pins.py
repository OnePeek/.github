#!/usr/bin/env python3
"""The pin check (onepeek-docs architecture/lld/infra/foundation-playbook.md §8a P2 and S4).

    python3 check_pins.py <repo-dir>

Every `uses:` in the repo's workflows that names `OnePeek/.github` (in any letter case) is a pin: its ref is a
   40-character commit SHA (a branch or a tag name fails), the commit of a `v*` tag of OnePeek/.github (S4), and all of
   them are the commit of **one** tag (P2). Exit 1 when the rule is broken.

The docs ancestor rule (`docs-version.txt` on onepeek-docs' trunk, and its `--docs` flag) is gone (PIPE-SIMPLE v2
DOC-1: cross-repo docs are read at docs `dev`); a repo with or without `docs-version.txt` passes the same way.
DEPRECATED, kept only as a library: docs_pin, docs_trunk, compare_status, docs_problems and DOCS_PIN are still imported
by Vanguard's merge tool and preflight (tools/ship_change_set.py, tools/preflight.py, loaded from this repo's dev);
the check itself never runs them. Delete them once Vanguard no longer imports them (PIPE-SIMPLE v2 VG-1).
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
                  r"""(?:@(?P<ref>[^\s"'#},]*))?(?P<quote>["']?)(?P<comment>[ \t]+#[^\n]*)?""", re.I | re.M)
SHA = re.compile(r"[0-9a-f]{40}")
TAG = re.compile(r"v\d+\.\d+\.\d+")
DOCS_PIN = re.compile(r"onepeek-docs@([0-9a-f]{40})")
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")  # a default branch we put in a URL path: no `/`, space or `..`


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


def _api() -> str:
    return os.environ.get('GITHUB_API_URL', 'https://api.github.com')


def docs_trunk(token: str, opener=None) -> str:
    """onepeek-docs' trunk: the repo's `default_branch` from `GET /repos/OnePeek/onepeek-docs` (decision 176: `dev`).
    Any answer that is not a plain branch name is a refusal (SystemExit), never a silent fallback to a guessed name."""
    req = urllib.request.Request(f"{_api()}/repos/{ORG}/onepeek-docs",
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    try:
        with (opener or urllib.request.urlopen)(req, timeout=30) as r:  # looked up at call time, like compare_status
            branch = json.load(r).get("default_branch")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"cannot read the default branch of onepeek-docs: HTTP {e.code}")
    except (OSError, ValueError, AttributeError) as e:  # URLError, a timeout, a body that is not a JSON object
        raise SystemExit(f"cannot read the default branch of onepeek-docs: {type(e).__name__}: {str(e)[:200]}")
    if not isinstance(branch, str) or not BRANCH.fullmatch(branch) or ".." in branch:
        raise SystemExit(f"cannot read the default branch of onepeek-docs: {branch!r} is not a branch name")
    return branch


def compare_status(sha: str, token: str, trunk=None, opener=None) -> str:
    """GitHub's compare status of docs' trunk (its default branch, `dev`) against the pinned commit:
    `ahead` or `identical` means the commit is an ancestor of the trunk."""
    branch = (trunk or docs_trunk)(token)
    req = urllib.request.Request(
        f"{_api()}/repos/{ORG}/onepeek-docs/compare/{sha}...{branch}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    try:
        with (opener or urllib.request.urlopen)(req, timeout=30) as r:
            return json.load(r)["status"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return "missing"
        raise SystemExit(f"cannot compare {sha[:7]} with onepeek-docs {branch}: HTTP {e.code}")


def docs_problems(repo: pathlib.Path, token: str, status=None, trunk=None) -> list[str]:
    """The docs ancestor rule. The trunk is read once (`trunk`, default docs_trunk, looked up at call time) and named in
    every message, so a failure never tells people to move the pin to a branch the check did not read."""
    has, sha = docs_pin(repo)
    if not has:
        return []
    if not sha:
        return ["docs-version.txt holds no onepeek-docs@<40-character sha> line"]
    if not token:
        return ["no token to read onepeek-docs (pass the agents' App secrets, playbook §8a S6)"]
    branch = (trunk or docs_trunk)(token)  # SystemExit names an unreadable answer
    st = (status or compare_status)(sha, token, trunk=lambda _token: branch)
    if st in ("ahead", "identical"):
        return []
    if st == "missing":
        return [f"docs-version.txt pins {sha[:7]}, which onepeek-docs does not have"]
    return [f"docs-version.txt pins {sha[:7]}, which is not an ancestor of onepeek-docs {branch} ({st}); "
            f"merge the docs pull request first, or move the pin to a commit on {branch}"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("repo")
    a = ap.parse_args(argv)
    repo = pathlib.Path(a.repo)
    found = pins(repo)
    problems = pin_problems(found, remote_tags()) if found else []
    if found and not problems:
        print(f"OK  {len(found)} {ORG}/.github pin(s), all at {found[0][3][:7]}")
    elif not found:
        print(f"OK  no {ORG}/.github pins")
    for p in problems:
        print(f"::error::{p}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
