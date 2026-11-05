#!/usr/bin/env python3
"""The gate of each contract step of the move off locks/release.json (onepeek-docs architecture/lld/infra/
foundation-playbook.md §5.4a "Moving from release.json", steps 2 and 3).

  check_lock_pins.py --min-tag v1.16.0 [--repos onepeek-infra,backend-service,onepeek-web,mcp-athena]

Reads every caller of release-ecs.yml, operate.yml, release-lock.yml and cancel-release.yml on `dev`, `release` and
every `hotfix/*` branch of every releasing repo, and refuses unless each pins (by commit SHA) a OnePeek/.github tag at
least `--min-tag`. Run by the step-2 (and step-3) tag's pull request and by ship_change_set.py before it merges it.
Anything it cannot read refuses: a pin it cannot place is never assumed new enough.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
from typing import Callable

ORG = "OnePeek"
REPOS = ("onepeek-infra", "backend-service", "onepeek-web", "mcp-athena")
WORKFLOWS = ("release-ecs.yml", "operate.yml", "release-lock.yml", "cancel-release.yml")
USES = re.compile(r"(?i)OnePeek/\.github/\.github/workflows/(?P<wf>[\w.-]+\.ya?ml)@(?P<ref>[^\s#'\"]+)")
TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")

Api = Callable[[str], object]  # a GitHub API path -> parsed JSON; raises LookupError on 404 and RuntimeError otherwise


def gh_api(path: str) -> object:
    r = subprocess.run(["gh", "api", "--paginate", path], capture_output=True, text=True)
    if r.returncode != 0:
        if "Not Found" in r.stderr or "(HTTP 404)" in r.stderr:
            raise LookupError(path)
        raise RuntimeError(f"{path}: {r.stderr.strip()[:300]}")
    text = r.stdout.replace("][", ",")  # --paginate concatenates JSON arrays
    return json.loads(text or "null")


def version(tag: str) -> tuple[int, int, int] | None:
    m = TAG.match(tag)
    return tuple(int(x) for x in m.groups()) if m else None  # type: ignore[return-value]


def tag_commits(api: Api) -> dict[str, str]:
    """OnePeek/.github's tag commit SHA -> its highest v* tag."""
    out: dict[str, str] = {}
    for t in api(f"repos/{ORG}/.github/tags?per_page=100") or []:
        name, sha = t["name"], t["commit"]["sha"]
        if version(name) and (sha not in out or version(name) > version(out[sha])):
            out[sha] = name
    return out


def problems(min_tag: str, repos: tuple[str, ...] = REPOS, api: Api = gh_api) -> list[str]:
    least = version(min_tag)
    if least is None:
        return [f"--min-tag must be v<major>.<minor>.<patch>, not {min_tag!r}"]
    out: list[str] = []
    try:
        tags = tag_commits(api)
    except (LookupError, RuntimeError) as e:
        return [f"cannot read OnePeek/.github's tags: {e}"]
    for repo in repos:
        try:
            names = [b["name"] for b in api(f"repos/{ORG}/{repo}/branches?per_page=100") or []]
        except (LookupError, RuntimeError) as e:
            out.append(f"{repo}: cannot list branches: {e}")
            continue
        for branch in [b for b in names if b in ("dev", "release") or b.startswith("hotfix/")]:
            try:
                files = api(f"repos/{ORG}/{repo}/contents/.github/workflows?ref={branch}") or []
            except LookupError:
                continue  # no workflows on that branch
            except RuntimeError as e:
                out.append(f"{repo}@{branch}: cannot list workflows: {e}")
                continue
            for f in files:
                if not str(f.get("name", "")).endswith((".yml", ".yaml")):
                    continue
                try:
                    blob = api(f"repos/{ORG}/{repo}/contents/{f['path']}?ref={branch}")
                    text = base64.b64decode(blob["content"]).decode("utf-8")  # type: ignore[index]
                except (LookupError, RuntimeError, KeyError, ValueError) as e:
                    out.append(f"{repo}@{branch}:{f.get('path')}: cannot read: {e}")
                    continue
                for m in USES.finditer(text):
                    if m.group("wf") not in WORKFLOWS:
                        continue
                    where = f"{repo}@{branch}:{f['path']} {m.group('wf')}"
                    tag = tags.get(m.group("ref"))
                    if tag is None:
                        out.append(f"{where} pins {m.group('ref')[:12]}, which is not a OnePeek/.github tag commit")
                    elif version(tag) < least:
                        out.append(f"{where} pins {tag}, below {min_tag}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--min-tag", required=True)
    ap.add_argument("--repos", default=",".join(REPOS))
    a = ap.parse_args(argv)
    found = problems(a.min_tag, tuple(r for r in a.repos.split(",") if r))
    for p in found:
        print("REFUSED " + p)
    if not found:
        print(f"lock pins: every caller pins at least {a.min_tag}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
