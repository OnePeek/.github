"""Merge one change set in the fixed order, waiting only on required checks (onepeek-docs development-cycle §2a).

    python3 .github/workspace/ship_change_set.py onepeek-docs#116 .github#22 onepeek-infra#76 backend-service#59
    python3 .github/workspace/ship_change_set.py ... --go     # merge; without --go it only prints the plan and checks

Order: docs (merge commit into main) -> .github (squash into main, then wait for its release tag) -> infra ->
design system -> backend -> web -> the other repos (squash into dev). After .github merges, every later pull
request's `OnePeek/.github` workflow pins move to the new tag in one commit, so callers never run on a stale
or unmerged workflow. A required check that failed before an earlier repo in the set merged is re-run once
(it may have needed that repo); a second failure stops the run. Nothing is merged past a red required check.

Run it from the workspace root (the folder holding the repos side by side). Needs `gh` signed in.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import time

ORG = "OnePeek"
ROOT = pathlib.Path.cwd()  # the workspace root: the folder holding the repos side by side
ORDER = ["onepeek-docs", ".github", "onepeek-infra", "onepeek-design-system", "backend-service", "onepeek-web"]
BASE = {"onepeek-docs": "main", ".github": "main"}  # every other repo merges into dev
PIN = re.compile(r"(OnePeek/\.github/\.github/workflows/[\w.-]+\.yml)@[0-9a-f]{40}(?:[ \t]*#[^\n]*)?")
CHECK_TIMEOUT_S = 45 * 60
TAG_TIMEOUT_S = 10 * 60
POLL_S = 20


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def git(cwd: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


def say(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def rank(repo: str) -> int:
    return ORDER.index(repo) if repo in ORDER else len(ORDER)


def parse(item: str) -> tuple[str, int]:
    m = re.fullmatch(r"([\w.-]+)#(\d+)", item)
    if not m:
        raise SystemExit(f"expected <repo>#<number>, got {item!r}")
    return m.group(1), int(m.group(2))


def pr_info(repo: str, n: int) -> dict:
    return json.loads(gh("pr", "view", str(n), "-R", f"{ORG}/{repo}", "--json",
                         "state,baseRefName,headRefName,headRefOid,mergeCommit,isDraft,mergeable"))


def checks(repo: str, n: int) -> list[dict]:
    """Required checks; if the branch has no required checks, every check except AI review."""
    def read(required: bool) -> list[dict]:
        args = ["pr", "checks", str(n), "-R", f"{ORG}/{repo}", "--json", "name,bucket,link"]
        r = subprocess.run(["gh", *args, *(["--required"] if required else [])], capture_output=True, text=True, check=False)
        return json.loads(r.stdout) if r.stdout.strip().startswith("[") else []
    got = read(True)
    return got or [c for c in read(False) if c["name"] != "CodeRabbit"]


def wait_green(repo: str, n: int, may_rerun: bool) -> None:
    deadline, reran = time.time() + CHECK_TIMEOUT_S, False
    while time.time() < deadline:
        cs = checks(repo, n)
        pending = [c["name"] for c in cs if c["bucket"] == "pending"]
        failed = [c for c in cs if c["bucket"] == "fail" or c["bucket"] == "cancel"]
        if cs and not pending and not failed:
            say(f"{repo}#{n}: required checks green ({len(cs)})")
            return
        if failed and not pending:
            if may_rerun and not reran:
                runs = {m.group(1) for c in failed if (m := re.search(r"/runs/(\d+)", c.get("link") or ""))}
                say(f"{repo}#{n}: re-running {len(runs)} failed run(s) once, now that earlier repos merged")
                for run in runs:
                    gh("run", "rerun", run, "-R", f"{ORG}/{repo}", "--failed")
                reran = True
                time.sleep(POLL_S)
                continue
            raise SystemExit(f"STOP {repo}#{n}: failed {[c['name'] for c in failed]}; nothing after it is merged")
        time.sleep(POLL_S)
    raise SystemExit(f"STOP {repo}#{n}: checks still pending after {CHECK_TIMEOUT_S // 60} min")


def wait_tag(sha: str) -> tuple[str, str]:
    deadline = time.time() + TAG_TIMEOUT_S
    while time.time() < deadline:
        tags = json.loads(gh("api", f"repos/{ORG}/.github/tags", "--jq", "[.[]|{name,sha:.commit.sha}]"))
        for t in tags:
            if t["sha"] == sha:
                return t["name"], sha
        time.sleep(POLL_S)
    raise SystemExit(f"STOP: no tag on .github {sha[:7]} after {TAG_TIMEOUT_S // 60} min (tag-main workflow?)")


def bump_pins(repo: str, branch: str, tag: str, sha: str) -> bool:
    """Move every OnePeek/.github workflow pin on the branch to the tag, in one ordinary commit (hooks run)."""
    src = ROOT / repo
    git(src, "fetch", "-q", "origin", branch)
    with tempfile.TemporaryDirectory(prefix=f"ship-{repo}-") as tmp:
        wt = pathlib.Path(tmp) / "wt"
        git(src, "worktree", "add", "-q", "--detach", str(wt), f"origin/{branch}")
        try:
            changed = []
            for f in sorted((wt / ".github" / "workflows").glob("*.y*ml")):
                old = f.read_text()
                new = PIN.sub(lambda m: f"{m.group(1)}@{sha} # {tag}", old)
                if new != old:
                    f.write_text(new)
                    changed.append(str(f.relative_to(wt)))
            if not changed:
                return False
            git(wt, "add", "--", *changed)
            git(wt, "commit", "-q", "-m", f"chore: pin OnePeek/.github workflows to {tag} (ship_change_set)")
            git(wt, "push", "-q", "origin", f"HEAD:{branch}")
            say(f"{repo}: pins -> {tag} on {branch} ({len(changed)} file(s))")
            return True
        finally:
            git(src, "worktree", "remove", "--force", str(wt))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("prs", nargs="+", help="<repo>#<number>, any order")
    ap.add_argument("--go", action="store_true", help="merge (default: print the plan and the up-front checks)")
    a = ap.parse_args(argv)

    prs = sorted((parse(p) for p in a.prs), key=lambda p: rank(p[0]))
    if any(r == ".github" for r, _ in prs) and not all((ROOT / r / ".git").exists() for r, _ in prs):
        raise SystemExit(f"run from the workspace root (the folder holding the repos); {ROOT} is not it")
    info = {p: pr_info(*p) for p in prs}
    problems = []
    for (repo, n), i in info.items():
        want = BASE.get(repo, "dev")
        if i["state"] != "OPEN":
            problems.append(f"{repo}#{n} is {i['state']}")
        if i["baseRefName"] != want:
            problems.append(f"{repo}#{n} targets {i['baseRefName']}, not {want}")
        if i["isDraft"]:
            problems.append(f"{repo}#{n} is a draft")
    docs = [p for p in prs if p[0] == "onepeek-docs"]
    if len(docs) > 1:
        problems.append("a change set has one docs pull request")
    if docs:  # every code pin must point at the docs pull request's last commit (§2a step 2)
        head = info[docs[0]]["headRefOid"]
        for repo, n in prs:
            if repo in ("onepeek-docs", ".github"):
                continue
            pin = gh("api", f"repos/{ORG}/{repo}/contents/docs-version.txt?ref={info[(repo, n)]['headRefName']}",
                     "-H", "Accept: application/vnd.github.raw").strip()
            if not pin.endswith(head):
                problems.append(f"{repo}#{n} docs-version.txt is {pin.split('@')[-1][:7]}, not docs head {head[:7]}")

    say("plan: " + " -> ".join(f"{r}#{n} ({'merge' if r == 'onepeek-docs' else 'squash'} into {BASE.get(r, 'dev')})"
                               for r, n in prs))
    for p in problems:
        say(f"problem: {p}")
    if problems:
        return 1
    if not a.go:
        say("dry run: add --go to merge")
        return 0

    merged_any, tag = False, None
    for repo, n in prs:
        if tag and repo not in ("onepeek-docs", ".github"):
            bump_pins(repo, info[(repo, n)]["headRefName"], *tag)
            time.sleep(POLL_S)  # let the new push's checks register before reading them
        wait_green(repo, n, may_rerun=merged_any)
        method = "--merge" if repo == "onepeek-docs" else "--squash"
        gh("pr", "merge", str(n), "-R", f"{ORG}/{repo}", method, "--delete-branch")
        merged = pr_info(repo, n)
        if merged["state"] != "MERGED":
            raise SystemExit(f"STOP {repo}#{n}: merge did not complete ({merged['state']})")
        say(f"{repo}#{n}: merged {merged['mergeCommit']['oid'][:7]}")
        merged_any = True
        if repo == ".github":
            tag = wait_tag(merged["mergeCommit"]["oid"])
            say(f".github: tagged {tag[0]}")
    say("change set merged. Follow-ups: republish architecture pages after a docs merge; status.yaml.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
