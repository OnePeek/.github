#!/usr/bin/env python3
"""The release order's read side (onepeek-docs architecture/lld/infra/foundation-playbook.md §5.4a "The release order",
"Settled", "The run's GitHub reads and their grants"; development-cycle.md §2c step 4; provisional, 2026-11-05/06).

One function, `settled()`, decides whether an earlier scope is settled for a release. `release-ecs.yml` `prepare`,
`release-lock.yml` `freeze` and `ship_change_set.py`'s `chain_problem` call it, so the merge tool and the run agree.

  release_order.py settled --scope infra --json [--bucket <name>-deploy] [--independent] [--first-seen-file <path>]
  release_order.py check-paths --infra-plan <onepeek-infra .github/workflows/plan.yml>

`settled` prints one JSON object: `verdict` (settled | wait | fail), `reason`, `success_head` (the head of the scope's
latest successful release run, or null) and `error` (true when a read failed: the verdict is then `fail`, never
`settled`). It exits 0 whenever it printed that object; a caller that gets no object treats the scope as not settled.
`check-paths` fails when PLAN_RELEVANT_PATHS and the filter in infra's plan.yml differ in either direction (§5.4a
"Plan-relevant paths, and their drift check").

Reads (only those of §5.4a's table; the App token is GH_TOKEN, `actions: read` and `contents: read`):
  S3 head-object locks/<scope>.json (on a 404, list-objects-v2 of that key's name, to tell a missing bucket from a
  missing key; the same locks/* grant); GET actions/workflows/release.yml/runs?branch=release; GET actions/runs/<id>/jobs
  ?filter=latest; GET commits/release; GET compare/<success head>...<head>; GET commits/<sha> (the merge message).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

OWNER = "OnePeek"
REPOS = {"infra": "onepeek-infra", "backend": "backend-service", "web": "onepeek-web", "mcp": "mcp-athena"}
SCOPE_OF = {repo: scope for scope, repo in REPOS.items()}
INDEPENDENT, AFTER = "release-independent-of", "release-after"
LINE = re.compile(r"^(release-[A-Za-z-]*):\s*(\S.*?)\s*$")
CAP_STEP = "Wait cap reached (re-run)"  # §5.4a rule 2: a run that failed only at this step failed only at a wait cap
QUEUED_AFTER_CAP = datetime.timedelta(minutes=10)  # the merge tool re-runs such a run at once (rule 3)
QUEUED_AFTER_FIRST_SEEN = datetime.timedelta(minutes=5)  # GitHub creates the run a few seconds after the push
COMPARE_FORWARD = ("ahead", "identical")  # any other compare status (behind, diverged, missing) fails, never settles
COMPARE_FILE_LIMIT = 300  # the compare API lists at most 300 files; a compare at that limit counts as plan-relevant
PAGE = 100
MAX_PAGES = 30  # more runs than this is refused (fail closed), never read in part
READ_TIMEOUT = 60  # seconds per read
LIVE_DONE = "completed"
# The plan-relevant paths: the alternatives of the `grep -qE` filter in onepeek-infra .github/workflows/plan.yml
# (`changes` job, `filter` step), copied verbatim; `check-paths` fails when the two differ in either direction.
PLAN_RELEVANT_PATHS = (
    r"\.tf$",
    r"\.tfbackend$",
    r"\.hcl$",
    r"^config/",
    r"^images/",
    r"^policy/",
    r"^scripts/(env_identity|node_images|check_no_release_reads|check_provisioners|check_plan_policy)\.py$",
    r"^\.github/workflows/plan\.yml$",
)
PLAN_RELEVANT = re.compile("(" + "|".join(PLAN_RELEVANT_PATHS) + ")")


class ReadError(RuntimeError):
    """A read failed, timed out or answered something unexpected: never settled."""


class BadLines(ValueError):
    """A merge commit message with a `release-` line that is not one of the two declared forms."""


# --- reads (§5.4a "The run's GitHub reads and their grants"; nothing else is ever called) ---

def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=READ_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise ReadError(f"{cmd[0]} {cmd[1]} timed out after {READ_TIMEOUT} s") from e
    except OSError as e:
        raise ReadError(f"{cmd[0]} cannot run: {e}") from e


def _gh(path: str) -> dict:
    out = _run(["gh", "api", path])
    if out.returncode != 0:
        raise ReadError(f"GET {path}: {(out.stderr or out.stdout).strip()[:300]}")
    try:
        doc = json.loads(out.stdout)
    except ValueError as e:
        raise ReadError(f"GET {path}: not JSON") from e
    if not isinstance(doc, dict):
        raise ReadError(f"GET {path}: not an object")
    return doc


def _paged(path: str, field: str) -> list[dict]:
    """Every item of a paginated list (each page read in full; a page count past MAX_PAGES is refused)."""
    sep = "&" if "?" in path else "?"
    seen: dict = {}
    total = None
    for page in range(1, MAX_PAGES + 1):
        doc = _gh(f"{path}{sep}per_page={PAGE}&page={page}")
        items = doc.get(field)
        if not isinstance(items, list) or not isinstance(doc.get("total_count"), int):
            raise ReadError(f"GET {path}: no {field} list or total_count")
        total = doc["total_count"] if total is None else total
        for item in items:
            if not isinstance(item, dict) or "id" not in item:
                raise ReadError(f"GET {path}: an entry with no id")
            seen.setdefault(item["id"], item)
        if len(items) < PAGE:
            if len(seen) < total:
                raise ReadError(f"GET {path}: read {len(seen)} of {total} {field}")
            return list(seen.values())
    raise ReadError(f"GET {path}: more than {MAX_PAGES * PAGE} {field}; refused rather than read in part")


def _when(value) -> datetime.datetime:
    if not isinstance(value, str):
        raise ReadError(f"a run has no time ({value!r})")
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:
        raise ReadError(f"a run time is not ISO 8601: {value!r}") from e


@dataclass
class Run:
    """A release.yml run on `release` (only the fields settled() reads)."""
    id: int
    head_sha: str
    status: str
    conclusion: str | None
    created: datetime.datetime
    ended: datetime.datetime


class GitHub:
    """The reads of §5.4a's table, through `gh api` (GH_TOKEN) and `aws s3api head-object` (and list-objects-v2 on a 404)."""

    def __init__(self, bucket: str | None = None):
        self.bucket = bucket

    def key_held(self, scope: str) -> bool:
        if not self.bucket:
            raise ReadError("no lock bucket (--bucket or NAME) to read the key from")
        key = f"locks/{scope}.json"
        out = _run(["aws", "s3api", "head-object", "--bucket", self.bucket, "--key", key])
        if out.returncode == 0:
            return True
        err = out.stderr or ""
        if "(NoSuchKey)" in err:
            return False
        if "(404)" not in err:
            raise ReadError(f"s3://{self.bucket}/{key} cannot be read: {err.strip()[:300]}")
        # head-object answers a missing bucket with the same 404 as a missing key: free only once a listing of the key's
        # own name (the locks/* ListBucket grant, §5.4 "Grants") shows the bucket exists and the key is absent.
        listed = _run(["aws", "s3api", "list-objects-v2", "--bucket", self.bucket, "--prefix", key, "--max-keys", "1",
                       "--query", "KeyCount", "--output", "text"])
        if listed.returncode != 0:
            raise ReadError(f"s3://{self.bucket}/{key}: 404, and the bucket cannot be listed: {(listed.stderr or '').strip()[:300]}")
        count = listed.stdout.strip()
        if count == "0":
            return False
        if count.isdigit():
            return True
        raise ReadError(f"s3://{self.bucket}/{key}: 404, and the listing answered {count!r}")

    def release_runs(self, repo: str) -> list[Run]:
        runs = []
        for r in _paged(f"repos/{OWNER}/{repo}/actions/workflows/release.yml/runs?branch=release", "workflow_runs"):
            if not isinstance(r.get("head_sha"), str) or not isinstance(r.get("status"), str):
                raise ReadError(f"{repo} release run {r.get('id')}: no head_sha or status")
            runs.append(Run(r["id"], r["head_sha"], r["status"], r.get("conclusion"), _when(r.get("created_at")),
                            _when(r.get("updated_at"))))
        return sorted(runs, key=lambda r: (r.created, r.id), reverse=True)  # newest first

    def run_jobs(self, repo: str, run_id: int) -> list[dict]:
        return _paged(f"repos/{OWNER}/{repo}/actions/runs/{run_id}/jobs?filter=latest", "jobs")

    def release_head(self, repo: str) -> str:
        sha = _gh(f"repos/{OWNER}/{repo}/commits/release").get("sha")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ReadError(f"{repo}'s release head is not a commit SHA: {sha!r}")
        return sha

    def changed_paths(self, repo: str, base: str, head: str) -> tuple[str, list[str]]:
        """The compare's `status` (ahead, identical, behind, diverged) and its file names."""
        doc = _gh(f"repos/{OWNER}/{repo}/compare/{base}...{head}")
        files = doc.get("files")
        if not isinstance(files, list) or not all(isinstance(f, dict) and isinstance(f.get("filename"), str) for f in files):
            raise ReadError(f"compare {base[:7]}...{head[:7]}: no file list")
        status = doc.get("status")
        return (status if isinstance(status, str) else repr(status)), [f["filename"] for f in files]

    def commit_message(self, repo: str, sha: str) -> str:
        message = (_gh(f"repos/{OWNER}/{repo}/commits/{sha}").get("commit") or {}).get("message")
        if not isinstance(message, str):
            raise ReadError(f"{repo}@{sha[:7]}: the commit has no message")
        return message


# --- the declared lines (§5.4a "Declared lines are read from the merge commit") ---

def declared(message: str) -> dict[str, set[str]]:
    """The `release-independent-of` and `release-after` lines of a merge commit message, each alone on a line; any other
    line starting `release-` fails, so a typo never silently means "no edge"."""
    out: dict[str, set[str]] = {INDEPENDENT: set(), AFTER: set()}
    for raw in message.splitlines():
        line = raw.strip()
        if not line.lower().startswith("release-"):
            continue
        m = LINE.fullmatch(line)
        if not m or m.group(1) not in out:
            raise BadLines(f"not a release order line (only `{INDEPENDENT}: <repo>` and `{AFTER}: <repo>`): {line!r}")
        if m.group(2) not in SCOPE_OF:
            raise BadLines(f"{m.group(1)} names {m.group(2)!r}, which releases nothing")
        out[m.group(1)].add(m.group(2))
    return out


def merge_lines(gh: GitHub, repo: str, sha: str) -> dict[str, set[str]]:
    """The declared lines of the run's own commit, read through the commit API (a missing message is a ReadError)."""
    return declared(gh.commit_message(repo, sha))


# --- settled() ---

def failed_only_at_cap(jobs: list[dict]) -> bool:
    """Rule 3: every failed job has exactly one failed step, named CAP_STEP; any other unsuccessful job means no."""
    failed = 0
    for job in jobs:
        conclusion = job.get("conclusion")
        if conclusion in ("success", "skipped", "neutral"):
            continue
        if conclusion != "failure":
            return False  # cancelled, timed out, still running or unknown: not only a cap
        steps = job.get("steps")
        if not isinstance(steps, list):
            return False
        bad = [s for s in steps if not isinstance(s, dict) or s.get("conclusion") not in ("success", "skipped", "neutral")]
        if len(bad) != 1 or bad[0].get("conclusion") != "failure" or bad[0].get("name") != CAP_STEP:
            return False
        failed += 1
    return failed > 0


@dataclass
class Verdict:
    verdict: str  # settled | wait | fail
    reason: str
    success_head: str | None = None
    error: bool = False

    def as_json(self) -> str:
        return json.dumps({"verdict": self.verdict, "reason": self.reason, "success_head": self.success_head, "error": self.error})


def settled(scope: str, gh: GitHub, independent: bool = False, now: datetime.datetime | None = None,
            first_seen: dict[str, datetime.datetime] | None = None) -> Verdict:
    """Whether the earlier `scope` is settled (§5.4a "Settled"). `independent` is `release-independent-of: onepeek-infra`
    (it lifts only the "unreleased head" part). `first_seen` maps a head to when this check first saw it (its own clock,
    never a GitHub time); it is updated in place, so a polling caller passes the same dict every poll."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    seen = first_seen if first_seen is not None else {}
    if scope not in REPOS:
        return Verdict("fail", f"unknown scope {scope!r}", error=True)
    repo = REPOS[scope]
    success_head = None
    try:
        runs = gh.release_runs(repo)
        success = next((r for r in runs if r.status == LIVE_DONE and r.conclusion == "success"), None)
        success_head = success.head_sha if success else None
        if gh.key_held(scope):
            return Verdict("wait", f"{scope}.json is held", success_head)
        live = [r for r in runs if r.status != LIVE_DONE]
        if live:
            return Verdict("wait", f"a {repo} release run ({live[0].id}) is {live[0].status}", success_head)
        for r in runs:
            # now - ended may be negative under clock skew: that is still inside the 10 minutes (wait, never settled).
            if r.conclusion == "failure" and now - r.ended < QUEUED_AFTER_CAP and failed_only_at_cap(gh.run_jobs(repo, r.id)):
                return Verdict("wait", f"{repo} release run {r.id} failed only at a wait cap; it is re-run at once", success_head)
        if independent and scope == "infra":
            return Verdict("settled", "release-independent-of: onepeek-infra (its key is free and nothing is queued or running)", success_head)
        head = gh.release_head(repo)
        if runs and runs[0].head_sha == head:
            if runs[0].conclusion == "success":
                return Verdict("settled", f"{repo}'s release head {head[:7]} is released (run {runs[0].id})", success_head)
            return Verdict("fail", f"{repo}'s release of {head[:7]} ended {runs[0].conclusion} (run {runs[0].id}): fix forward, "
                                   "or a reviewed release-independent-of line", success_head)
        if any(r.head_sha == head for r in runs):
            return Verdict("fail", f"{repo}'s latest release run is not for its release head {head[:7]}, and nothing is queued or running", success_head)
        seen.setdefault(head, now)
        if now - seen[head] < QUEUED_AFTER_FIRST_SEEN:
            return Verdict("wait", f"{repo}'s release head {head[:7]} has no run yet (first seen {seen[head]:%H:%M:%S}Z); "
                                   "it counts as queued for 5 minutes", success_head)
        if scope != "infra":
            return Verdict("fail", f"{repo}'s release head {head[:7]} has no release run (every app change releases)", success_head)
        if success_head is None:
            return Verdict("fail", f"{repo} has no successful release run to compare its head {head[:7]} with", success_head)
        status, paths = gh.changed_paths(repo, success_head, head)
        if status not in COMPARE_FORWARD:
            # A three-dot compare lists only the head's side of the merge base: behind or diverged hides what the
            # success head deployed, so it never proves that no path changed (ruling R-S2l0-2).
            return Verdict("fail", f"{repo}'s release head {head[:7]} is not a descendant of its last successful release "
                                   f"{success_head[:7]} (compare status {status}): not released", success_head)
        if len(paths) >= COMPARE_FILE_LIMIT:
            return Verdict("fail", f"{repo}: {len(paths)} files changed since {success_head[:7]} (the compare limit): counted as "
                                   "plan-relevant; its release head is not released", success_head)
        relevant = [p for p in paths if PLAN_RELEVANT.search(p)]
        if relevant:
            return Verdict("fail", f"{repo}'s release head {head[:7]} is not released ({relevant[0]} changed since "
                                   f"{success_head[:7]}) and nothing is queued or running", success_head)
        return Verdict("settled", f"{repo}'s changes since {success_head[:7]} skip the release (none plan-relevant)", success_head)
    except ReadError as e:
        return Verdict("fail", f"read error: {e}", success_head, error=True)


# --- the drift check of the plan-relevant paths ---

def plan_filter(plan_yml: str) -> tuple[str, ...]:
    """The top-level alternatives of the one `grep -qE '(...)'` filter in infra's plan.yml."""
    found = re.findall(r"grep -qE '\((.*)\)'", plan_yml)
    if len(found) != 1:
        raise ValueError(f"expected one `grep -qE '(...)'` filter in plan.yml, found {len(found)}")
    parts, depth, cur = [], 0, ""
    for ch in found[0]:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "|" and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return tuple(parts)


def path_drift(plan_yml: str) -> list[str]:
    theirs, ours = set(plan_filter(plan_yml)), set(PLAN_RELEVANT_PATHS)
    return ([f"plan.yml counts {p!r} as plan-relevant; release_order.py does not" for p in sorted(theirs - ours)]
            + [f"release_order.py counts {p!r} as plan-relevant; plan.yml does not" for p in sorted(ours - theirs)])


# --- CLI ---

def _load_seen(path: Path | None) -> dict[str, datetime.datetime]:
    if path is None or not path.exists():
        return {}
    try:
        return {k: datetime.datetime.fromtimestamp(float(v), datetime.timezone.utc) for k, v in json.loads(path.read_text()).items()}
    except (ValueError, AttributeError, TypeError):
        return {}  # an unreadable record restarts the 5 minutes (wait longer, never settle sooner)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("settled")
    s.add_argument("--scope", required=True, choices=sorted(REPOS))
    s.add_argument("--json", action="store_true", required=True)
    s.add_argument("--bucket", default=(os.environ["NAME"] + "-deploy") if os.environ.get("NAME") else None)
    s.add_argument("--independent", action="store_true", help="release-independent-of: onepeek-infra")
    s.add_argument("--first-seen-file", type=Path, help="keeps when each head was first seen, across polls")
    c = sub.add_parser("check-paths")
    c.add_argument("--infra-plan", type=Path, required=True)
    args = ap.parse_args(argv)
    if args.cmd == "check-paths":
        try:
            problems = path_drift(args.infra_plan.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            problems = [f"cannot read the plan filter: {e}"]
        for p in problems:
            print("::error::" + p)
        if not problems:
            print("plan-relevant paths: ok")
        return 1 if problems else 0
    seen = _load_seen(args.first_seen_file)
    verdict = settled(args.scope, GitHub(args.bucket), independent=args.independent, first_seen=seen)
    if args.first_seen_file is not None:
        try:
            args.first_seen_file.write_text(json.dumps({k: v.timestamp() for k, v in seen.items()}))
        except OSError:
            pass
    print(verdict.as_json())
    return 0


if __name__ == "__main__":
    sys.exit(main())
