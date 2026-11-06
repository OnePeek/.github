"""The watcher and the merge queue (onepeek-docs standards/agent-operations.md §6.6): a script, no AI, no tokens.

    python3 .github/workspace/ship_watch.py                    # a pass every 3 minutes
    python3 .github/workspace/ship_watch.py --once --dry-run   # one pass that merges nothing
    python3 .github/workspace/ship_watch.py --go               # merge READY pull requests

Each pass reads every open pull request of the repos in ship_watch.yaml (its required checks on the head, `Depends on:`
lines, unresolved review threads, mergeable, draft, release or not), the org's runners against queued runs, the latest
onepeek-infra sandbox.yml run and the open release workflow runs. Each pull request is READY, BLOCKED (one reason) or
HOLD (a release, a draft, or an author not in the allow-list). READY ones merge one per `ship_change_set.py --go` run
(so one docs pull request per run), docs first, then in the fixed order; a run that fails is recorded and the queue
goes on (F32). A red check is re-run once only when it is the depends-on check and every pull request its `Depends on:`
lines name has merged, or when it is a known flake. The only write this script makes itself is `gh run rerun`: it never
merges a release, closes a pull request, pushes, or touches a review thread. Any read failure fails the pass closed:
nothing is merged or re-run, and an alert says why. While ship_watch.yaml lists `warn_checks` (decision 173: the sandbox
workflow's, until `warn_until`), those checks never block READY; a red one is an alert ("sandbox red on ..."). Without --go or --dry-run, the first run (no state.json yet) is a
dry run and later runs merge.

It writes ship-status.json, ship-status.md, state.json and measures.jsonl (one line per pull request it merged) to
--out, default $ONEPEEK_WORKSPACE/.ship/, never inside a repo.
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


class ReadError(Exception):
    pass


def _gh_process(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=False)


def _merge_process(cmd: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)


GH_RUN = _gh_process  # the one GitHub call layer (tests replace it)
MERGE_RUN = _merge_process  # ship_change_set.py runs (tests replace it)


def allowed(args: list[str]) -> bool:
    """Reads, plus `run rerun`; never a merge, a close, a push, a thread or any other write."""
    if args[:2] in READS or args[:2] == ["run", "rerun"]:
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
    cfg["warn"] = scs.warn_checks(path)[0]  # decision 173: one reader, shared with ship_change_set's wait
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
    hard = [c for c in p["checks"] if not scs.is_warn(c, cfg["warn"])]  # warn-only checks never block (decision 173)
    red = [c["name"] for c in hard if c["bucket"] in ("fail", "cancel")]
    if red:
        return "BLOCKED", f"red: {', '.join(red)}"
    pending = [c["name"] for c in hard if c["bucket"] == "pending"]
    if pending:
        return "BLOCKED", f"pending: {', '.join(pending)}"
    if not hard:
        return "BLOCKED", "no checks reported"
    if p["threads"]:
        return "BLOCKED", f"{len(p['threads'])} unresolved review thread(s): {', '.join(p['threads'])}"
    return "READY", ""


def reruns(p: dict, cfg: dict, done: dict) -> tuple[list[tuple[str, str]], list[str]]:
    """(key, run id) of each red check to re-run once, and the alerts for checks red again after their re-run."""
    if p["class"] == "HOLD" or p.get("error"):
        return [], []
    out, alerts = [], []
    deps_merged = bool(p["deps"]) and all(s == "MERGED" for s in p["dep_states"].values())
    for c in p["checks"]:
        if c["bucket"] not in ("fail", "cancel"):
            continue
        key = f"{p['label']}@{p['headRefOid']}:{c['name']}"
        if key in done:
            alerts.append(f"{p['label']}: required check {c['name']} red twice")
            continue
        m = re.search(r"/runs/(\d+)", c.get("link") or "")
        if m and ((c["name"] == cfg["depends_on_check"] and deps_merged) or c["name"] in cfg["known_flakes"]):
            out.append((key, m.group(1)))
    return out, alerts


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
    state_file = out / "state.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    done, seen = dict(state.get("reruns") or {}), state.get("prs") or {}
    errors: list[str] = []
    alerts: list[str] = []
    actions: list[str] = []
    warned: list[dict] = []
    prs = read_prs(cfg, errors)
    ci = read_ci(cfg, errors)
    progress, todo = {}, []
    for p in prs:
        p["class"], p["reason"] = classify(p, cfg)
        fp = [p["headRefOid"], p["class"], p["reason"]]
        since = seen[p["label"]]["since"] if seen.get(p["label"], {}).get("fp") == fp else now
        progress[p["label"]] = {"fp": fp, "since": since}
        if now - since >= NO_PROGRESS_S:
            alerts.append(f"{p['label']}: no progress for {(now - since) / 3600:.1f} h ({p['class']} {p['reason']})".rstrip())
        for c in p.get("checks") or []:
            if c["bucket"] in ("fail", "cancel") and scs.is_warn(c, cfg["warn"]):
                run = re.match(r".*/runs/\d+", c.get("link") or "")
                alerts.append(f"sandbox red on {p['label']}: {run.group(0) if run else c.get('link')}")
                warned.append({"pr": p["label"], "check": c["name"], "link": c.get("link")})
        more, red = reruns(p, cfg, done)
        todo += more
        alerts += red
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
    alerts += [f"read failed, nothing merged or re-run this pass: {e}" for e in errors]
    if not errors:
        for key, run_id in todo:
            repo = key.split("#", 1)[0]
            if dry:
                actions.append(f"would re-run {key} (run {run_id})")
                continue
            try:
                gh("run", "rerun", run_id, "-R", f"{ORG}/{repo}", "--failed")
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
            if p["baseRefName"] == scs.RELEASE_BASE or p["isDraft"] or p["threads"]:  # defence in depth
                continue
            if dry:
                actions.append(f"would merge {p['label']}")
                continue
            r = MERGE_RUN([sys.executable, str(HERE / "ship_change_set.py"), p["label"], "--go"], root)
            if r.returncode != 0:
                last = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-1:] or ["no output"]
                actions.append(f"skipped {p['label']}: {last[0][:300]}")
                continue
            actions.append(f"merged {p['label']}")
            try:
                with (out / "measures.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(measure(repo, n, done)) + "\n")
            except (ReadError, ValueError, KeyError, TypeError) as e:
                alerts.append(f"{p['label']}: measures unreadable: {e}")
    open_labels = {p["label"] for p in prs}
    status = {"at": iso(now), "mode": "dry-run" if dry else "go", "alerts": alerts, "actions": actions, "warned": warned, "ci": ci,
              "prs": [{k: p.get(k) for k in ("label", "title", "url", "class", "reason", "headRefOid", "baseRefName",
                                              "isDraft", "mergeable", "checks", "threads", "dep_states")} for p in prs]}
    new_state = {"prs": progress, "no_runner_since": no_runner,
                 "reruns": {k: v for k, v in done.items() if k.split("@", 1)[0] in open_labels}}
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
    while True:
        s = one_pass(cfg, out, dry, time.time(), pathlib.Path(ws) if ws else None)
        print(markdown(s), end="", flush=True)
        if a.once:
            return 1 if any(x.startswith("read failed") for x in s["alerts"]) else 0
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
