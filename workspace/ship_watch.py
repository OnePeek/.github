"""The watcher and the merge queue (onepeek-docs standards/agent-operations.md §6.6): a script, no AI, no tokens.

    python3 .github/workspace/ship_watch.py                    # a pass every 3 minutes
    python3 .github/workspace/ship_watch.py --once --dry-run   # one pass that merges nothing
    python3 .github/workspace/ship_watch.py --go               # merge READY pull requests

Each pass reads every open pull request of the repos in ship_watch.yaml (its required checks on the head, `Depends on:`
lines, mergeable, draft, release or not), the org's runners against queued runs, the latest onepeek-infra sandbox.yml
run and the open release workflow runs. Each pull request is READY, BLOCKED (one reason) or HOLD (a release, a draft, or
an author not in the allow-list). Unresolved review threads and CodeRabbit's state never block, in any lane (founder,
2026-11-07, T-100). READY also needs the review gate (ship_change_set.review_problem): the coordinator's
$ONEPEEK_WORKSPACE/.ship/reviews.json naming that head (bots on the list exempt), except for a pull request whose
body's first line is `Lane: hotfix` (operating-model §3, packet.lane_of; `Lane: docs` keeps the gate): green checks
merge it, and its review is due after the merge (`post_merge_review_due` in measures.jsonl; ship-status.md lists every
one not yet in the orchestrator's $ONEPEEK_WORKSPACE/.ship/post-reviews.json under "Post-merge reviews due"). A check is
green only when it is `pass` or `skipping`. Every GitHub call goes through one layer that allows reads only, and every
gh or git read is bounded by READ_TIMEOUT_S (a timeout is a read error). Any read failure, or an odd shape (a missing
field, a non-list, a corrupt state or reviews file), fails the pass closed: an alert says why, nothing is merged or
re-run, and `--once` exits 1; the loop keeps running.

Direct pushes (T-86): ship_change_set.py pushes pin moves straight to a trunk and records each in
$ONEPEEK_WORKSPACE/.ship/direct-pushes.jsonl, one JSON object a line: {"repo", "branch" (the repo's trunk), "sha"}. Each
pass reads the CI of every entry not yet settled (the check runs on that commit, warn-only checks and CodeRabbit aside):
all green marks it verified; one red (failure, timed out, startup failure) reverts it: `git revert --no-edit <sha>` in a
fresh temporary worktree of $ONEPEEK_WORKSPACE/<repo>, a plain push to the trunk, proven by `git ls-remote`, and an
alert naming the repo and sha; still pending (or cancelled) after DIRECT_PENDING_S is an alert. It never reverts a
commit that is not in the file, not on the trunk, or already reverted, and a read error or an odd line is an alert and
no revert. The marks live in state.json.

Self-update (T-92): at the start of each pass (never during a merge run) it runs `git pull --ff-only origin main` in
$ONEPEEK_WORKSPACE/.github (on main only) and, when the blob of ship_watch.py, ship_change_set.py or packet.py on
origin/main differs from what it loaded, re-executes itself with the same arguments (os.execv), provided the file it
runs from now holds that blob; otherwise an alert says to restart it by hand.

READY ones merge one per `ship_change_set.py --go` run (so one docs pull request per run), docs first, then in the
fixed order; a run that fails is recorded and the queue goes on (F32). A red check is re-run once only when it is the
depends-on check and every pull request its `Depends on:` lines name has merged, or when it is a known flake. The only
writes are those merge-tool runs, `gh run rerun`, the self-update pull and the revert of a red direct push: it never
merges a release, closes a pull request, pushes anything else, or touches a review thread. While ship_watch.yaml lists
`warn_checks` (decision 173), those checks never block READY; a red one is an alert ("sandbox red on ..."). Without
--go or --dry-run, the first run (no state.json yet) is a dry run and later runs merge; measures.jsonl gets one line per
pull request it merged.

It writes ship-status.json, ship-status.md and state.json to --out, default $ONEPEEK_WORKSPACE/.ship/, never inside a
repo. A stop signal (SIGTERM, SIGHUP) still writes state.json, so timed-out runs, re-runs and direct-push marks survive.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import tempfile
import time
import types

import yaml

HERE = pathlib.Path(__file__).resolve().parent
ORG = "OnePeek"
INTERVAL_S = 3 * 60
NO_RUNNER_S = 10 * 60
NO_PROGRESS_S = 2 * 3600
RED_RUN = {"failure", "timed_out", "startup_failure"}
PR_LIMIT = 200
PR_FIELDS = ("number,title,url,author,isDraft,mergeable,headRefOid,headRefName,baseRefName,body,createdAt,"
             "isCrossRepository,headRepositoryOwner")
READS = (["pr", "list"], ["pr", "view"], ["pr", "checks"], ["run", "list"])
VALUE_FLAGS = {"--jq", "-q", "--template", "-t", "--header", "-H", "--cache", "--preview", "-p", "--hostname"}
GREEN = ("pass", "skipping")  # any other bucket, or none, is red or pending (§6.6 fail closed on every shape)
READ_TIMEOUT_S = 120  # every gh or git call the watcher makes; a timeout is a read error (T-97)
DIRECT_PENDING_S = 45 * 60  # a direct push whose CI is not settled by then is an alert (T-86 partner)
STOP_SIGNALS = {signal.SIGTERM, signal.SIGHUP}
CODE = ("ship_watch.py", "ship_change_set.py", "packet.py")  # a change to any on main re-executes the watcher (T-92)
SHA40 = re.compile(r"[0-9a-f]{40}")


class ReadError(Exception):
    pass


def _gh_process(args: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["gh", *args], capture_output=True, text=True, check=False, timeout=READ_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise ReadError(f"gh {' '.join(args[:3])} timed out after {READ_TIMEOUT_S} s") from None


def git(cwd: pathlib.Path, *args: str) -> str:
    """One git call, bounded by READ_TIMEOUT_S; a failure or a timeout is a ReadError (fail closed)."""
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False,
                           timeout=READ_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise ReadError(f"git {' '.join(args[:2])} timed out after {READ_TIMEOUT_S} s") from None
    except OSError as e:
        raise ReadError(f"git {' '.join(args[:2])}: {e}") from None
    if r.returncode != 0:
        raise ReadError(f"git {' '.join(args[:2])} exited {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}")
    return r.stdout


class MergeTimeout(Exception):
    pass


def _merge_process(cmd: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess:
    """One merge run, in its own session: past MERGE_TIMEOUT_S its whole process group (gh, git, their children) is
    killed and MergeTimeout raised (ruling T-56). Its own session keeps it out of the terminal's group, so Ctrl-C or any
    other exception in the wait kills the group too, then goes on up: a run never outlives the watcher (T-56 round 2).
    SIGTERM and SIGHUP reach this path as Stop (exit_on_signals). They are blocked from before the child starts until
    the try that kills it is armed, and again in the cleanup until the group is killed (T-97), so a Stop can never land
    between the two and leave the run free to merge; the child itself starts with the mask it would have had. A group
    holding only the exited child's zombie refuses the kill on macOS (PermissionError); it has nothing left to kill. Its
    pipes are never read after the kill, so a stray child holding them cannot hang the watcher."""
    old = signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
    try:
        child = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 start_new_session=True,
                                 preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_SETMASK, old))
    except BaseException:
        signal.pthread_sigmask(signal.SIG_SETMASK, old)
        raise
    try:
        signal.pthread_sigmask(signal.SIG_SETMASK, old)  # a Stop held since Popen is raised here, inside the try
        out, err = child.communicate(timeout=MERGE_TIMEOUT_S)
    except BaseException as e:
        signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
        try:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            child.wait()
            for pipe in (child.stdout, child.stderr):
                pipe.close()
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, old)  # a Stop held during the kill goes on up from here
        if isinstance(e, subprocess.TimeoutExpired):
            raise MergeTimeout(MERGE_TIMEOUT_S) from None
        raise
    return subprocess.CompletedProcess(cmd, child.returncode, out, err)


class Stop(BaseException):
    """SIGTERM or SIGHUP. A BaseException but not SystemExit, so no `except SystemExit` or `except Exception` in a pass
    swallows it (T-96); main() alone ends on it, with `code` (128 + the signal)."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.code = 128 + signum


def exit_on_signals() -> None:
    """SIGTERM and SIGHUP raise Stop, so a running merge run's group is killed on the way out (#82 round 2 review);
    by default they end the watcher at once and leave the run free to merge."""
    def leave(signum: int, _frame: object) -> None:
        raise Stop(signum)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, leave)


GH_RUN = _gh_process  # the one GitHub call layer (tests replace it)
MERGE_RUN = _merge_process  # ship_change_set.py runs (tests replace it)


def api_fields(args: list[str]) -> list[str] | None:
    """The `-f`/`-F`/`--field`/`--raw-field` values of a `gh api` call, joined or separate, or None when the call is
    not a read: `--input` in any form, or `-X`/`--method` other than GET."""
    fields, toks = [], list(args[1:])
    while toks:
        a = toks.pop(0)
        name, eq, val = a.partition("=") if a.startswith("--") else (a, "", "")
        if name in ("--field", "--raw-field"):
            fields.append(val if eq else (toks.pop(0) if toks else ""))
        elif name == "--method":
            if (val if eq else (toks.pop(0) if toks else "")).upper() != "GET":
                return None
        elif name == "--input":
            return None
        elif name in VALUE_FLAGS:
            if not eq and toks:
                toks.pop(0)
        elif a.startswith("-") and not a.startswith("--") and len(a) > 1:  # a short flag, or a cluster of them
            for k, ch in enumerate(a[1:]):
                rest = a[k + 2:] or (toks.pop(0) if toks and ch in "fFXqtHp" else "")
                if ch in "fF":
                    fields.append(rest)
                elif ch == "X" and rest.upper() != "GET":
                    return None
                if ch in "fFXqtHp":
                    break
    return fields


def allowed(args: list[str]) -> bool:
    """Reads, plus `run rerun`: never a merge, a close, a push, a thread or any other write. `gh api` takes fields only
    for a GraphQL query (one `query=query ...`, no `mutation`, no `operationName`)."""
    if args[:2] in READS or args[:2] == ["run", "rerun"]:
        return True
    fields = api_fields(args) if args[:1] == ["api"] else None
    if fields is None:
        return False
    if not fields and args[1:2] != ["graphql"]:
        return True  # a GET
    keys = [f.partition("=")[0] for f in fields]
    query = [f.partition("=")[2].lstrip() for f in fields if f.startswith("query=")]
    return (args[1:2] == ["graphql"] and len(query) == 1 and query[0].startswith("query")
            and "mutation" not in query[0] and "operationName" not in keys)


def gh(*args: str) -> str:
    if not allowed(list(args)):
        raise ReadError(f"refused: gh {' '.join(args[:3])}")
    r = GH_RUN(list(args))
    if r.returncode != 0:
        raise ReadError(f"gh {' '.join(args[:3])} exited {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}")
    return r.stdout


def _scs_run(cmd: list[str], **_kw) -> subprocess.CompletedProcess:
    """ship_change_set.py's own subprocess calls, routed through the same layer when this script reuses its readers."""
    if cmd[:1] != ["gh"] or not allowed(cmd[1:]):
        raise ReadError(f"refused: {' '.join(cmd[:3])}")
    return GH_RUN(cmd[1:])


_spec = importlib.util.spec_from_file_location("ship_change_set", HERE / "ship_change_set.py")
scs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scs)
scs.subprocess = types.SimpleNamespace(run=_scs_run)  # its checks and review-threads readers, through GH_RUN
MERGE_TIMEOUT_S = scs.CHECK_TIMEOUT_S + 15 * 60  # one merge run at most: its checks' wait plus 15 min (ruling T-56)


def load_config(path: pathlib.Path) -> dict:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    keys = {"repos": list, "authors": list, "known_flakes": list, "depends_on_check": str}
    if not isinstance(cfg, dict) or any(not isinstance(cfg.get(k), t) for k, t in keys.items()):
        raise SystemExit(f"{path}: needs {', '.join(f'{k} ({t.__name__})' for k, t in keys.items())}")
    cfg["warn"] = scs.warn_checks(path)[0]  # decision 173: one reader, shared with ship_change_set's wait
    cfg["path"] = str(path)
    return cfg


def out_dir(arg: str) -> pathlib.Path:
    ws = os.environ.get("ONEPEEK_WORKSPACE", "")
    if not arg and not ws:
        raise SystemExit("set ONEPEEK_WORKSPACE (the folder holding the repos) or pass --out")
    p = pathlib.Path(arg or pathlib.Path(ws) / ".ship").resolve()
    if any((d / ".git").exists() for d in (p, *p.parents)):
        raise SystemExit(f"{p} is inside a repo; the status files never go in one")
    return p


def listed(value, what: str, keys: tuple[str, ...] = ()) -> list:
    """A list of objects that each hold `keys`, or a ReadError (fail closed on every shape)."""
    if not isinstance(value, list) or any(not isinstance(i, dict) or any(k not in i for k in keys) for i in value):
        raise ReadError(f"{what}: not a list of objects with {', '.join(keys) or 'fields'}")
    return value


def read_prs(cfg: dict, errors: list[str], unread: set[str] | None = None) -> list[dict]:
    """unread: gets each repo whose open pull requests could not be listed."""
    prs, states = [], {}
    for repo in cfg["repos"]:
        try:
            items = listed(json.loads(gh("pr", "list", "-R", f"{ORG}/{repo}", "--state", "open", "--limit",
                                         str(PR_LIMIT), "--json", PR_FIELDS)), "pull requests", tuple(PR_FIELDS.split(",")))
            if len(items) >= PR_LIMIT:
                raise ReadError(f"{PR_LIMIT} or more open pull requests: the list may be cut short")
        except (ReadError, ValueError) as e:
            errors.append(f"{repo}: open pull requests unreadable: {e}")
            if unread is not None:
                unread.add(repo)
            continue
        for i in items:
            prs.append(dict(i, repo=repo, label=f"{repo}#{i['number']}"))
            states[(repo, i["number"])] = "OPEN"
    docs_commits: dict[int, set[str]] = {}
    for p in prs:
        p["deps"] = scs.depends_on.parse(p.get("body"))
        try:
            p["checks"] = listed(scs.checks(p["repo"], p["number"]), "checks", ("name", "bucket"))
            p["all_checks"] = listed(scs.read_checks(p["repo"], p["number"], False), "checks", ("name", "bucket"))
            p["docs_pr"] = None  # N2: a code pull request pinned to an open docs pull request waits a pass
            if p["repo"] not in (scs.DOCS, ".github"):
                pin = scs.branch_pin(p["repo"], p["headRefName"])
                for d in (q for q in prs if q["repo"] == scs.DOCS) if pin else ():
                    if d["number"] not in docs_commits:
                        docs_commits[d["number"]] = scs.pr_commits(scs.DOCS, d["number"])
                    if pin in docs_commits[d["number"]]:
                        p["docs_pr"] = d["label"]
            for d in p["deps"]:
                if d not in states:
                    view = gh("pr", "view", str(d[1]), "-R", f"{ORG}/{d[0]}", "--json", "state")
                    states[d] = json.loads(view)["state"]
            p["dep_states"] = {f"{r}#{n}": states[(r, n)] for r, n in p["deps"]}
        except (SystemExit, ReadError, ValueError, KeyError, TypeError) as e:
            p["error"] = f"unreadable: {e}"
            errors.append(f"{p['label']}: {e}")
    return prs


def classify(p: dict, cfg: dict) -> tuple[str, str]:
    login = (p.get("author") or {}).get("login")
    if p["baseRefName"] == scs.RELEASE_BASE:
        return "HOLD", "a release pull request: it needs the founder's go"
    if p["isDraft"] is not False:
        return "HOLD", "a draft (isDraft is not false)"
    if login not in cfg["authors"]:
        return "HOLD", f"author {login} is not in ship_watch.yaml"
    if p.get("error"):
        return "BLOCKED", p["error"]
    want = scs.BASE.get(p["repo"], "dev")
    if p["baseRefName"] != want:
        return "BLOCKED", f"targets {p['baseRefName']}, not {want}"
    why = scs.own_branch_problem(p["repo"], p["number"], p)
    if why:
        return "BLOCKED", why
    if p["mergeable"] != "MERGEABLE":
        return "BLOCKED", f"mergeable is {p['mergeable']}"
    waiting = [d for d, s in p["dep_states"].items() if s != "MERGED"]
    if waiting:
        return "BLOCKED", f"waiting for {', '.join(waiting)}"
    if p["docs_pr"]:
        return "BLOCKED", f"docs pin is a commit of open {p['docs_pr']}: waits for the pass after it merges"
    hard = [c for c in p["checks"] if not scs.is_warn(c, cfg["warn"], p["repo"])]  # never block (decision 173)
    red = [c["name"] for c in hard if c.get("bucket") not in (*GREEN, "pending")]
    if red:
        return "BLOCKED", f"red: {', '.join(red)}"
    pending = [c["name"] for c in hard if c.get("bucket") == "pending"]
    if pending:
        return "BLOCKED", f"pending: {', '.join(pending)}"
    if not hard:
        return "BLOCKED", "no checks reported"
    if scs.hotfix(p):  # the body read in this pass, never cached; the review comes after the merge
        return "READY", "Lane: hotfix, post-merge review due"
    why = scs.review_problem(p["label"], p["headRefOid"], p.get("author"), p["all_checks"], cfg["reviews"],
                             cfg["authors"])
    if why:
        return "BLOCKED", why
    return "READY", ""


def reruns(p: dict, cfg: dict, done: dict) -> list[tuple[str, str]]:
    """(key, run id) of each red check to re-run once: the depends-on check once every pull request its `Depends on:`
    lines name has merged, or a known flake; never twice on the same head (`done`), never a warn-only check."""
    if p["class"] == "HOLD" or p.get("error"):
        return []
    out = []
    deps_merged = bool(p["deps"]) and all(s == "MERGED" for s in p["dep_states"].values())
    for c in p["checks"]:
        if c.get("bucket") in (*GREEN, "pending") or scs.is_warn(c, cfg["warn"], p["repo"]):
            continue
        key = f"{p['label']}@{p['headRefOid']}:{c['name']}"
        m = re.search(r"/runs/(\d+)", c.get("link") or "")
        if key not in done and m and ((c["name"] == cfg["depends_on_check"] and deps_merged)
                                      or c["name"] in cfg["known_flakes"]):
            out.append((key, m.group(1)))
    return out


def read_ci(cfg: dict, errors: list[str]) -> dict:
    ci: dict = {"online": None, "busy": None, "offline": None, "queued": {}, "sandbox": None, "release_runs": []}
    try:
        rs = [json.loads(ln) for ln in gh("api", "--paginate", f"orgs/{ORG}/actions/runners?per_page=100", "--jq",
                                          ".runners[] | [.status, .busy] | @json").splitlines() if ln.strip()]
        ci["online"] = sum(1 for s, _ in rs if s == "online")
        ci["busy"] = sum(1 for s, b in rs if s == "online" and b)
        ci["offline"] = len(rs) - ci["online"]
        for repo in cfg["repos"]:
            n = int(gh("api", f"repos/{ORG}/{repo}/actions/runs?status=queued&per_page=1", "--jq", ".total_count"))
            if n:
                ci["queued"][repo] = n
        runs = json.loads(gh("run", "list", "-R", f"{ORG}/onepeek-infra", "--workflow", "sandbox.yml", "--limit", "1",
                             "--json", "status,conclusion,url,createdAt,headBranch"))
        ci["sandbox"] = runs[0] if runs else None
        for repo in scs.CHAIN:
            runs = json.loads(gh("run", "list", "-R", f"{ORG}/{repo}", "--workflow", "release.yml", "--limit", "20",
                                 "--json", "status,conclusion,url,createdAt,headBranch"))
            ci["release_runs"] += [dict(r, repo=repo) for r in runs if r.get("status") != "completed"]
    except (ReadError, ValueError, TypeError) as e:
        errors.append(f"CI runners or runs unreadable: {e}")
    return ci


def iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def red_twice(prs: list[dict], reds: dict, warn: list) -> tuple[dict, list[str]]:
    """Each required check red a second time on the same head: red, then not red (or a new job link), then red again.
    Keyed by pull request, head, check name and run, so checks of the same name in other workflows stay apart."""
    out, alerts = {}, []
    for p in prs:
        for c in [] if p.get("error") else [c for c in p["checks"] if not scs.is_warn(c, warn, p["repo"])]:
            run = re.search(r"/runs/(\d+)", c.get("link") or "")
            key = f"{p['label']}@{p['headRefOid']}:{c['name']}:{run.group(1) if run else ''}"
            prev = reds.get(key) if isinstance(reds.get(key), dict) else None
            if c.get("bucket") in (*GREEN, "pending"):
                if prev:
                    out[key] = dict(prev, last="other")
                continue
            count = (prev or {}).get("count", 0)
            if not prev or prev.get("last") != "red" or prev.get("link") != c.get("link"):
                count += 1
            out[key] = {"count": count, "last": "red", "link": c.get("link")}
            if count >= 2:
                alerts.append(f"{p['label']}: required check {c['name']} red twice on {p['headRefOid']}")
    return out, alerts


def hours_between(a: str, b: str) -> float:
    t = [datetime.datetime.fromisoformat(x.replace("Z", "+00:00")) for x in (a, b)]
    return round((t[1] - t[0]).total_seconds() / 3600, 2)


def measure(repo: str, n: int, done: dict) -> dict:
    v = json.loads(gh("pr", "view", str(n), "-R", f"{ORG}/{repo}", "--json", "createdAt,mergedAt,comments"))
    rounds = [int(x) for c in v.get("comments") or [] for x in re.findall(r"\bRound (\d+)\b", c.get("body") or "")]
    bots = gh("api", "--paginate", f"repos/{ORG}/{repo}/pulls/{n}/comments?per_page=100", "--jq",
              '.[] | select(.user.type == "Bot" and .in_reply_to_id == null) | .id').split()
    return {"pr": f"{repo}#{n}", "merged_at": v["mergedAt"],
            "hours_open_to_merge": hours_between(v["createdAt"], v["mergedAt"]),
            "review_rounds": max(rounds) if rounds else None, "bot_threads": len(bots),
            "reruns": sum(1 for k in done if k.startswith(f"{repo}#{n}@"))}


def ship_file(name: str) -> pathlib.Path:
    """A file the other roles write beside reviews.json in $ONEPEEK_WORKSPACE/.ship/."""
    return scs.reviews_file().with_name(name)


def direct_pushes(alerts: list[str], repos: list[str]) -> list[dict]:
    """The entries of direct-pushes.jsonl, each {repo, branch, sha} with branch the repo's trunk and sha 40 hex. A line
    that does not parse or has an odd shape is an alert and is skipped: nothing is reverted on it (fail closed)."""
    path = ship_file("direct-pushes.jsonl")
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    except OSError as e:
        alerts.append(f"direct pushes unreadable, nothing verified or reverted: {path}: {e}")
        return []
    out, seen = [], set()
    for k, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            e = json.loads(line)
            ok = (isinstance(e, dict) and e.get("repo") in repos and isinstance(e.get("sha"), str)
                  and SHA40.fullmatch(e["sha"]) and e.get("branch") == scs.BASE.get(e["repo"], "dev"))
        except ValueError:
            ok = False
        if not ok:
            alerts.append(f"direct pushes line {k} is not {{repo, branch (its trunk), sha}}: not verified or reverted")
        elif (e["repo"], e["sha"]) not in seen:
            seen.add((e["repo"], e["sha"]))
            out.append({"repo": e["repo"], "branch": e["branch"], "sha": e["sha"]})
    return out


def commit_checks(repo: str, sha: str) -> list[dict]:
    """The check runs on one commit as {name, workflow, bucket}, the workflow read from each run's own run, so warn
    checks match as on a pull request. A check not completed is pending; success is pass, skipped is skipping, a red
    run conclusion is fail, and anything else (cancelled, neutral, stale) is pending: it is never reverted on."""
    runs = {str(r["databaseId"]): r["workflowName"] for r in listed(json.loads(gh(
        "run", "list", "-R", f"{ORG}/{repo}", "--commit", sha, "--limit", "100", "--json", "databaseId,workflowName")),
        "runs", ("databaseId", "workflowName"))}
    out = []
    for line in gh("api", "--paginate", f"repos/{ORG}/{repo}/commits/{sha}/check-runs?per_page=100", "--jq",
                   ".check_runs[] | [.name, .status, .conclusion, .details_url] | @json").splitlines():
        if not line.strip():
            continue
        name, status, conclusion, url = json.loads(line)
        run = re.search(r"/runs/(\d+)", url or "")
        bucket = ("pending" if status != "completed" else "pass" if conclusion == "success"
                  else "skipping" if conclusion == "skipped" else "fail" if conclusion in RED_RUN else "pending")
        out.append({"name": name, "workflow": runs.get(run.group(1)) if run else None, "bucket": bucket, "link": url})
    return out


def _revert_push(repo: str, branch: str, sha: str, ws: pathlib.Path) -> str:
    """Revert one red direct push on its trunk: a fresh temporary worktree of $ONEPEEK_WORKSPACE/<repo> at
    origin/<branch>, `git revert --no-edit <sha>`, a plain push, and the remote read back. Refuses a sha that is not on
    the trunk; a trunk that already holds its revert is left alone. Returns what it did; any failure is a ReadError."""
    src = ws / repo
    git(src, "fetch", "-q", "origin", branch)
    try:
        git(src, "merge-base", "--is-ancestor", sha, f"origin/{branch}")
    except ReadError as e:
        raise ReadError(f"{sha} is not on origin/{branch}, not reverted: {e}") from None
    done = git(src, "log", "--format=%H", "--fixed-strings", f"--grep=This reverts commit {sha}", f"origin/{branch}")
    if done.split():
        return f"already reverted by {done.split()[0][:12]}"
    with tempfile.TemporaryDirectory(prefix="ship-watch-revert-") as d:
        wt = pathlib.Path(d) / "wt"
        git(src, "worktree", "add", "-q", "--detach", str(wt), f"origin/{branch}")
        try:
            git(wt, "revert", "--no-edit", sha)
            git(wt, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
            head = git(wt, "rev-parse", "HEAD").strip()
            remote = git(wt, "ls-remote", "origin", f"refs/heads/{branch}").split()
            if remote[:1] != [head]:
                raise ReadError(f"the revert {head[:12]} is not origin/{branch} ({(remote or ['none'])[0][:12]})")
            return f"reverted by {head[:12]}"
        finally:
            try:
                git(src, "worktree", "remove", "--force", str(wt))
            except ReadError:
                pass


REVERT = _revert_push  # the one revert path (tests replace it)


def verify_direct_pushes(cfg: dict, marks: dict, now: float, dry: bool, root: pathlib.Path | None,
                         alerts: list[str], actions: list[str]) -> dict:
    """T-86 partner: settle each direct push in the file once. Returns the marks of the entries still in the file."""
    out = {}
    for e in direct_pushes(alerts, cfg["repos"]):
        key = f"{e['repo']}@{e['sha']}"
        mark = dict(marks[key]) if isinstance(marks.get(key), dict) else {"status": "pending", "since": now}
        out[key] = mark
        if mark.get("status") in ("verified", "reverted"):
            continue
        if not isinstance(mark.get("since"), (int, float)):
            mark["since"] = now
        where = f"direct push {e['repo']}@{e['sha'][:12]} on {e['branch']}"
        try:
            cs = [c for c in commit_checks(e["repo"], e["sha"])
                  if c["name"] != "CodeRabbit" and not scs.is_warn(c, cfg["warn"], e["repo"])]
        except (ReadError, ValueError, KeyError, TypeError) as err:
            alerts.append(f"{where}: CI unreadable, not reverted: {err}")
            continue
        red = sorted({c["name"] for c in cs if c["bucket"] == "fail"})
        if red:
            if dry:
                actions.append(f"would revert {where}: red {', '.join(red)}")
                continue
            try:
                if root is None:
                    raise ReadError("no ONEPEEK_WORKSPACE")
                did = REVERT(e["repo"], e["branch"], e["sha"], root)
            except ReadError as err:
                alerts.append(f"{where} red ({', '.join(red)}); the revert FAILED, revert it by hand: {err}")
                continue
            out[key] = dict(mark, status="reverted", at=now)
            alerts.append(f"{where} red ({', '.join(red)}): {did}")
            actions.append(f"{did}: {where}")
        elif cs and all(c["bucket"] in GREEN for c in cs):
            out[key] = dict(mark, status="verified", at=now)
            actions.append(f"verified {where}")
        elif now - mark["since"] >= DIRECT_PENDING_S:
            alerts.append(f"{where}: CI not settled after {int((now - mark['since']) // 60)} min")
    return out


def reviews_due(alerts: list[str], measures: pathlib.Path) -> list[str]:
    """S2: every pull request merged as a hotfix (measures.jsonl `post_merge_review_due`; a timed-out run only when it
    did merge or its state is unknown) that the orchestrator's post-reviews.json (`{"<repo>#<n>": ...}`) does not name.
    An unreadable file is an alert, and then nothing counts as reviewed (fail closed)."""
    due: list[str] = []
    try:
        rows = measures.read_text(encoding="utf-8").splitlines() if measures.exists() else []
    except OSError as e:
        alerts.append(f"post-merge reviews: {measures} unreadable: {e}")
        rows = []
    for k, line in enumerate(rows, 1):
        try:
            r = json.loads(line) if line.strip() else {}
        except ValueError:
            alerts.append(f"post-merge reviews: {measures.name} line {k} does not parse")
            continue
        if isinstance(r, dict) and r.get("post_merge_review_due") is True and r.get("merged", True) is not False \
                and isinstance(r.get("pr"), str) and r["pr"] not in due:
            due.append(r["pr"])
    path = ship_file("post-reviews.json")
    try:
        done = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(done, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as e:
        alerts.append(f"post-merge reviews: {path} unreadable, every one listed as due: {e}")
        done = {}
    return [d for d in due if d not in done]


def blob(data: bytes) -> str:
    """The git blob id of these bytes (`git hash-object`)."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


LOADED = {n: blob((HERE / n).read_bytes()) for n in CODE}  # what this process runs (T-92)


def reload_check(gh_dir: pathlib.Path) -> tuple[bool, str | None]:
    """(re-execute, alert). Pulls main into the shared .github checkout (fast-forward only, on main only), then compares
    each CODE file's blob on origin/main with the one loaded. It re-executes only when the file it runs from holds the
    new blob, so a watcher started from another folder alerts instead of looping."""
    try:
        branch = git(gh_dir, "symbolic-ref", "--short", "HEAD").strip()
        if branch != "main":
            return False, f"self-update: {gh_dir} is on {branch}, not main; running the loaded code"
        git(gh_dir, "pull", "-q", "--ff-only", "origin", "main")
        new = {n: git(gh_dir, "rev-parse", f"origin/main:workspace/{n}").strip() for n in CODE}
    except ReadError as e:
        return False, f"self-update check failed, running the loaded code: {e}"
    changed = [n for n in CODE if new[n] != LOADED[n]]
    if not changed:
        return False, None
    try:
        stale = [n for n in changed if blob((HERE / n).read_bytes()) != new[n]]
    except OSError as e:
        return False, f"self-update: {HERE} unreadable: {e}"
    if stale:
        return False, (f"{', '.join(changed)} changed on main, but {HERE} does not hold it: restart the watcher from "
                       f"{gh_dir / 'workspace'}")
    return True, None


RELOAD = reload_check  # tests replace it
EXEC = os.execv


def one_pass(cfg: dict, out: pathlib.Path, dry: bool, now: float, root: pathlib.Path | None,
             notes: list[str] | tuple = ()) -> dict:
    """One pass. A pass cut short (a Stop, any exception) after the state was read still writes state.json, with the
    timed-out runs, re-runs and direct-push marks recorded so far (T-97), so none is retried or lost."""
    keep: dict = {}
    finished = False
    try:
        status = _one_pass(cfg, out, dry, now, root, notes, keep)
        finished = True
        return status
    finally:
        if not finished and "state" in keep:
            write(out, None, dict(keep["state"], reruns=keep["done"], timed_out=keep["timed_out"],
                                  direct_pushes=keep["marks"]))


def _one_pass(cfg: dict, out: pathlib.Path, dry: bool, now: float, root: pathlib.Path | None, notes, keep: dict) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    alerts: list[str] = list(notes)
    actions: list[str] = []
    warned: list[dict] = []
    state_file = out / "state.json"
    try:
        state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
        if not isinstance(state, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as e:
        errors.append(f"{state_file} unreadable: {e}")
        state = {}
    try:
        cfg = dict(cfg, reviews=scs.read_reviews())
    except (OSError, ValueError) as e:
        errors.append(f"{scs.reviews_file()} unreadable: {e}")
        cfg = dict(cfg, reviews={})
    seen = state.get("prs") if isinstance(state.get("prs"), dict) else {}
    done = dict(state["reruns"]) if isinstance(state.get("reruns"), dict) else {}
    timed_out = dict(state["timed_out"]) if isinstance(state.get("timed_out"), dict) else {}
    marks = dict(state["direct_pushes"]) if isinstance(state.get("direct_pushes"), dict) else {}
    if not errors:  # a state that could not be read is never written back from a cut-short pass
        keep.update(state=state, done=done, timed_out=timed_out, marks=marks)
    unread: set[str] = set()
    prs = read_prs(cfg, errors, unread)
    ci = read_ci(cfg, errors)
    progress, todo, due = {}, [], []
    for p in prs:
        p["class"], p["reason"] = classify(p, cfg)
        if p["class"] != "HOLD" and timed_out.get(p["label"]) == p.get("headRefOid"):  # not retried every pass (T-56 r2)
            p["class"], p["reason"] = "BLOCKED", f"merge run timed out at {p['headRefOid']}; waits a new head"
        fp = [p["headRefOid"], p["class"], p["reason"]]
        old = seen.get(p["label"]) if isinstance(seen.get(p["label"]), dict) else {}
        since = old["since"] if old.get("fp") == fp and isinstance(old.get("since"), (int, float)) else now
        progress[p["label"]] = {"fp": fp, "since": since}
        if p["class"] != "HOLD" and now - since >= NO_PROGRESS_S:  # HOLD items never raise it (§6.6 rulings)
            alerts.append(f"{p['label']}: no progress for {(now - since) / 3600:.1f} h ({p['class']} {p['reason']})".rstrip())
        for c in [] if p.get("error") else p["checks"]:
            if c.get("bucket") not in (*GREEN, "pending") and scs.is_warn(c, cfg["warn"], p["repo"]):
                run = re.match(r".*/runs/\d+", c.get("link") or "")
                alerts.append(f"sandbox red on {p['label']}: {run.group(0) if run else c.get('link')}")
                warned.append({"pr": p["label"], "check": c["name"], "link": c.get("link")})
        todo += reruns(p, cfg, done)
    reds, twice = red_twice(prs, state.get("reds") if isinstance(state.get("reds"), dict) else {}, cfg["warn"])
    alerts += twice
    no_runner = state.get("no_runner_since") if isinstance(state.get("no_runner_since"), (int, float)) else None
    if ci["online"] is not None:
        no_runner = (no_runner or now) if ci["online"] == 0 and ci["queued"] else None
        if no_runner is not None and now - no_runner >= NO_RUNNER_S:
            alerts.append(f"queued runs with no online runner for {int((now - no_runner) // 60)} min: {ci['queued']}")
    sandbox = ci["sandbox"] or {}
    if sandbox.get("status") == "completed" and sandbox.get("conclusion") in RED_RUN:
        alerts.append(f"sandbox run red ({sandbox['conclusion']}): {sandbox.get('url')}")
    keys = scs.ordered([(p["repo"], p["number"]) for p in prs], "")
    by_key = {(p["repo"], p["number"]): p for p in prs}
    try:
        scs.with_dependencies(keys, {k: by_key[k]["deps"] for k in keys})
    except SystemExit as e:
        alerts.append(f"dependency cycle or order problem: {e}")
    alerts += [f"read failed, nothing merged or re-run this pass: {e}" for e in errors]
    if not errors:
        reran: set[str] = set()
        for key, run_id in todo:
            repo = key.split("#", 1)[0]
            if dry:
                actions.append(f"would re-run {key} (run {run_id})")
                continue
            if run_id in reran:  # one run holding several red checks is re-run once (#71 follow-up)
                done[key] = now
                continue
            try:
                gh("run", "rerun", run_id, "-R", f"{ORG}/{repo}", "--failed")
                reran.add(run_id)
                done[key] = now
                actions.append(f"re-ran {key} (run {run_id})")
            except ReadError as e:
                alerts.append(f"re-run of {key} failed: {e}")
        ready = [k for k in keys if by_key[k]["class"] == "READY"]
        try:
            queue = scs.with_dependencies(ready, {k: by_key[k]["deps"] for k in ready})
        except SystemExit as e:
            queue = []
            alerts.append(f"merge queue not ordered, nothing merged: {e}")
        for repo, n in queue:
            p = by_key[(repo, n)]
            if p["baseRefName"] == scs.RELEASE_BASE or p["isDraft"] is not False:  # defence in depth
                continue
            if dry:
                actions.append(f"would merge {p['label']}")
                continue
            try:
                r = MERGE_RUN([sys.executable, str(HERE / "ship_change_set.py"), p["label"], "--go",
                               "--config", cfg.get("path") or str(HERE / "ship_watch.yaml")], root)
            except MergeTimeout as e:  # killed; the queue goes on with the next pull request
                alerts.append(f"merge run for {p['label']} timed out after {e.args[0]} s")
                timed_out[p["label"]] = p["headRefOid"]
                r, row = None, {"pr": p["label"], "merge_timed_out_after_s": e.args[0],
                                "post_merge_review_due": scs.hotfix(p), "merged": None}
                try:  # it may have merged before it hung (T-56 r2)
                    row["merged"] = json.loads(gh("pr", "view", str(n), "-R", f"{ORG}/{repo}", "--json", "state"))[
                        "state"] == "MERGED"
                except (ReadError, ValueError, KeyError, TypeError) as err:
                    alerts.append(f"{p['label']}: state after the timed-out run unreadable, check it by hand: {err}")
                if row["merged"] and scs.hotfix(p):
                    due.append(p["label"])
            if r is not None and r.returncode != 0:
                last = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-1:] or ["no output"]
                actions.append(f"skipped {p['label']}: {last[0][:300]}")
                continue
            if r is not None:
                actions.append(f"merged {p['label']}" + ("; post-merge review due" if scs.hotfix(p) else ""))
                due += [p["label"]] if scs.hotfix(p) else []
            try:
                if r is not None:
                    row = dict(measure(repo, n, done), post_merge_review_due=scs.hotfix(p))
                with (out / "measures.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row) + "\n")
            except (ReadError, ValueError, KeyError, TypeError, OSError) as e:  # never aborts the pass
                alerts.append(f"{p['label']}: measures unreadable: {e}")
    marks = verify_direct_pushes(cfg, marks, now, dry, root, alerts, actions)
    keep["marks"] = marks
    open_labels = {p["label"] for p in prs}
    reviews = reviews_due(alerts, out / "measures.jsonl")
    status = {"at": iso(now), "mode": "dry-run" if dry else "go", "alerts": alerts, "actions": actions,
              "warned": warned, "ci": ci, "post_merge_review_due": due, "post_merge_reviews_due": reviews,
              "prs": [{k: p.get(k) for k in ("label", "title", "url", "class", "reason", "headRefOid", "baseRefName",
                                              "isDraft", "mergeable", "checks", "dep_states")} for p in prs]}
    new_state = {"prs": progress, "no_runner_since": no_runner, "reds": reds,
                 "reruns": {k: v for k, v in done.items() if k.split("@", 1)[0] in open_labels},
                 "timed_out": {k: v for k, v in timed_out.items()  # kept while its repo is unreadable
                               if k in open_labels or k.rsplit("#", 1)[0] in unread},
                 "direct_pushes": marks}
    write(out, status, new_state)
    return status


def markdown(s: dict) -> str:
    ci, box = s["ci"], s["ci"]["sandbox"] or {}
    lines = [f"# Ship status {s['at']} ({s['mode']})", ""]
    lines += [f"{p['class']:<8} {p['label']}  {p['reason'] or p['title']}" for p in s["prs"]] or ["no open pull requests"]
    lines += ["", f"Runners: {ci['online']} online ({ci['busy']} busy), {ci['offline']} offline; "
                  f"queued runs: {ci['queued'] or 'none'}",
              f"Sandbox: {box.get('conclusion') or box.get('status') or 'none'} {box.get('url', '')}".rstrip(),
              f"Release runs open: {', '.join(r['repo'] + ' ' + r['url'] for r in ci['release_runs']) or 'none'}",
              "", "## Alerts"]
    lines += [f"- {a}" for a in s["alerts"]] or ["none"]
    lines += ["", "## Actions"] + ([f"- {a}" for a in s["actions"]] or ["none"])
    lines += ["", "## Post-merge reviews due"] + ([f"- {d}" for d in s["post_merge_reviews_due"]] or ["none"])
    return "\n".join(lines) + "\n"


def write(out: pathlib.Path, status: dict | None, state: dict) -> None:
    """The status files and state.json; with no status (a pass cut short), state.json alone."""
    files = [("state.json", json.dumps(state, indent=1))]
    if status is not None:
        files[:0] = [("ship-status.json", json.dumps(status, indent=1)), ("ship-status.md", markdown(status))]
    for name, text in files:
        tmp = out / f".{name}.tmp"
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(out / name)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="one pass, then exit (1 when a read failed)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="report what it would merge and re-run; change nothing")
    mode.add_argument("--go", action="store_true", help="merge and re-run (also the default after the first run)")
    ap.add_argument("--out", default="", help="status directory (default $ONEPEEK_WORKSPACE/.ship/)")
    ap.add_argument("--config", default=str(HERE / "ship_watch.yaml"))
    a = ap.parse_args(argv)
    cfg, out = load_config(pathlib.Path(a.config)), out_dir(a.out)
    dry = a.dry_run or (not a.go and not (out / "state.json").exists())
    ws = os.environ.get("ONEPEEK_WORKSPACE", "")
    if not dry and not (ws and (pathlib.Path(ws) / ".github" / ".git").exists()):
        raise SystemExit("merging needs ONEPEEK_WORKSPACE, the folder holding the repos (ship_change_set.py runs there)")
    exit_on_signals()
    gh_dir = pathlib.Path(ws) / ".github" if ws and (pathlib.Path(ws) / ".github" / ".git").exists() else None
    try:
        while True:
            notes = []
            if gh_dir is not None:  # T-92: between passes only, never during a merge run
                go, why = RELOAD(gh_dir)
                if go:
                    print(f"{time.strftime('%H:%M:%S')} the watcher's code changed on main: re-executing", flush=True)
                    EXEC(sys.executable, [sys.executable, str(HERE / "ship_watch.py"), *argv])
                notes += [why] if why else []
            try:
                s = one_pass(cfg, out, dry, time.time(), pathlib.Path(ws) if ws else None, notes)
            except Exception as e:  # noqa: BLE001 - a pass that breaks merges nothing; the loop keeps running
                if a.once:
                    raise
                print(f"{time.strftime('%H:%M:%S')} pass failed, nothing merged: {e!r}", flush=True)
                time.sleep(INTERVAL_S)
                continue
            print(markdown(s), end="", flush=True)
            if a.once:
                return 1 if any(x.startswith("read failed") for x in s["alerts"]) else 0
            time.sleep(INTERVAL_S)
    except Stop as e:  # a merge run in flight was already killed on the way up (_merge_process)
        print(f"{time.strftime('%H:%M:%S')} stopped by signal {e.args[0]}", flush=True)
        return e.code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
