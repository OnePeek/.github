"""The watcher (onepeek-docs standards/agent-operations.md §6.6): a script, no AI, no tokens. Read-only for now: the
merge queue (merging READY pull requests with ship_change_set.py, and the re-runs) is the next slice.

    python3 .github/workspace/ship_watch.py          # a pass every 3 minutes
    python3 .github/workspace/ship_watch.py --once   # one pass

Each pass reads every open pull request of the repos in ship_watch.yaml (its required checks on the head, `Depends on:`
lines, unresolved review threads, mergeable, draft, release or not), the org's runners against queued runs, the latest
onepeek-infra sandbox.yml run and the open release workflow runs. Each pull request is READY, BLOCKED (one reason) or
HOLD (a release, a draft, or an author not in the allow-list). READY also needs the review gates (§6.6, F36):
CodeRabbit finished on the head and the coordinator's $ONEPEEK_WORKSPACE/.ship/reviews.json naming that head (bots on
the list exempt). A check is green only when it is `pass` or `skipping`. Every GitHub call goes through one layer that
allows reads only. Any read failure, or an odd shape (a missing field, a non-list, a corrupt state or reviews file),
fails the pass closed: an alert says why, and `--once` exits 1; the loop keeps running.

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


GH_RUN = _gh_process  # the one GitHub call layer (tests replace it)


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
    """Reads only: never a merge, a close, a push, a re-run, a thread or any other write. `gh api` takes fields only
    for a GraphQL query (one `query=query ...`, no `mutation`, no `operationName`)."""
    if args[:2] in READS:
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


def load_config(path: pathlib.Path) -> dict:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    keys = {"repos": list, "authors": list, "known_flakes": list, "depends_on_check": str}
    if not isinstance(cfg, dict) or any(not isinstance(cfg.get(k), t) for k, t in keys.items()):
        raise SystemExit(f"{path}: needs {', '.join(f'{k} ({t.__name__})' for k, t in keys.items())}")
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


def read_prs(cfg: dict, errors: list[str]) -> list[dict]:
    prs, states = [], {}
    for repo in cfg["repos"]:
        try:
            items = listed(json.loads(gh("pr", "list", "-R", f"{ORG}/{repo}", "--state", "open", "--limit",
                                         str(PR_LIMIT), "--json", PR_FIELDS)), "pull requests", tuple(PR_FIELDS.split(",")))
            if len(items) >= PR_LIMIT:
                raise ReadError(f"{PR_LIMIT} or more open pull requests: the list may be cut short")
        except (ReadError, ValueError) as e:
            errors.append(f"{repo}: open pull requests unreadable: {e}")
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
    red = [c["name"] for c in p["checks"] if c.get("bucket") not in (*GREEN, "pending")]
    if red:
        return "BLOCKED", f"red: {', '.join(red)}"
    pending = [c["name"] for c in p["checks"] if c.get("bucket") == "pending"]
    if pending:
        return "BLOCKED", f"pending: {', '.join(pending)}"
    if not p["checks"]:
        return "BLOCKED", "no checks reported"
    if p["threads"]:
        return "BLOCKED", f"{len(p['threads'])} unresolved review thread(s): {', '.join(p['threads'])}"
    why = scs.review_problem(p["label"], p["headRefOid"], p.get("author"), p["all_checks"], cfg["reviews"],
                             cfg["authors"])
    if why:
        return "BLOCKED", why
    return "READY", ""


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


def red_twice(prs: list[dict], reds: dict) -> tuple[dict, list[str]]:
    """Each required check red a second time on the same head: red, then not red (or a new job link), then red again.
    Keyed by pull request, head, check name and run, so checks of the same name in other workflows stay apart."""
    out, alerts = {}, []
    for p in prs:
        for c in [] if p.get("error") else p["checks"]:
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


def one_pass(cfg: dict, out: pathlib.Path, now: float) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    alerts: list[str] = []
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
    prs = read_prs(cfg, errors)
    ci = read_ci(cfg, errors)
    progress = {}
    for p in prs:
        p["class"], p["reason"] = classify(p, cfg)
        fp = [p["headRefOid"], p["class"], p["reason"]]
        old = seen.get(p["label"]) if isinstance(seen.get(p["label"]), dict) else {}
        since = old["since"] if old.get("fp") == fp and isinstance(old.get("since"), (int, float)) else now
        progress[p["label"]] = {"fp": fp, "since": since}
        if p["class"] != "HOLD" and now - since >= NO_PROGRESS_S:  # HOLD items never raise it (§6.6 rulings)
            alerts.append(f"{p['label']}: no progress for {(now - since) / 3600:.1f} h ({p['class']} {p['reason']})".rstrip())
    reds, twice = red_twice(prs, state.get("reds") if isinstance(state.get("reds"), dict) else {})
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
    alerts += [f"read failed: {e}" for e in errors]
    status = {"at": iso(now), "alerts": alerts, "ci": ci,
              "prs": [{k: p.get(k) for k in ("label", "title", "url", "class", "reason", "headRefOid", "baseRefName",
                                              "isDraft", "mergeable", "checks", "threads", "dep_states")} for p in prs]}
    new_state = {"prs": progress, "no_runner_since": no_runner, "reds": reds}
    write(out, status, new_state)
    return status


def markdown(s: dict) -> str:
    ci, box = s["ci"], s["ci"]["sandbox"] or {}
    lines = [f"# Ship status {s['at']}", ""]
    lines += [f"{p['class']:<8} {p['label']}  {p['reason'] or p['title']}" for p in s["prs"]] or ["no open pull requests"]
    lines += ["", f"Runners: {ci['online']} online ({ci['busy']} busy), {ci['offline']} offline; "
                  f"queued runs: {ci['queued'] or 'none'}",
              f"Sandbox: {box.get('conclusion') or box.get('status') or 'none'} {box.get('url', '')}".rstrip(),
              f"Release runs open: {', '.join(r['repo'] + ' ' + r['url'] for r in ci['release_runs']) or 'none'}",
              "", "## Alerts"]
    lines += [f"- {a}" for a in s["alerts"]] or ["none"]
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
    ap.add_argument("--out", default="", help="status directory (default $ONEPEEK_WORKSPACE/.ship/)")
    ap.add_argument("--config", default=str(HERE / "ship_watch.yaml"))
    a = ap.parse_args(argv)
    cfg, out = load_config(pathlib.Path(a.config)), out_dir(a.out)
    while True:
        try:
            s = one_pass(cfg, out, time.time())
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
