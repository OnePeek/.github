"""The watcher and the merge queue (onepeek-docs standards/agent-operations.md §6.6): a script, no AI, no tokens.

    python3 .github/workspace/ship_watch.py                    # a pass every 3 minutes
    python3 .github/workspace/ship_watch.py --once --dry-run   # one pass that merges nothing
    python3 .github/workspace/ship_watch.py --go               # merge READY pull requests

Each pass reads every open pull request of the repos in ship_watch.yaml (its required checks on the head, `Depends on:`
lines, unresolved review threads, mergeable, draft, release or not), the org's runners against queued runs, the latest
onepeek-infra sandbox.yml run and the open release workflow runs. Each pull request is READY, BLOCKED (one reason) or
HOLD (a release, a draft, or an author not in the allow-list). READY also needs the review gates (§6.6, F36):
CodeRabbit finished on the head and the coordinator's $ONEPEEK_WORKSPACE/.ship/reviews.json naming that head (bots on
the list exempt), except for a pull request whose body's first line is `Lane: hotfix` (operating-model §3,
packet.lane_of; `Lane: docs` keeps the gates): green checks merge it, and its review is due after the merge
(`post_merge_review_due` in ship-status and measures.jsonl). A check is green only when it is `pass` or `skipping`. Every GitHub call goes through one layer that allows reads only. Any read failure, or an odd
shape (a missing field, a non-list, a corrupt state or reviews file), fails the pass closed: an alert says why, nothing
is merged or re-run, and `--once` exits 1; the loop keeps running.

READY ones merge one per `ship_change_set.py --go` run (so one docs pull request per run), docs first, then in the
fixed order; a run that fails is recorded and the queue goes on (F32). A red check is re-run once only when it is the
depends-on check and every pull request its `Depends on:` lines name has merged, or when it is a known flake. The only
writes are those merge-tool runs and `gh run rerun`: it never merges a release, closes a pull request, pushes, or
touches a review thread. While ship_watch.yaml lists `warn_checks` (decision 173), those checks never block READY; a
red one is an alert ("sandbox red on ..."). Without --go or --dry-run, the first run (no state.json yet) is a dry run
and later runs merge; measures.jsonl gets one line per pull request it merged.

It writes ship-status.json, ship-status.md and state.json to --out, default $ONEPEEK_WORKSPACE/.ship/, never inside a
repo.
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
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


class ReadError(Exception):
    pass


def _gh_process(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=False)


class MergeTimeout(Exception):
    pass


def _merge_process(cmd: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess:
    """One merge run, in its own session: past MERGE_TIMEOUT_S its whole process group (gh, git, their children) is
    killed and MergeTimeout raised (ruling T-56). Its own session keeps it out of the terminal's group, so Ctrl-C or any
    other exception in the wait kills the group too, then goes on up: a run never outlives the watcher (T-56 round 2).
    SIGTERM and SIGHUP reach this path as SystemExit (exit_on_signals). A group holding only the exited child's zombie
    refuses the kill on macOS (PermissionError); it has nothing left to kill. Its pipes are never read after the kill, so a stray child holding them cannot hang the watcher."""
    child = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             start_new_session=True)
    try:
        out, err = child.communicate(timeout=MERGE_TIMEOUT_S)
    except BaseException as e:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        child.wait()
        for pipe in (child.stdout, child.stderr):
            pipe.close()
        if isinstance(e, subprocess.TimeoutExpired):
            raise MergeTimeout(MERGE_TIMEOUT_S) from None
        raise
    return subprocess.CompletedProcess(cmd, child.returncode, out, err)


def exit_on_signals() -> None:
    """SIGTERM and SIGHUP raise SystemExit(128 + signal), so a running merge run's group is killed on the way out
    (#82 round 2 review); by default they end the watcher at once and leave the run free to merge."""
    def leave(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)
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
            p["threads"] = scs.unresolved_threads(p["repo"], p["number"])
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
    if scs.hotfix(p):  # the body read in this pass, never cached; threads and review come after the merge
        return "READY", "Lane: hotfix, post-merge review due"
    if p["threads"]:
        return "BLOCKED", f"{len(p['threads'])} unresolved review thread(s): {', '.join(p['threads'])}"
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


def one_pass(cfg: dict, out: pathlib.Path, dry: bool, now: float, root: pathlib.Path | None) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    alerts: list[str] = []
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
            if (p["baseRefName"] == scs.RELEASE_BASE or p["isDraft"] is not False  # defence in depth
                    or (p["threads"] and not scs.hotfix(p))):
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
    open_labels = {p["label"] for p in prs}
    status = {"at": iso(now), "mode": "dry-run" if dry else "go", "alerts": alerts, "actions": actions,
              "warned": warned, "ci": ci, "post_merge_review_due": due,
              "prs": [{k: p.get(k) for k in ("label", "title", "url", "class", "reason", "headRefOid", "baseRefName",
                                              "isDraft", "mergeable", "checks", "threads", "dep_states")} for p in prs]}
    new_state = {"prs": progress, "no_runner_since": no_runner, "reds": reds,
                 "reruns": {k: v for k, v in done.items() if k.split("@", 1)[0] in open_labels},
                 "timed_out": {k: v for k, v in timed_out.items()  # kept while its repo is unreadable
                               if k in open_labels or k.rsplit("#", 1)[0] in unread}}
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
    return "\n".join(lines) + "\n"


def write(out: pathlib.Path, status: dict, state: dict) -> None:
    for name, text in (("ship-status.json", json.dumps(status, indent=1)), ("ship-status.md", markdown(status)),
                       ("state.json", json.dumps(state, indent=1))):
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
    while True:
        try:
            s = one_pass(cfg, out, dry, time.time(), pathlib.Path(ws) if ws else None)
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


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
