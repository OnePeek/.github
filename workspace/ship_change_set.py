"""Merge one change set in the fixed order, waiting only on required checks (onepeek-docs development-cycle §2a).

    python3 .github/workspace/ship_change_set.py onepeek-docs#116 .github#22 onepeek-infra#76 backend-service#59
    python3 .github/workspace/ship_change_set.py ... --go     # merge; without --go it only prints the plan and checks
    python3 .github/workspace/ship_change_set.py ... --order backend-service,onepeek-infra   # expand first (§8a P1)

Conflicts are checked first: a pull request that conflicts with its base stops the run before any wait, and
every pull request still to merge is checked again after each merge.

Order: docs (merge commit into main) -> .github (squash into main, then wait for its release tag) -> infra ->
design system -> backend -> web -> the other repos (squash into dev). After .github merges, every later pull
request's `OnePeek/.github` workflow pins move to the new tag in one commit, so callers never run on a stale
or unmerged workflow; every other caller repo then gets a pin-move pull request to the same tag (foundation-playbook
§8a P2), with a note when it was more than one minor version behind. `--order repoA,repoB,...` replaces the fixed
order (§8a P1, development-cycle §2a item 7: the consumer that validates a contract strictly merges before the
producer); it names every repo in the set once. A pull request whose body says `Depends on: OnePeek/<repo>#<n>` (§8a P1,
lesson L26) merges after that one (the order is otherwise kept), a listed pull request outside the set must already be
merged, and after the dependency merges the dependent's failed checks (its `depends-on` job) are re-run. After each merge, the local worktrees of that branch are removed
when clean (§8a P6). A required check that failed before an earlier repo in the set merged is re-run once
(it may have needed that repo); a second failure stops the run. Nothing is merged past a red required check.

Run it from the workspace root (the folder holding the repos side by side). Needs `gh` signed in.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import time

ORG = "OnePeek"
ROOT = pathlib.Path.cwd()
_DEPS_SPEC = importlib.util.spec_from_file_location(
    "check_depends_on", pathlib.Path(__file__).resolve().parents[1] / "scripts" / "check_depends_on.py")
depends_on = importlib.util.module_from_spec(_DEPS_SPEC)
_DEPS_SPEC.loader.exec_module(depends_on)  # the workspace root: the folder holding the repos side by side
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


def ordered(prs: list[tuple[str, int]], order: str) -> list[tuple[str, int]]:
    """The merge order: the fixed one, or `--order` (every repo in the set, each once, nothing else)."""
    if not order:
        return sorted(prs, key=lambda p: rank(p[0]))
    names = [r.strip() for r in order.split(",") if r.strip()]
    repos = [r for r, _ in prs]
    if len(set(names)) != len(names) or sorted(names) != sorted(repos):
        raise SystemExit(f"--order must name each repo of the set once: got {names}, the set has {sorted(repos)}")
    return sorted(prs, key=lambda p: names.index(p[0]))


def parse(item: str) -> tuple[str, int]:
    m = re.fullmatch(r"([\w.-]+)#(\d+)", item)
    if not m:
        raise SystemExit(f"expected <repo>#<number>, got {item!r}")
    return m.group(1), int(m.group(2))


def pr_info(repo: str, n: int) -> dict:
    return json.loads(gh("pr", "view", str(n), "-R", f"{ORG}/{repo}", "--json",
                         "state,baseRefName,headRefName,headRefOid,mergeCommit,isDraft,mergeable,body"))


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
            changed, _ = rewrite_pins(wt, tag, sha)
            if not changed:
                return None
            git(wt, "add", "--", *changed)
            git(wt, "commit", "-q", "-m", f"chore: pin OnePeek/.github workflows to {tag} (ship_change_set)")
            git(wt, "push", "-q", "origin", f"HEAD:{branch}")
            say(f"{repo}: pins -> {tag} on {branch} ({len(changed)} file(s))")
            return git(wt, "rev-parse", "HEAD").strip()
        finally:
            git(src, "worktree", "remove", "--force", str(wt))


def with_dependencies(prs: list[tuple[str, int]], deps: dict) -> list[tuple[str, int]]:
    """Each pull request after the ones in the set it declares (`Depends on:`); otherwise the given order is kept."""
    left, out = list(prs), []
    while left:
        ready = next((p for p in left if all(d in out or d not in prs for d in deps.get(p, []))), None)
        if ready is None:
            raise SystemExit(f"STOP: the Depends on lines form a cycle among {[f'{r}#{n}' for r, n in left]}")
        out.append(ready)
        left.remove(ready)
    return out


def rerun_failed(repo: str, n: int) -> None:
    """Re-run the failed runs of a pull request (its depends-on job failed while the dependency was unmerged)."""
    failed = [c for c in read_checks(repo, n, False) if c["bucket"] in ("fail", "cancel")]
    runs = {m.group(1) for c in failed if (m := re.search(r"/runs/(\d+)", c.get("link") or ""))}
    for run_id in runs:
        gh("run", "rerun", run_id, "-R", f"{ORG}/{repo}", "--failed")
    if runs:
        say(f"{repo}#{n}: re-ran {len(runs)} failed run(s) now that its dependency merged")


def minor_behind(old_tags: set[str], new: str) -> bool:
    """True when any old pin is more than one minor version behind the new tag (or a major, or untagged)."""
    want = re.fullmatch(r"v(\d+)\.(\d+)\.\d+", new)
    if not want:
        return False
    for tag in old_tags:
        m = re.fullmatch(r"v(\d+)\.(\d+)\.\d+", tag)
        if not m or int(m.group(1)) != int(want.group(1)) or int(want.group(2)) - int(m.group(2)) > 1:
            return True
    return False


def rewrite_pins(wt: pathlib.Path, tag: str, sha: str) -> tuple[list[str], set[str]]:
    """Move every pin under wt/.github/workflows to tag@sha; returns (changed files, the tags they were on)."""
    changed, old = [], set()
    for f in sorted((wt / ".github" / "workflows").glob("*.y*ml")):
        text = f.read_text()
        for m in PIN.finditer(text):
            c = re.search(r"#\s*(\S+)", m.group(0))
            old.add(c.group(1) if c else "untagged")
        new = PIN.sub(lambda m: f"{m.group(1)}@{sha} # {tag}", text)
        if new != text:
            f.write_text(new)
            changed.append(str(f.relative_to(wt)))
    return changed, old


def callers() -> list[str]:
    """Every repo checked out beside .github whose workflows pin OnePeek/.github (§8a P2), read at its base branch."""
    out = []
    for d in sorted(p for p in ROOT.iterdir() if (p / ".git").exists() and p.name != ".github"):
        base = BASE.get(d.name, "dev")
        subprocess.run(["git", "-C", str(d), "fetch", "-q", "origin", base], capture_output=True, check=False)
        r = subprocess.run(["git", "-C", str(d), "grep", "-q", "-E", "OnePeek/[.]github/[.]github/workflows/[A-Za-z0-9_.-]+[.]yml@[0-9a-f]{40}", f"origin/{base}", "--", ".github/workflows"],
                           capture_output=True, check=False)
        if r.returncode == 0:
            out.append(d.name)
    return out


def open_pin_move(repo: str, tag: str, sha: str) -> str | None:
    """A pin-move pull request in a caller outside the set: every pin to tag@sha on a new branch off its base."""
    src, base = ROOT / repo, BASE.get(repo, "dev")
    branch = f"chore/pin-github-{tag}"
    git(src, "fetch", "-q", "origin", base)
    with tempfile.TemporaryDirectory(prefix=f"pins-{repo}-") as tmp:
        wt = pathlib.Path(tmp) / "wt"
        git(src, "worktree", "add", "-q", "--detach", str(wt), f"origin/{base}")
        try:
            changed, old = rewrite_pins(wt, tag, sha)
            if not changed:
                return None
            git(wt, "switch", "-q", "-c", branch)
            git(wt, "add", "--", *changed)
            git(wt, "commit", "-q", "-m", f"chore: pin OnePeek/.github workflows to {tag} (ship_change_set)")
            git(wt, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        finally:
            git(src, "worktree", "remove", "--force", str(wt))
            subprocess.run(["git", "-C", str(src), "branch", "-D", branch], capture_output=True, check=False)
    body = (f"Moves every `OnePeek/.github` workflow pin to `{tag}` (`{sha}`), opened by `ship_change_set.py` after "
            f"`.github` was tagged (onepeek-docs foundation-playbook §8a P2). Pins before: {', '.join(sorted(old))}.")
    if minor_behind(old, tag):
        body += ("\n\n**More than one minor version behind:** the shared workflows' inputs or secrets may have changed "
                 "since; check each call against the workflow at the new tag and fix it by hand in this pull request.")
    url = gh("pr", "create", "-R", f"{ORG}/{repo}", "--base", base, "--head", branch,
             "--title", f"chore: pin OnePeek/.github workflows to {tag}", "--body", body).strip()
    say(f"{repo}: pin-move pull request {url}")
    return url


def worktrees_of(src: pathlib.Path, branch: str) -> list[pathlib.Path]:
    """Linked worktrees (not the main checkout) that have `branch` checked out, from `git worktree list`."""
    r = subprocess.run(["git", "-C", str(src), "worktree", "list", "--porcelain"], capture_output=True, text=True, check=False)
    out, path, first = [], None, True
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            path = pathlib.Path(line[len("worktree "):])
        elif line == f"branch refs/heads/{branch}" and path is not None and not first:
            out.append(path)
        elif line == "":
            first = False
    return out


def remove_worktrees(repo: str, branch: str) -> None:
    """After a merge, remove the local worktrees of its branch that are clean; a dirty one is kept and named (§8a P6)."""
    src = ROOT / repo
    if not (src / ".git").exists():
        return
    for wt in worktrees_of(src, branch):
        dirty = subprocess.run(["git", "-C", str(wt), "status", "--porcelain"], capture_output=True, text=True, check=False)
        if dirty.returncode or dirty.stdout.strip():
            say(f"{repo}: kept worktree {wt} (uncommitted changes)")
            continue
        r = subprocess.run(["git", "-C", str(src), "worktree", "remove", str(wt)], capture_output=True, text=True, check=False)
        say(f"{repo}: {'removed' if r.returncode == 0 else 'could not remove'} worktree {wt}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("prs", nargs="+", help="<repo>#<number>, any order")
    ap.add_argument("--go", action="store_true", help="merge (default: print the plan and the up-front checks)")
    ap.add_argument("--order", default="", help="repoA,repoB,...: merge in this order instead of the fixed one (§8a P1)")
    a = ap.parse_args(argv)

    prs = ordered([parse(p) for p in a.prs], a.order)
    if any(r == ".github" for r, _ in prs) and not all((ROOT / r / ".git").exists() for r, _ in prs):
        raise SystemExit(f"run from the workspace root (the folder holding the repos); {ROOT} is not it")
    info = {p: pr_info(*p) for p in prs}
    deps = {p: depends_on.parse(i.get("body")) for p, i in info.items()}
    prs = with_dependencies(prs, deps)
    problems = []
    for p, ds in deps.items():
        for d in ds:
            if d not in info and pr_info(*d)["state"] != "MERGED":
                problems.append(f"{p[0]}#{p[1]} depends on {d[0]}#{d[1]}, which is neither in the set nor merged")
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
        remove_worktrees(repo, info[(repo, n)]["headRefName"])
        for later in prs[k + 1:]:
            if (repo, n) in deps.get(later, []):
                rerun_failed(*later)
        merged_any = True
        if repo == ".github":
            tag = wait_tag(merged["mergeCommit"]["oid"])
            say(f".github: tagged {tag[0]}")
    if tag:
        in_set = {r for r, _ in prs}
        for repo in callers():
            if repo not in in_set:
                open_pin_move(repo, *tag)
    say("change set merged. Follow-ups: republish architecture pages after a docs merge; status.yaml.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
