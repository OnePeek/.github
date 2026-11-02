"""Merge one change set in the fixed order, waiting only on required checks (onepeek-docs development-cycle §2a).

    python3 .github/workspace/ship_change_set.py onepeek-docs#116 .github#22 onepeek-infra#76 backend-service#59
    python3 .github/workspace/ship_change_set.py ... --go     # merge; without --go it only prints the plan and checks

Conflicts are checked first: a pull request that conflicts with its base stops the run before any wait, and
every pull request still to merge is checked again after each merge.

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


def run(cmd: list[str]) -> str:
    """Run a command; on failure stop with its own error text, not a bare traceback."""
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise SystemExit(f"STOP: `{' '.join(cmd[:4])} ...` failed: {(r.stderr or r.stdout).strip()[:500]}")
    return r.stdout


def gh(*args: str) -> str:
    return run(["gh", *args])


def git(cwd: pathlib.Path, *args: str) -> str:
    return run(["git", "-C", str(cwd), *args])


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


def read_checks(repo: str, n: int, required: bool) -> list[dict]:
    """`gh pr checks` exits non-zero while checks are pending or failed but still prints them; an empty result is
    only taken from its own "no ... checks reported" message, and any other failure stops the run."""
    cmd = ["gh", "pr", "checks", str(n), "-R", f"{ORG}/{repo}", "--json", "name,bucket,link"]
    r = subprocess.run([*cmd, *(["--required"] if required else [])], capture_output=True, text=True, check=False)
    if r.stdout.strip().startswith("["):
        return json.loads(r.stdout)
    if re.search(r"no (required )?checks reported", r.stderr + r.stdout):
        return []
    raise SystemExit(f"STOP {repo}#{n}: could not read checks: {(r.stderr or r.stdout).strip()[:300]}")


def checks(repo: str, n: int) -> list[dict]:
    """The branch's required checks. A branch with none required (no protection yet) waits on every check except
    the AI review instead, so nothing merges unchecked; the skill documents this."""
    return read_checks(repo, n, True) or [c for c in read_checks(repo, n, False) if c["name"] != "CodeRabbit"]


def mergeable(repo: str, n: int) -> str:
    """GitHub computes this lazily: UNKNOWN right after a push or a merge into the base, so ask until it settles."""
    for _ in range(15):
        state = pr_info(repo, n)["mergeable"]
        if state != "UNKNOWN":
            return state
        time.sleep(4)
    return "UNKNOWN"


def no_conflicts(prs: list[tuple[str, int]]) -> None:
    """Stop before any wait if a pull request still to merge conflicts with its base (often after an earlier merge),
    or if GitHub cannot say yet: an unknown state is not taken as clean."""
    states = {f"{r}#{n}": mergeable(r, n) for r, n in prs}
    bad = [p for p, st in states.items() if st != "MERGEABLE"]
    if bad:
        raise SystemExit(f"STOP: not mergeable {[(p, states[p]) for p in bad]}; resolve conflicts, push, run again")


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


def wait_head(repo: str, n: int, sha: str) -> None:
    """After a push: the pull request's head is the pushed commit and its checks have registered on it."""
    deadline = time.time() + 5 * 60
    while time.time() < deadline:
        if pr_info(repo, n)["headRefOid"] == sha and read_checks(repo, n, False):
            return
        time.sleep(5)
    raise SystemExit(f"STOP {repo}#{n}: head is not {sha[:7]} with checks after 5 min")


def wait_tag(sha: str) -> tuple[str, str]:
    deadline = time.time() + TAG_TIMEOUT_S
    while time.time() < deadline:
        query = '.[]|"\\(.name) \\(.commit.sha)"'
        lines = gh("api", "--paginate", f"repos/{ORG}/.github/tags?per_page=100", "--jq", query)
        for line in lines.splitlines():
            name, _, tag_sha = line.partition(" ")
            if tag_sha == sha:
                return name, sha
        time.sleep(POLL_S)
    raise SystemExit(f"STOP: no tag on .github {sha[:7]} after {TAG_TIMEOUT_S // 60} min (tag-main workflow?)")


def bump_pins(repo: str, branch: str, tag: str, sha: str) -> str | None:
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
                return None
            git(wt, "add", "--", *changed)
            git(wt, "commit", "-q", "-m", f"chore: pin OnePeek/.github workflows to {tag} (ship_change_set)")
            git(wt, "push", "-q", "origin", f"HEAD:{branch}")
            say(f"{repo}: pins -> {tag} on {branch} ({len(changed)} file(s))")
            return git(wt, "rev-parse", "HEAD").strip()
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
        state = mergeable(repo, n) if i["mergeable"] == "UNKNOWN" else i["mergeable"]
        if state != "MERGEABLE":
            problems.append(f"{repo}#{n} is {state}: resolve conflicts before any check is waited on")
    docs = [p for p in prs if p[0] == "onepeek-docs"]
    if len(docs) > 1:
        problems.append("a change set has one docs pull request")
    if docs:  # every code pin must point at the docs pull request's last commit (§2a step 2)
        head = info[docs[0]]["headRefOid"]
        for repo, n in prs:
            if repo in ("onepeek-docs", ".github"):
                continue
            url = f"repos/{ORG}/{repo}/contents/docs-version.txt?ref={info[(repo, n)]['headRefName']}"
            r = subprocess.run(["gh", "api", url, "-H", "Accept: application/vnd.github.raw"],
                               capture_output=True, text=True, check=False)
            pin = r.stdout.strip()
            if r.returncode != 0:
                problems.append(f"{repo}#{n} has no readable docs-version.txt")
            elif not pin.endswith(head):
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
    for k, (repo, n) in enumerate(prs):
        no_conflicts(prs[k:])
        if tag and repo not in ("onepeek-docs", ".github"):
            pushed = bump_pins(repo, info[(repo, n)]["headRefName"], *tag)
            if pushed:
                wait_head(repo, n, pushed)
                no_conflicts([(repo, n)])
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
