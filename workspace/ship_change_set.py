"""Merge one change set in the fixed order, waiting only on required checks (onepeek-docs development-cycle §2a).

    python3 .github/workspace/ship_change_set.py onepeek-docs#116 .github#22 onepeek-infra#76 backend-service#59
    python3 .github/workspace/ship_change_set.py --refresh backend-service#59   # only refresh a conflicting docs pin
    python3 .github/workspace/ship_change_set.py ... --go     # merge; without --go it only prints the plan and checks
    python3 .github/workspace/ship_change_set.py ... --order backend-service,onepeek-infra   # expand first (§8a P1)

A code pull request that conflicts with its base only in `docs-version.txt` (every change set edits that one line, so
each merge conflicts the others) is refreshed automatically: the base is merged into its branch in a temporary worktree,
the pin is set to the change set's docs pull request head, and the merge is pushed ("Merge <base>; docs-version to
<docs>#<n> head"); any other conflicting file still stops the run. `--refresh <repo>#<n>` does only that, for one pull
request (its docs pull request is the open one containing its pin).

The docs pin is also a wait, not a failure, in the pull request's own `depends-on` check, but here it is strict: a code
pull request whose pin is neither a commit of the set's docs pull request nor on docs main stops the run.

Conflicts are checked first: a pull request that conflicts with its base stops the run before any wait, and
every pull request still to merge is checked again after each merge.

Order: docs (merge commit into main) -> .github (squash into main, then wait for its release tag) -> infra ->
design system -> backend -> web -> the other repos (squash into dev). After .github merges, every later pull
request's `OnePeek/.github` workflow pins move to the new tag in one commit, so callers never run on a stale
or unmerged workflow; every caller repo whose pins were not moved inside the set (one outside it, or one merged
before .github, such as onepeek-docs) then gets a pin-move pull request to the same tag (foundation-playbook §8a P2),
with a note when it was more than one minor version behind. Pin moves are safe to re-run: an existing branch or pull
request for the tag is updated (never a second one), no local branch is created or deleted, and a failure in one caller
is reported at the end without stopping the others. `--order repoA,repoB,...` replaces the fixed
order (§8a P1, development-cycle §2a item 7: the consumer that validates a contract strictly merges before the
producer); it names every repo in the set once. A pull request whose body says `Depends on: OnePeek/<repo>#<n>` (§8a P1,
lesson L26) merges after that one (the order is otherwise kept), a listed pull request outside the set must already be
merged, and after the dependency merges the dependent's failed checks (its `depends-on` job) are re-run. The docs pin
is an implicit dependency (lesson L27): a code pull request's `docs-version.txt` may be any commit of the set's docs
pull request, not only its head; after the docs pull request merges, its checks are re-run like a declared dependent's,
and once they are green its pin moves to the docs merge commit in one commit pushed to its branch, and it is merged
without waiting for a new CI run, because only that one line changed since the green run (checked; any other change
waits for CI as before). Neither
`--order` nor `Depends on:` ever moves onepeek-docs off first place. After each merge, the local worktrees of that branch are removed
when clean (§8a P6). A required check that failed before an earlier repo in the set merged is re-run once
(it may have needed that repo); a second failure stops the run. Nothing is merged past a red required check, except
the warn-only checks ship_watch.yaml lists in `warn_checks` (decision 173: the sandbox workflow's, until `warn_until`),
which are not waited on; deleting the list makes them block again.

Run it from the workspace root (the folder holding the repos side by side). Needs `gh` signed in.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time

ORG = "OnePeek"
ROOT = pathlib.Path.cwd()  # the workspace root: the folder holding the repos side by side


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


depends_on = _load("check_depends_on")
check_pins = _load("check_pins")
_packet_spec = importlib.util.spec_from_file_location("packet", pathlib.Path(__file__).resolve().parent / "packet.py")
packet = importlib.util.module_from_spec(_packet_spec)
_packet_spec.loader.exec_module(packet)
ORDER = ["onepeek-docs", ".github", "onepeek-infra", "onepeek-design-system", "backend-service", "onepeek-web"]
BASE = {"onepeek-docs": "main", ".github": "main", "marketing-website": "main"}  # every other repo merges into dev
PIN = check_pins.USES  # every `uses:` naming OnePeek/.github, any letter case, any ref (§8a P2)
VERSION_COMMENT = re.compile(r"[ \t]+#[ \t]*(v\d+\.\d+\.\d+)(.*)")
DOCS = "onepeek-docs"
CHECK_TIMEOUT_S = 45 * 60
TAG_TIMEOUT_S = 10 * 60
POLL_S = 20


CHAIN = ["onepeek-infra", "backend-service", "onepeek-web"]  # release order (development-cycle §2c step 4)
RELEASE_BASE = "release"
RELEASE_ENV = "preprod"  # until launch release = preprod (the release branch deploys it)
PROTECTED = {"main", "dev", "release"}


def own_branch_problem(repo: str, n: int, i: dict) -> str | None:
    """Why this tool must not push to the pull request's head branch: it is from a fork, or is the base branch itself."""
    owner = (i.get("headRepositoryOwner") or {}).get("login")
    if i.get("isCrossRepository") or (owner and owner.lower() != ORG.lower()):
        return f"{repo}#{n} is from a fork ({owner or 'another repository'}); this tool never pushes to a fork's branch"
    release = i["baseRefName"] == RELEASE_BASE and i["headRefName"] == "dev"  # dev to release: the head is `dev` by design,
    if not release and (i["headRefName"] == i["baseRefName"] or i["headRefName"] in PROTECTED):  # but nothing is ever pushed to it
        return f"{repo}#{n}'s head branch {i['headRefName']} is a base branch; this tool never pushes to one"
    return None


def refuse_protected(repo: str, branch: str, base: str | None = None) -> None:
    """Defence in depth under every function that pushes to a pull request's branch."""
    if branch in PROTECTED or branch == base:
        raise SystemExit(f"STOP {repo}: refusing to push to {branch}, a base branch")


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
    """The merge order: the fixed one, or `--order` (every repo in the set, each once, nothing else, onepeek-docs
    first when it is in the set)."""
    if not order:
        return sorted(prs, key=lambda p: rank(p[0]))
    names = [r.strip() for r in order.split(",") if r.strip()]
    repos = [r for r, _ in prs]
    if len(set(names)) != len(names) or sorted(names) != sorted(repos):
        raise SystemExit(f"--order must name each repo of the set once: got {names}, the set has {sorted(repos)}")
    if DOCS in names and names[0] != DOCS:
        raise SystemExit(f"--order must keep {DOCS} first (§8a P2): got {names}")
    return sorted(prs, key=lambda p: names.index(p[0]))


def release_lock(env: str, profile: str, aws=subprocess.run) -> str | None:
    """Whether the environment's release lock (s3://onepeek-<env>-deploy/locks/release.json, as release-lock.yml keeps it)
    is held: its LastModified time when it is, None when it is not. Only `head-object` is used, so the lock's token is
    never read. Only a 404 / NotFound means free; no profile, no credentials, AccessDenied or anything else stops the
    run: the lock is never assumed free."""
    bucket = f"onepeek-{env}-deploy"
    cmd = ["aws", "s3api", "head-object", "--bucket", bucket, "--key", "locks/release.json", "--query", "LastModified", "--output", "text"]
    if profile:
        cmd += ["--profile", profile]
    profile_args = ["--profile", profile] if profile else []
    try:
        # The bucket first: a missing or misnamed bucket must stop here, never read as a free lock.
        b = aws(["aws", "s3api", "head-bucket", "--bucket", bucket, *profile_args], capture_output=True, text=True, check=False)
        if b.returncode != 0:
            raise SystemExit(f"STOP: cannot check the release lock of {env} (bucket {bucket}: {((b.stderr or b.stdout) or '').strip()[:300]}); "
                             f"pass --aws-profile for the {env} account")
        r = aws(cmd, capture_output=True, text=True, check=False)
    except OSError as e:
        raise SystemExit(f"STOP: cannot check the release lock of {env} (aws is not runnable: {e}); pass --aws-profile and try again")
    if r.returncode == 0:
        since = (r.stdout or "").strip()
        if not since or since == "None":
            raise SystemExit(f"STOP: cannot check the release lock of {env}: head-object gave no LastModified")
        return since
    err = r.stderr or ""
    if "(404)" in err or "NotFound" in err:
        return None
    raise SystemExit(f"STOP: cannot check the release lock of {env} (s3://{bucket}/locks/release.json): "
                     f"{(err or r.stdout).strip()[:300]}; pass --aws-profile for the {env} account")


def lock_problem(env: str, since: str | None) -> str | None:
    return None if since is None else f"the {env} release lock is held since {since}; wait for it to be released"


def chain_problem(repo: str, release_prs: list[str], read=None) -> str | None:
    """development-cycle §2c step 4: the earlier release of the chain (infra, backend-service, onepeek-web) must have
    succeeded: the latest release.yml run of that repo has `headSha` equal to the repo's current `release` head, is
    completed and is `success`. `read(prev)` -> (latest run dict or None, release head sha); fakeable."""
    if repo not in CHAIN or CHAIN.index(repo) == 0:
        return None
    prev = CHAIN[CHAIN.index(repo) - 1]
    if prev in release_prs:
        return f"{prev} releases in this set: merge it, wait for its release run to succeed, then ship {repo}"
    run_, head = (read or release_run)(prev)
    if run_ is None:
        return f"no release.yml run on {prev}'s release branch: {prev} has not released"
    if run_.get("headSha") != head:
        return f"the latest {prev} release run is for {str(run_.get('headSha'))[:7]}, not the current release head {head[:7]}: it has not released yet"
    if run_.get("status") != "completed" or run_.get("conclusion") != "success":
        return f"the latest {prev} release run is {run_.get('conclusion') or run_.get('status')}, not success"
    return None


def release_run(prev: str) -> tuple[dict | None, str]:
    runs = json.loads(gh("run", "list", "-R", f"{ORG}/{prev}", "--workflow", "release.yml", "--branch", RELEASE_BASE,
                         "--limit", "1", "--json", "status,conclusion,headSha"))
    head = gh("api", f"repos/{ORG}/{prev}/commits/{RELEASE_BASE}", "--jq", ".sha").strip()
    return (runs[0] if runs else None), head


def merge_method(repo: str, base: str) -> str:
    """docs, and every pull request into `release` (dev to release), merge with a merge commit; squash would break the
    release branch's history and release-please. Everything else squashes into dev."""
    return "--merge" if repo == DOCS or base == RELEASE_BASE else "--squash"


def parse(item: str) -> tuple[str, int]:
    m = re.fullmatch(r"([\w.-]+)#(\d+)", item)
    if not m:
        raise SystemExit(f"expected <repo>#<number>, got {item!r}")
    return m.group(1), int(m.group(2))


def pr_info(repo: str, n: int) -> dict:
    return json.loads(gh("pr", "view", str(n), "-R", f"{ORG}/{repo}", "--json",
                         "state,baseRefName,headRefName,headRefOid,mergeCommit,isDraft,mergeable,body,isCrossRepository,headRepositoryOwner,"
                         "author"))


def read_checks(repo: str, n: int, required: bool) -> list[dict]:
    """`gh pr checks` exits non-zero while checks are pending or failed but still prints them; an empty result is
    only taken from its own "no ... checks reported" message, and any other failure stops the run."""
    cmd = ["gh", "pr", "checks", str(n), "-R", f"{ORG}/{repo}", "--json", "name,bucket,link,workflow"]
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


def conflict_state(repo: str, n: int, resolve=None) -> str:
    """The pull request's mergeable state; a CONFLICTING one that `resolve(repo, n)` fixes (it returns the pushed
    commit, or None when it cannot) is waited on and read again."""
    state = mergeable(repo, n)
    if state == "CONFLICTING" and resolve:
        sha = resolve(repo, n)
        if sha:
            wait_head(repo, n, sha)
            state = mergeable(repo, n)
    return state


def no_conflicts(prs: list[tuple[str, int]], resolve=None) -> None:
    """Stop before any wait if a pull request still to merge conflicts with its base (often after an earlier merge),
    or if GitHub cannot say yet: an unknown state is not taken as clean. `resolve` (see conflict_state) refreshes a
    code pull request whose only conflict is docs-version.txt."""
    states = {f"{r}#{n}": conflict_state(r, n, resolve) for r, n in prs}
    bad = [p for p, st in states.items() if st != "MERGEABLE"]
    if bad:
        raise SystemExit(f"STOP: not mergeable {[(p, states[p]) for p in bad]}; resolve conflicts, push, run again")


def refresh_docs_pin(repo: str, branch: str, base: str, docs_label: str, docs_head: str, push: bool = True,
                     source: str | None = None) -> str | None:
    """Merge `base` into the pull request's branch in a temporary worktree and set docs-version.txt to the docs pull
    request's head `docs_head`, committing "Merge <base>; docs-version to <docs_label> head" and pushing it as a
    fast-forward. Returns the pushed commit; "" when nothing needed pushing (or `push` is false and the conflict is
    only the pin); None when another file conflicts (nothing is changed, the caller stops as for any conflict).
    `source`, when given, is the commit to build on instead of the branch head (the push fails if the branch moved)."""
    refuse_protected(repo, branch, base)
    src = ROOT / repo
    git(src, "fetch", "-q", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
        f"+refs/heads/{base}:refs/remotes/origin/{base}")
    with tempfile.TemporaryDirectory(prefix=f"refresh-{repo}-") as tmp:
        wt = pathlib.Path(tmp) / "wt"
        git(src, "worktree", "add", "-q", "--detach", str(wt), source or f"origin/{branch}")
        try:
            r = subprocess.run(["git", "-C", str(wt), "merge", "--no-commit", "--no-ff", f"origin/{base}"],
                               capture_output=True, text=True, check=False)
            unmerged = git(wt, "diff", "--name-only", "--diff-filter=U").split()
            if r.returncode != 0 and not unmerged:
                raise SystemExit(f"STOP {repo}: merging {base} into {branch} failed: {(r.stderr or r.stdout).strip()[:300]}")
            if unmerged and unmerged != ["docs-version.txt"]:
                say(f"{repo}: {branch} also conflicts with {base} in {[u for u in unmerged if u != 'docs-version.txt']}; not refreshing")
                return None
            if not push:
                return ""
            if unmerged:  # docs-version.txt truly conflicts: start from the branch's side
                text = subprocess.run(["git", "-C", str(wt), "show", "HEAD:docs-version.txt"], capture_output=True,
                                      text=True, check=False).stdout
            else:  # merged cleanly: keep the merge result, with only the pin line moved
                text = (wt / "docs-version.txt").read_text(encoding="utf-8") if (wt / "docs-version.txt").exists() else ""
            at = pin_at(text)
            if at is None:
                say(f"{repo}: docs-version.txt at {branch} holds no onepeek-docs@<sha> line; not refreshing")
                return None
            lines = text.splitlines(keepends=True)
            lines[at[0]] = lines[at[0]].replace(f"onepeek-docs@{at[1]}", f"onepeek-docs@{docs_head}", 1)
            (wt / "docs-version.txt").write_text("".join(lines), encoding="utf-8")
            check = pin_at((wt / "docs-version.txt").read_text(encoding="utf-8"))
            if check is None or check[1] != docs_head:
                raise SystemExit(f"STOP {repo}: docs-version.txt does not name {docs_head[:7]} after the merge; not pushing")
            git(wt, "add", "--", "docs-version.txt")
            merging = pathlib.Path(git(wt, "rev-parse", "--git-path", "MERGE_HEAD").strip())
            merging = merging if merging.is_absolute() else wt / merging
            if not merging.exists():
                say(f"{repo}: {branch} is up to date with {base}; nothing to refresh")
                return ""
            git(wt, "commit", "-q", "-m", f"Merge {base}; docs-version to {docs_label} head")
            new = git(wt, "rev-parse", "HEAD").strip()
            git(wt, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
            say(f"{repo}: merged {base} into {branch}; docs pin -> {docs_head[:7]} ({docs_label} head)")
            return new
        finally:
            git(src, "worktree", "remove", "--force", str(wt))


CONFIG = [pathlib.Path(__file__).resolve().parent / "ship_watch.yaml"]  # --config (the watcher passes its own)


def read_config(path: pathlib.Path | None = None) -> dict:
    """ship_watch.yaml (or --config): {} when it is missing, unreadable or not a mapping, so nothing is exempted."""
    try:
        import yaml  # only here: the rest of this tool needs no third-party package
        cfg = yaml.safe_load((path or CONFIG[0]).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - any failure reads as no settings (fail closed)
        return {}
    return cfg if isinstance(cfg, dict) else {}


def warn_checks(path: pathlib.Path | None = None) -> tuple[list[tuple[str, str, str]], str]:
    """Decision 173 (§6.6 build rulings): the checks that warn but never block a merge, as (repo, workflow, exact check
    name) from `warn_checks`, and the end condition `warn_until`. No `warn_until`, an empty list, or any malformed
    entry gives no warn checks, so every check blocks (fail closed). Deleting the list ends it."""
    cfg = read_config(path)
    rows, until = cfg.get("warn_checks"), cfg.get("warn_until")
    if not isinstance(until, str) or not until.strip() or not isinstance(rows, list) or not rows:
        return [], ""
    keys = ("repo", "workflow", "name")
    if not all(isinstance(r, dict) and all(isinstance(r.get(k), str) and r[k] for k in keys) for r in rows):
        return [], ""
    return [(r["repo"], r["workflow"], r["name"]) for r in rows], until


def is_warn(c: dict, warn: list[tuple[str, str, str]], repo: str) -> bool:
    return (repo, c.get("workflow"), c.get("name")) in warn


GREEN = ("pass", "skipping")  # any other bucket, or none, is red or pending (§6.6 fail closed on every shape)


def wait_green(repo: str, n: int, may_rerun: bool) -> None:
    deadline, reran = time.time() + CHECK_TIMEOUT_S, False
    warn, until = warn_checks()
    told = False
    while time.time() < deadline:
        cs = checks(repo, n)
        skipped = sorted({c["name"] for c in cs if is_warn(c, warn, repo)})
        if skipped and not told:  # decision 173: they still run and report; the watcher alerts when one is red
            say(f"{repo}#{n}: not waiting on warn-only check(s) {skipped} (until {until})")
            told = True
        cs = [c for c in cs if not is_warn(c, warn, repo)]
        pending = [c["name"] for c in cs if c.get("bucket") == "pending"]
        failed = [c for c in cs if c.get("bucket") not in (*GREEN, "pending")]
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


def bump_pins(repo: str, branch: str, tag: str, sha: str, source: str | None = None) -> str | None:
    """Move every OnePeek/.github workflow pin on the branch to the tag, in one ordinary commit (hooks run), on top of
    `source` when given (the push fails if the branch moved), else on the branch head."""
    refuse_protected(repo, branch)
    src = ROOT / repo
    git(src, "fetch", "-q", "origin", branch)
    with tempfile.TemporaryDirectory(prefix=f"ship-{repo}-") as tmp:
        wt = pathlib.Path(tmp) / "wt"
        git(src, "worktree", "add", "-q", "--detach", str(wt), source or f"origin/{branch}")
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


def pr_commits(repo: str, n: int) -> set[str]:
    """Every commit of a pull request (GitHub lists at most 250)."""
    return set(gh("api", "--paginate", f"repos/{ORG}/{repo}/pulls/{n}/commits?per_page=100", "--jq", ".[].sha").split())


def pr_files(repo: str, n: int) -> list[str]:
    """The paths a pull request changes (GitHub lists at most 3,000)."""
    return gh("api", "--paginate", f"repos/{ORG}/{repo}/pulls/{n}/files?per_page=100", "--jq", ".[].filename").split()


def pin_at(text: str) -> tuple[int, str] | None:
    """(line index, docs commit) of the first `onepeek-docs@<sha>` line of a docs-version.txt outside comments."""
    for i, line in enumerate(text.splitlines()):
        m = check_pins.DOCS_PIN.search(line)
        if m and not line.strip().startswith("#"):
            return i, m.group(1)
    return None


def pin_line(text: str) -> str | None:
    """The docs commit a docs-version.txt names (its first `onepeek-docs@<sha>` line outside comments)."""
    at = pin_at(text)
    return at[1] if at else None


def only_pin_changed(wt: pathlib.Path, green: str, new: str) -> bool:
    """True when the diff from the green commit to the new one is exactly one line of docs-version.txt replaced by
    another `onepeek-docs@<sha>` line and nothing else. It does not say the line is the pin line (a comment holding
    such text would pass) and says nothing about the base branch; move_docs_pin edits only the pin line, and main
    checks the base separately (base_unchanged)."""
    if git(wt, "diff", "--numstat", green, new).strip() != "1\t1\tdocs-version.txt":
        return False
    lines = [ln for ln in git(wt, "diff", "-U0", green, new, "--", "docs-version.txt").splitlines()
             if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
    return len(lines) == 2 and all(check_pins.DOCS_PIN.fullmatch(ln[1:].strip()) for ln in lines)


def move_docs_pin(repo: str, branch: str, green: str, merge_sha: str) -> tuple[str | None, bool]:
    """Move the branch's docs-version.txt pin to the docs merge commit, in one commit on top of the green head
    `green`, pushed as an ordinary fast-forward (it fails if the branch moved). Returns (the pushed commit or None when
    the pin is already there, whether only the pin line changed since `green`)."""
    refuse_protected(repo, branch)
    src = ROOT / repo
    git(src, "fetch", "-q", "origin", branch)
    with tempfile.TemporaryDirectory(prefix=f"docs-pin-{repo}-") as tmp:
        wt = pathlib.Path(tmp) / "wt"
        git(src, "worktree", "add", "-q", "--detach", str(wt), green)
        try:
            f = wt / "docs-version.txt"
            text = f.read_text(encoding="utf-8")
            at = pin_at(text)
            old = at[1] if at else None
            if old is None:
                raise SystemExit(f"STOP {repo}: docs-version.txt at {green[:7]} holds no onepeek-docs@<sha> line")
            if old == merge_sha:
                return None, True
            lines = text.splitlines(keepends=True)
            lines[at[0]] = lines[at[0]].replace(f"onepeek-docs@{old}", f"onepeek-docs@{merge_sha}", 1)  # that line only
            f.write_text("".join(lines), encoding="utf-8")
            git(wt, "add", "--", "docs-version.txt")
            git(wt, "commit", "-q", "-m", f"chore: pin docs to the merge commit {merge_sha[:7]} (ship_change_set)")
            new = git(wt, "rev-parse", "HEAD").strip()
            only = only_pin_changed(wt, green, new)
            git(wt, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
            say(f"{repo}: docs pin -> {merge_sha[:7]} on {branch}")
            return new, only
        finally:
            git(src, "worktree", "remove", "--force", str(wt))


def tested_bases(repo: str, n: int) -> set[str]:
    """The base-branch commits the pull request's current GitHub Actions runs tested (each run's
    pull_requests[].base.sha for this pull request; equivalent to the merge commit's first parent, since the base only
    moves forward and the merge commit is built at trigger time). Checks without an Actions run link (CodeRabbit and
    other apps) are ignored, as checks() ignores them; empty when no run says, which callers treat as unknown."""
    out: set[str] = set()
    runs = 0
    for c in read_checks(repo, n, False):
        m = re.search(r"/runs/(\d+)", c.get("link") or "")
        if not m:
            continue
        runs += 1
        prs = json.loads(gh("api", f"repos/{ORG}/{repo}/actions/runs/{m.group(1)}", "--jq", ".pull_requests") or "[]")
        shas = {p["base"]["sha"] for p in prs if p.get("number") == n}
        if not shas:
            return set()
        out |= shas
    return out if runs else set()


def base_head(repo: str, branch: str) -> str:
    """The base branch's head commit now."""
    return gh("api", f"repos/{ORG}/{repo}/commits/{branch}", "--jq", ".sha").strip()


def base_unchanged(repo: str, n: int, tested: set[str], branch: str) -> bool:
    """True when the green run tested the base branch's current head (lesson L27 b), so a pin-only push needs no new run."""
    return tested == {base_head(repo, branch)}


HEAD_MOVES_MAX = 5  # a head that keeps moving stops the pull request instead of waiting forever (ruling T-56)


def green_head(repo: str, n: int, may_rerun: bool) -> str:
    """Wait for the required checks to be green and return the head they were green on (asked again if it moved).
    After HEAD_MOVES_MAX moves in one wait it STOPs, naming the heads."""
    heads = [pr_info(repo, n)["headRefOid"]]
    while True:
        wait_green(repo, n, may_rerun)
        heads.append(pr_info(repo, n)["headRefOid"])
        if heads[-1] == heads[-2]:
            return heads[-1]
        if len(heads) - 1 >= HEAD_MOVES_MAX:
            raise SystemExit(f"STOP {repo}#{n}: head moved {HEAD_MOVES_MAX} times while its checks ran "
                             f"({' -> '.join(h[:7] for h in heads)}); nothing after it is merged")
        say(f"{repo}#{n}: head moved while its checks ran; waiting again")


def with_dependencies(prs: list[tuple[str, int]], deps: dict) -> list[tuple[str, int]]:
    """Each pull request after the ones in the set it declares (`Depends on:`); otherwise the given order is kept.
    onepeek-docs stays first, so a docs pull request may not depend on another pull request in the set."""
    out = [p for p in prs if p[0] == DOCS]
    for p in out:
        inside = [d for d in deps.get(p, []) if d in prs]
        if inside:
            raise SystemExit(f"STOP: {p[0]}#{p[1]} merges first (§8a P2), so it cannot depend on "
                             f"{', '.join(f'{r}#{n}' for r, n in inside)} in the set")
    left = [p for p in prs if p[0] != DOCS]
    while left:
        ready = next((p for p in left if all(d in out or d not in prs for d in deps.get(p, []))), None)
        if ready is None:
            raise SystemExit(f"STOP: the Depends on lines form a cycle among {[f'{r}#{n}' for r, n in left]}")
        out.append(ready)
        left.remove(ready)
    return out


def rerun_failed(repo: str, n: int) -> None:
    """Re-run the failed runs of a pull request (its depends-on job failed while the dependency, declared or its docs
    pin's pull request, was unmerged)."""
    failed = [c for c in read_checks(repo, n, False) if c["bucket"] in ("fail", "cancel")]
    runs = {m.group(1) for c in failed if (m := re.search(r"/runs/(\d+)", c.get("link") or ""))}
    for run_id in runs:
        gh("run", "rerun", run_id, "-R", f"{ORG}/{repo}", "--failed")
    if runs:
        say(f"{repo}#{n}: re-ran {len(runs)} failed run(s) now that its dependency merged")
        time.sleep(POLL_S)  # let the re-runs register before the checks are read again


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


def moved_pin(m: re.Match, tag: str, sha: str) -> str:
    """One `uses:` moved to sha with `# tag`: only the ref and a leading version tag in its comment change; any text
    after that tag is kept. A comment that does not start with a version tag (e.g. `# pre-tag ...`) becomes `# tag`."""
    c = VERSION_COMMENT.fullmatch(m.group("comment") or "")
    path = f"/{m.group('path')}" if m.group("path") is not None else ""
    return f"{m.group('lead')}{m.group('name')}{path}@{sha}{m.group('quote')} # {tag}{c.group(2) if c else ''}"


def rewrite_pins(wt: pathlib.Path, tag: str, sha: str) -> tuple[list[str], set[str]]:
    """Move every pin under wt/.github/workflows to tag@sha; returns (changed files, the tags they were on)."""
    changed, old = [], set()
    for f in sorted((wt / ".github" / "workflows").glob("*.y*ml")):
        text = f.read_text()
        for m in PIN.finditer(text):
            c = VERSION_COMMENT.fullmatch(m.group("comment") or "")
            old.add(c.group(1) if c else "untagged")
        new = PIN.sub(lambda m: moved_pin(m, tag, sha), text)
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
        r = subprocess.run(["git", "-C", str(d), "grep", "-q", "-i", "-E", "uses:[[:space:]]*[\"']?OnePeek/[.]github([/@[:space:]\"']|$)",
                            f"origin/{base}", "--", ".github/workflows"],
                           capture_output=True, check=False)
        if r.returncode == 0:
            out.append(d.name)
    return out


def open_pin_move(repo: str, tag: str, sha: str) -> str | None:
    """A pin-move pull request in a caller whose pins were not moved inside the set: every pin to tag@sha, one commit
    pushed from a detached worktree to `chore/pin-github-<tag>` (no local branch is created, none is deleted). Safe to
    re-run: when that branch already exists the pins are rewritten on top of it (anyone else's commits stay) and pushed
    as an ordinary fast-forward, never forced, or not pushed at all when it already has them; only a missing branch is
    built from the base. An open pull request for it is updated, never a second one opened."""
    src, base = ROOT / repo, BASE.get(repo, "dev")
    branch = f"chore/pin-github-{tag}"
    git(src, "fetch", "-q", "origin", base)
    on_remote = bool(git(src, "ls-remote", "origin", f"refs/heads/{branch}").split())
    if on_remote:
        git(src, "fetch", "-q", "origin", branch)
    with tempfile.TemporaryDirectory(prefix=f"pins-{repo}-") as tmp:
        wt = pathlib.Path(tmp) / "wt"
        git(src, "worktree", "add", "-q", "--detach", str(wt), f"origin/{base}")
        try:
            changed, old = rewrite_pins(wt, tag, sha)
            if not changed:
                say(f"{repo}: {base} already pins {tag}; no pin-move pull request")
                return None
            existing = json.loads(gh("pr", "list", "-R", f"{ORG}/{repo}", "--head", branch, "--state", "open",
                                     "--json", "number,url") or "[]")
            if on_remote:
                git(wt, "checkout", "-q", "-f", "--detach", f"origin/{branch}")
                changed, _ = rewrite_pins(wt, tag, sha)
            if changed:
                git(wt, "add", "--", *changed)
                git(wt, "commit", "-q", "-m", f"chore: pin OnePeek/.github workflows to {tag} (ship_change_set)")
                git(wt, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        finally:
            git(src, "worktree", "remove", "--force", str(wt))
    if existing:
        url = existing[0]["url"]
        say(f"{repo}: pin-move pull request {url} updated")
        return url
    body = (f"Moves every `OnePeek/.github` workflow pin to `{tag}` (`{sha}`), opened by `ship_change_set.py` after "
            f"`.github` was tagged (onepeek-docs foundation-playbook §8a P2). Pins before: {', '.join(sorted(old))}.")
    if minor_behind(old, tag):
        body += ("\n\n**More than one minor version behind:** the shared workflows' inputs or secrets may have changed "
                 "since; check each call against the workflow at the new tag and fix it by hand in this pull request.")
    url = gh("pr", "create", "-R", f"{ORG}/{repo}", "--base", base, "--head", branch,
             "--title", f"chore: pin OnePeek/.github workflows to {tag}", "--body", body).strip()
    say(f"{repo}: pin-move pull request {url}")
    return url


def pin_moves(moved: set[str], tag: str, sha: str) -> list[str]:
    """A pin-move pull request in every caller whose pins were not moved inside the set (§8a P2), including one merged
    before .github; a failure in one caller is collected and the others still run. Returns the failures."""
    failures = []
    for repo in callers():
        if repo in moved:
            continue
        try:
            open_pin_move(repo, tag, sha)
        except SystemExit as e:
            failures.append(f"{repo}: {e}")
            say(f"{repo}: pin move failed, going on with the others: {e}")
    return failures


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


def branch_pin(repo: str, branch: str) -> str | None:
    """The docs commit docs-version.txt names on the branch; None when the file or the line is not there (a 404).
    Any other read error stops the run: it is not "no pin"."""
    r = subprocess.run(["gh", "api", f"repos/{ORG}/{repo}/contents/docs-version.txt?ref={branch}",
                        "-H", "Accept: application/vnd.github.raw"], capture_output=True, text=True, check=False)
    if r.returncode == 0:
        return pin_line(r.stdout)
    if "404" in r.stderr:
        return None
    raise SystemExit(f"STOP {repo}: cannot read docs-version.txt on {branch}: {(r.stderr or r.stdout).strip()[:200]}")


def docs_pin_problems(repo: str, n: int, branch: str) -> list[str]:
    """Strict form of the implicit docs wait for a pull request whose docs pull request is not in the set: its pin must
    be on docs main (check_depends_on.docs_waiting with strict, so a pin in an open docs pull request fails too). Only
    a missing file or line (404) means no pin; any other read error is a problem."""
    try:
        pin = branch_pin(repo, branch)
    except SystemExit as e:
        return [str(e)]
    if not pin:
        return []
    token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=False).stdout.strip()
    problems, _ = depends_on.docs_waiting(pin, lambda s: check_pins.compare_status(s, token),
                                          lambda s: depends_on.open_docs_prs(s, token), strict=True)
    return [f"{repo}#{n}: {p}; add its docs pull request to the set" for p in problems]


def docs_head(n: int) -> str:
    return pr_info(DOCS, n)["headRefOid"]


def resolve_pin(repo: str, n: int, push: bool = True) -> str | None:
    """refresh_docs_pin for a code pull request of the set, against the set's docs pull request (`DOCS_PR`, set by
    main; empty for a set with none or a pin round). None (not refreshed) for the docs and .github pull requests, a
    fork or base-branch head, a branch pin that is not a commit of that docs pull request, or another conflicting file."""
    if repo in (DOCS, ".github") or not DOCS_PR:
        return None
    i = pr_info(repo, n)
    why = own_branch_problem(repo, n, i)
    if why:
        say(why)
        return None
    pin = branch_pin(repo, i["headRefName"])
    if pin not in pr_commits(DOCS, DOCS_PR[0]):
        say(f"{repo}#{n}: docs pin {(pin or 'none')[:7]} is not a commit of {DOCS}#{DOCS_PR[0]}; not re-pinning it")
        return None
    source = tool_source(repo, n, i["headRefOid"])
    sha = refresh_docs_pin(repo, i["headRefName"], i["baseRefName"], f"{DOCS}#{DOCS_PR[0]}", docs_head(DOCS_PR[0]), push,
                           source=source)
    if sha:
        TOOL_PUSHED[sha] = source
    return sha


def conflict_after(repo: str, n: int, sha: str) -> str:
    wait_head(repo, n, sha)
    return mergeable(repo, n)


DOCS_PR: list[int] = []
TOOL_PUSHED: dict[str, str] = {}  # commit this run pushed (pin moves, docs-pin refreshes) -> the commit it was built on
GATED: dict[tuple[str, int], str] = {}  # the head each pull request passed the up-front gates at (reviewed or planned)


def tool_source(repo: str, n: int, head: str) -> str:
    """The commit a tool push may build on (ruling T-55): the head that passed the gates, or a commit this run pushed
    on top of it. Anything else (a commit pushed since the gate, or no gate at all) stops the pull request."""
    gated = GATED.get((repo, n))
    if gated and (head == gated or head in TOOL_PUSHED):
        return head
    raise SystemExit(f"STOP {repo}#{n}: head {head[:7]} is neither the gated head {(gated or 'none')[:7]} nor a "
                     "commit this run pushed; not pushing onto it, nothing after it is merged")


def hotfix(i: dict) -> bool:
    """operating-model §3 unblock lane: the body's first line says `Lane: hotfix` (packet.lane_of, ruling T-53 round 3)
    and it is not a release pull request. Only hotfix skips the review gates: `docs`, `feature`, no lane line or an odd
    body keep them (fail closed)."""
    return i.get("baseRefName") != RELEASE_BASE and packet.lane_of(i.get("body")) == "hotfix"


def review_gate(repo: str, n: int, i: dict) -> str | None:
    """agent-operations.md §6.6 "Review gates for merging" (F36) for the pull request's head `i["headRefOid"]`, read
    live; the bot allow-list is the `authors` of ship_watch.yaml (--config). An unreadable record is a problem.
    A hotfix pull request has none: it is reviewed after the merge (operating-model §3)."""
    if hotfix(i):
        return None
    try:
        reviews = read_reviews()
    except (OSError, ValueError) as e:
        return f"{repo}#{n}: cannot read the review record: {e}"
    authors = read_config().get("authors")
    why = review_problem(f"{repo}#{n}", i["headRefOid"], i.get("author"), read_checks(repo, n, False), reviews,
                         authors if isinstance(authors, list) else [])
    return f"{repo}#{n}: {why}" if why else None


def refresh_one(repo: str, n: int) -> int:
    """`--refresh`: only the docs-pin refresh, for one open, conflicting pull request of this repository; its docs pull
    request is the one open docs pull request holding its pin."""
    if not (ROOT / repo / ".git").exists():
        raise SystemExit(f"run from the workspace root (the folder holding the repos); {ROOT} is not it")
    i = pr_info(repo, n)
    if i["state"] != "OPEN":
        raise SystemExit(f"STOP {repo}#{n} is {i['state']}")
    why = own_branch_problem(repo, n, i)
    if why:
        raise SystemExit(f"STOP {why}")
    state = mergeable(repo, n)
    if state != "CONFLICTING":
        raise SystemExit(f"STOP {repo}#{n} is {state}, not CONFLICTING; nothing to refresh")
    pin = branch_pin(repo, i["headRefName"])
    if not pin:
        raise SystemExit(f"STOP {repo}#{n}: no readable docs pin on {i['headRefName']}")
    token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=False).stdout.strip()
    found = depends_on.open_docs_prs(pin, token)
    if not found:
        raise SystemExit(f"STOP {repo}#{n}: pin {pin[:7]} is in no open {DOCS} pull request; nothing to refresh to")
    if len(found) > 1:
        raise SystemExit(f"STOP {repo}#{n}: pin {pin[:7]} is in {len(found)} open {DOCS} pull requests {found}; "
                         "cannot tell which is its change set")
    sha = refresh_docs_pin(repo, i["headRefName"], i["baseRefName"], f"{DOCS}#{found[0]}", docs_head(found[0]))
    if sha is None:
        raise SystemExit(f"STOP {repo}#{n}: conflicts in more than docs-version.txt; resolve them by hand")
    return 0


REVIEW_THREADS = """query($owner: String!, $repo: String!, $n: Int!, $endCursor: String) {
  repository(owner: $owner, name: $repo) { pullRequest(number: $n) {
    reviewThreads(first: 100, after: $endCursor) {
      nodes { isResolved path line originalLine }
      pageInfo { hasNextPage endCursor } } } } }"""


def unresolved_threads(repo: str, n: int) -> list[str]:
    """`path:line` of every review thread not resolved (agent-operations.md §6.4, F11). A thread whose resolution is not
    `true` counts as unresolved, and a failed or unreadable query stops the run (fail closed, pitfalls.md 14)."""
    out = gh("api", "graphql", "--paginate", "-f", f"query={REVIEW_THREADS}", "-f", f"owner={ORG}", "-f", f"repo={repo}",
             "-F", f"n={n}", "--jq", ".data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved != true)"
             " | [.path, (.line // .originalLine)] | @json")
    threads = []
    for line in out.splitlines():
        if line.strip():
            path, at = json.loads(line)
            threads.append(f"{path}:{'?' if at is None else at}")
    return threads


REVIEWED_CLASSES = ("mechanical", "docs only")  # §5.4a classes the coordinator's check stands in for G6


def reviews_file() -> pathlib.Path:
    return pathlib.Path(os.environ.get("ONEPEEK_WORKSPACE") or ROOT) / ".ship" / "reviews.json"


def read_reviews(path: pathlib.Path | None = None) -> dict:
    """The coordinator's record of independent-review verdicts (agent-operations.md §6.6 "Review gates for merging"):
    `{"<repo>#<n>": "<head sha>"}` or `{"<repo>#<n>": {"sha": "<head>", "class": "mechanical"}}`. A missing file is no
    reviews; one that does not parse or is not an object raises ValueError (fail closed)."""
    path = path or reviews_file()
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def review_problem(label: str, head: str, author: dict | None, all_checks: list[dict], reviews: dict,
                   bots: list[str]) -> str | None:
    """Why the pull request may not merge yet under the review gates (F36, pitfalls.md 30), or None: CodeRabbit has
    finished on the head (`pass` or `skipping`) and the recorded review is at exactly this head (or the recorded class
    is mechanical or docs only, at this head). A bot author on the allow-list is exempt."""
    author = author or {}
    if author.get("is_bot") is True and author.get("login") in bots:
        return None
    bot = [c.get("bucket") for c in all_checks if c.get("name") == "CodeRabbit"]
    if not bot or any(b not in ("pass", "skipping") for b in bot):
        return f"no review at {head}: CodeRabbit {'/'.join(map(str, bot)) or 'has not reported'} on the head"
    r = reviews.get(label)
    if r == head or (isinstance(r, dict) and r.get("sha") == head and r.get("class") in REVIEWED_CLASSES):
        return None
    return f"no review at {head}"


def refuse_unresolved(repo: str, n: int) -> None:
    """Right before a merge: stop while the pull request has any unresolved review thread, naming each one."""
    threads = unresolved_threads(repo, n)
    if threads:
        for t in threads:
            say(f"{repo}#{n}: unresolved review thread {t}")
        raise SystemExit(f"STOP {repo}#{n}: {len(threads)} unresolved review thread(s); fix or answer and resolve "
                         "each one, then run again; nothing after it is merged")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("prs", nargs="*", help="<repo>#<number>, any order")
    ap.add_argument("--refresh", metavar="REPO#N", help="only merge the base into that pull request's branch and set "
                    "docs-version.txt to its docs pull request head (for a conflict in docs-version.txt alone)")
    ap.add_argument("--go", action="store_true", help="merge (default: print the plan and the up-front checks)")
    ap.add_argument("--order", default="", help="repoA,repoB,...: merge in this order instead of the fixed one (§8a P1)")
    ap.add_argument("--aws-profile", default="", help="AWS profile that can read s3://onepeek-<env>-deploy (release pull requests)")
    ap.add_argument("--no-chain", metavar="REASON", default="", help="release pull requests: skip the chain check (never the lock), with the reason")
    ap.add_argument("--config", default=str(CONFIG[0]), help="ship_watch.yaml to read: warn-only checks, bot authors")
    a = ap.parse_args(argv)
    CONFIG[:] = [pathlib.Path(a.config)]
    if a.refresh:
        return refresh_one(*parse(a.refresh))
    if not a.prs:
        ap.error("give the pull requests of the change set, or --refresh <repo>#<n>")

    prs = ordered([parse(p) for p in a.prs], a.order)
    local = any(r == ".github" for r, _ in prs) or (any(r == DOCS for r, _ in prs) and len(prs) > 1)
    if local and not all((ROOT / r / ".git").exists() for r, _ in prs):
        raise SystemExit(f"run from the workspace root (the folder holding the repos); {ROOT} is not it")
    info = {p: pr_info(*p) for p in prs}
    GATED.clear()
    TOOL_PUSHED.clear()
    GATED.update({p: i["headRefOid"] for p, i in info.items()})
    deps = {p: depends_on.parse(i.get("body")) for p, i in info.items()}
    prs = with_dependencies(prs, deps)
    problems = []
    release_prs = [r for (r, _), i in info.items() if i["baseRefName"] == RELEASE_BASE and r in CHAIN]
    if release_prs:  # release pull requests (§2c steps 3 and 4): their own set, then the lock, then the chain
        mixed = [r for r, _ in prs if r in (DOCS, ".github")]
        if mixed:
            say(f"problem: release pull requests ship in their own set, after the dependencies have merged; this set also holds {', '.join(mixed)}")
            return 1
        why = lock_problem(RELEASE_ENV, release_lock(RELEASE_ENV, a.aws_profile))
        if why:
            problems.append(why)
        if a.no_chain:
            say(f"chain check skipped (--no-chain: {a.no_chain}); the lock is still checked")
        else:
            for r in release_prs:
                why = chain_problem(r, release_prs)
                if why:
                    problems.append(why + " (override with --no-chain REASON)")
    docs = [p for p in prs if p[0] == "onepeek-docs"]
    DOCS_PR[:] = [docs[0][1]] if docs else []
    for p, ds in deps.items():
        for d in ds:
            if d not in info and pr_info(*d)["state"] != "MERGED":
                problems.append(f"{p[0]}#{p[1]} depends on {d[0]}#{d[1]}, which is neither in the set nor merged")
                if not info[p]["isDraft"]:  # §2c step 2: a pull request with an open dependency is a draft
                    problems += depends_on.draft_gate([d], False, lambda r, n: (None, False))
    conflicting: list[tuple[str, int]] = []
    for (repo, n), i in info.items():
        want = RELEASE_BASE if i["baseRefName"] == RELEASE_BASE and repo in CHAIN else BASE.get(repo, "dev")
        if i["state"] != "OPEN":
            problems.append(f"{repo}#{n} is {i['state']}")
        if i["baseRefName"] != want:
            problems.append(f"{repo}#{n} targets {i['baseRefName']}, not {want}")
        if i["isDraft"]:
            problems.append(f"{repo}#{n} is a draft")
        why = own_branch_problem(repo, n, i)
        if why:
            problems.append(why)
        gate = review_gate(repo, n, i) if i["state"] == "OPEN" else None
        if gate:
            problems.append(gate)
        state = mergeable(repo, n) if i["mergeable"] == "UNKNOWN" else i["mergeable"]
        if state == "CONFLICTING" and docs and repo not in (DOCS, ".github") and not why:
            conflicting.append((repo, n))  # maybe only docs-version.txt: refreshed below, once nothing else is wrong
        elif state != "MERGEABLE":
            problems.append(f"{repo}#{n} is {state}: resolve conflicts before any check is waited on")
    if len(docs) > 1:
        problems.append("a change set has one docs pull request")
    pinned = set()  # code pull requests whose docs pin is a commit of the set's docs pull request (lesson L27 b)
    files = pr_files(*docs[0]) if docs else []
    workflow_only = bool(files) and all(f.startswith(".github/") for f in files)
    if workflow_only:  # a pin round's docs pull request changes no docs content: no code pin needs to be its commit (L31)
        say(f"{docs[0][0]}#{docs[0][1]} changes only .github/ (a pin round): code pins are not checked against it")
        DOCS_PR[:] = []  # and no pin is moved to its head
        problems += [f"{r}#{n} is CONFLICTING: resolve conflicts before any check is waited on" for r, n in conflicting]
        conflicting = []
    if docs and not workflow_only:  # every code pin must be a commit of the set's docs pull request (§2a step 2, L27 b)
        commits = pr_commits(*docs[0])
        for repo, n in prs:
            if repo in ("onepeek-docs", ".github"):
                continue
            url = f"repos/{ORG}/{repo}/contents/docs-version.txt?ref={info[(repo, n)]['headRefName']}"
            r = subprocess.run(["gh", "api", url, "-H", "Accept: application/vnd.github.raw"],
                               capture_output=True, text=True, check=False)
            pin = pin_line(r.stdout) if r.returncode == 0 else None
            if r.returncode != 0:
                problems.append(f"{repo}#{n} has no readable docs-version.txt")
            elif pin not in commits:
                problems.append(f"{repo}#{n} docs-version.txt is {(pin or 'no pin')[:7]}, not a commit of "
                                f"{docs[0][0]}#{docs[0][1]}")
            else:
                pinned.add((repo, n))
                deps.setdefault((repo, n), []).append(docs[0])  # implicit: re-run its checks after the docs merge

    if not docs:  # no docs pull request in the set: every code pin must already be on docs main (strict wait, L27 a)
        for repo, n in prs:
            if repo != ".github":
                problems += docs_pin_problems(repo, n, info[(repo, n)]["headRefName"])

    say("plan: " + " -> ".join(f"{r}#{n} ({merge_method(r, info[(r, n)]['baseRefName'])[2:]} into {info[(r, n)]['baseRefName']})"
                               for r, n in prs))
    for p in problems:
        say(f"problem: {p}")
    if problems:
        return 1
    for repo, n in conflicting:  # only now, with every other check passed: a conflict in docs-version.txt alone
        sha = resolve_pin(repo, n, push=a.go)
        if sha is None:
            say(f"problem: {repo}#{n} is CONFLICTING and cannot be refreshed: resolve conflicts, push, run again")
            return 1
        if not a.go:
            say(f"would refresh {repo}#{n}: merge {info[(repo, n)]['baseRefName']} into its branch, "
                f"docs pin -> {DOCS}#{docs[0][1]} head (add --go)")
            continue
        state = conflict_after(repo, n, sha) if sha else mergeable(repo, n)
        if state != "MERGEABLE":
            say(f"problem: {repo}#{n} is {state} after the refresh: resolve conflicts, push, run again")
            return 1
    if not a.go:
        say("dry run: add --go to merge")
        return 0

    merged_any, tag, moved, docs_merge = False, None, set(), None
    for k, (repo, n) in enumerate(prs):
        no_conflicts(prs[k:], resolve_pin)
        if tag and repo not in (DOCS, ".github"):
            source = tool_source(repo, n, pr_info(repo, n)["headRefOid"])
            pushed = bump_pins(repo, info[(repo, n)]["headRefName"], *tag, source=source)
            if pushed:
                TOOL_PUSHED[pushed] = source
            moved.add(repo)  # its pins are at the tag inside the set (moved now, or already there)
            if pushed:
                wait_head(repo, n, pushed)
                no_conflicts([(repo, n)])
        head = green_head(repo, n, may_rerun=merged_any)
        tested = tested_bases(repo, n) if docs_merge and (repo, n) in pinned else set()
        if docs_merge and (repo, n) in pinned:
            pushed, only_pin = move_docs_pin(repo, info[(repo, n)]["headRefName"], tool_source(repo, n, head), docs_merge)
            if pushed:
                TOOL_PUSHED[pushed] = head
            if pushed:
                wait_head(repo, n, pushed)
                head = pushed
                if not only_pin or not base_unchanged(repo, n, tested, info[(repo, n)]["baseRefName"]):
                    say(f"{repo}#{n}: more than the docs pin changed since the green run, or its base moved; "
                        "waiting for the new CI run")
                    head = green_head(repo, n, may_rerun=False)
                else:
                    say(f"{repo}#{n}: only the docs pin changed and the green run tested the current base; merging without a new CI run")
        method = merge_method(repo, info[(repo, n)]["baseRefName"])
        base_ref = info[(repo, n)]["baseRefName"]
        if base_ref == RELEASE_BASE and repo in CHAIN:  # the lock again, right before the merge (§2c step 3)
            why = lock_problem(RELEASE_ENV, release_lock(RELEASE_ENV, a.aws_profile))
            if why:
                raise SystemExit(f"STOP {repo}#{n}: {why}")
        keep = [] if base_ref == RELEASE_BASE else ["--delete-branch"]  # a release pull request's head is `dev`: never deleted
        live = dict(info[(repo, n)], body=pr_info(repo, n).get("body"))  # the lane, read again now (never cached)
        lanes = ["hotfix" if hotfix(x) else "feature" for x in (info[(repo, n)], live)]
        if lanes[0] != lanes[1]:
            raise SystemExit(f"STOP {repo}#{n}: the lane changed since the plan ({lanes[0]} -> {lanes[1]}); run "
                             "again; nothing after it is merged")
        if lanes[1] == "feature":
            refuse_unresolved(repo, n)  # every review thread read and resolved before the merge (§6.4)
        reviewed = info[(repo, n)]["headRefOid"]  # the review gates, again, right before the merge (F36)
        why = review_gate(repo, n, live) if lanes[1] == "feature" else None
        if not why and head != reviewed and head not in TOOL_PUSHED:
            why = f"{repo}#{n}: head {head[:7]} is neither the reviewed head {reviewed[:7]} nor a commit this run pushed"
        if why:
            raise SystemExit(f"STOP {why}; nothing after it is merged")
        gh("pr", "merge", str(n), "-R", f"{ORG}/{repo}", method, *keep, "--match-head-commit", head)
        merged = pr_info(repo, n)
        if merged["state"] != "MERGED":
            raise SystemExit(f"STOP {repo}#{n}: merge did not complete ({merged['state']})")
        say(f"{repo}#{n}: merged {merged['mergeCommit']['oid'][:7]}")
        if hotfix(live):
            say(f"{repo}#{n}: Lane: hotfix, post-merge review due (operating-model §3)")
        if repo == DOCS:
            docs_merge = merged["mergeCommit"]["oid"]
        if base_ref != RELEASE_BASE:  # never touch the worktrees of `dev`, the head of a release pull request
            remove_worktrees(repo, info[(repo, n)]["headRefName"])
        for later in prs[k + 1:]:
            if (repo, n) in deps.get(later, []):
                rerun_failed(*later)
        merged_any = True
        if repo == ".github":
            tag = wait_tag(merged["mergeCommit"]["oid"])
            say(f".github: tagged {tag[0]}")
    failures = pin_moves(moved, *tag) if tag else []
    say("change set merged. Follow-ups: republish architecture pages after a docs merge; status.yaml.")
    if failures:
        for f in failures:
            say(f"pin move failed: {f}")
        say(f"{len(failures)} pin move(s) failed; fix and re-run them (they are safe to re-run)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
