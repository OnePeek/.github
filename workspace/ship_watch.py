"""The watcher (onepeek-docs standards/agent-operations.md §6.6): a script, no AI, no tokens. Read-only for now: the
merge queue (merging READY pull requests with ship_change_set.py, and the re-runs) is the next slice.

    python3 .github/workspace/ship_watch.py          # a pass every 3 minutes
    python3 .github/workspace/ship_watch.py --once   # one pass

Each pass reads every open pull request of the repos in ship_watch.yaml (its required checks on the head, `Depends on:`
lines, unresolved review threads, mergeable, draft, release or not), the org's runners against queued runs, the latest
onepeek-infra sandbox.yml run and the open release workflow runs. Each pull request is READY, BLOCKED (one reason) or
HOLD (a release, a draft, or an author not in the allow-list). Every GitHub call goes through one layer that allows
reads only. Any read failure fails the pass closed: an alert says why, and `--once` exits 1.

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


class ReadError(Exception):
    pass


def _gh_process(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=False)


GH_RUN = _gh_process  # the one GitHub call layer (tests replace it)


def allowed(args: list[str]) -> bool:
    """Reads only: never a merge, a close, a push, a re-run, a thread or any other write."""
    if args[:2] in READS:
        return True
    if args[:1] != ["api"] or any(a in ("-X", "--input") or a.startswith(("-X", "--method")) for a in args):
        return False
    fields = [args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-f", "-F", "--field", "--raw-field")]
    if not fields:
        return True  # a GET
    queries = [f[len("query="):].lstrip() for f in fields if f.startswith("query=")]
    return args[1:2] == ["graphql"] and len(queries) == 1 and queries[0].startswith("query")


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


def read_prs(cfg: dict, errors: list[str]) -> list[dict]:
    prs, states = [], {}
    for repo in cfg["repos"]:
        try:
            items = json.loads(gh("pr", "list", "-R", f"{ORG}/{repo}", "--state", "open", "--limit", str(PR_LIMIT),
                                  "--json", PR_FIELDS))
            if len(items) >= PR_LIMIT:
                raise ReadError(f"{PR_LIMIT} or more open pull requests: the list may be cut short")
        except (ReadError, ValueError) as e:
            errors.append(f"{repo}: open pull requests unreadable: {e}")
            continue
        for i in items:
            prs.append(dict(i, repo=repo, label=f"{repo}#{i['number']}"))
            states[(repo, i["number"])] = "OPEN"
    for p in prs:
        p["deps"] = scs.depends_on.parse(p.get("body"))
        try:
            p["checks"] = scs.checks(p["repo"], p["number"])
            p["threads"] = scs.unresolved_threads(p["repo"], p["number"])
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
    if p["isDraft"]:
        return "HOLD", "a draft"
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
    red = [c["name"] for c in p["checks"] if c["bucket"] in ("fail", "cancel")]
    if red:
        return "BLOCKED", f"red: {', '.join(red)}"
    pending = [c["name"] for c in p["checks"] if c["bucket"] == "pending"]
    if pending:
        return "BLOCKED", f"pending: {', '.join(pending)}"
    if not p["checks"]:
        return "BLOCKED", "no checks reported"
    if p["threads"]:
        return "BLOCKED", f"{len(p['threads'])} unresolved review thread(s): {', '.join(p['threads'])}"
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


def one_pass(cfg: dict, out: pathlib.Path, now: float) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    state_file = out / "state.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    seen = state.get("prs") or {}
    errors: list[str] = []
    alerts: list[str] = []
    prs = read_prs(cfg, errors)
    ci = read_ci(cfg, errors)
    progress = {}
    for p in prs:
        p["class"], p["reason"] = classify(p, cfg)
        fp = [p["headRefOid"], p["class"], p["reason"]]
        since = seen[p["label"]]["since"] if seen.get(p["label"], {}).get("fp") == fp else now
        progress[p["label"]] = {"fp": fp, "since": since}
        if now - since >= NO_PROGRESS_S:
            alerts.append(f"{p['label']}: no progress for {(now - since) / 3600:.1f} h ({p['class']} {p['reason']})".rstrip())
    no_runner = state.get("no_runner_since")
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
    new_state = {"prs": progress, "no_runner_since": no_runner}
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
        s = one_pass(cfg, out, time.time())
        print(markdown(s), end="", flush=True)
        if a.once:
            return 1 if any(x.startswith("read failed") for x in s["alerts"]) else 0
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
