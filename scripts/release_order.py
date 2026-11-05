"""The release order (onepeek-docs architecture/lld/infra/foundation-playbook.md §5.4a "The release order";
development-cycle.md §2c step 4; provisional, 2026-11-05).

One function, `settled()`, decides whether an earlier scope is settled for a release. `ship_change_set.py`'s chain check
calls it (and the release run's order check is to call the same function), so the merge tool and the run agree.

Edges (from a scope to an earlier one):
  every app scope -> infra   always; `release-independent-of: onepeek-infra` lifts only the "unreleased head" part;
  web -> backend, mcp -> backend   only with `release-after: backend-service`.
The lines are read from the merge commit message, never from a pull request body; any other line starting `release-`
fails, so a typo never silently means "no edge".
"""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from typing import Callable

OWNER = {"infra": "onepeek-infra", "backend": "backend-service", "web": "onepeek-web", "mcp": "mcp-athena"}
SCOPE = {repo: scope for scope, repo in OWNER.items()}
INDEPENDENT, AFTER = "release-independent-of", "release-after"
LINE = re.compile(r"^(release-[A-Za-z-]*):\s*(\S.*?)\s*$")
CAP_MESSAGE = "waiting for "
QUEUED_AFTER_CAP = datetime.timedelta(minutes=10)  # a run that failed only at a wait cap is re-run at once by the merge tool
QUEUED_AFTER_PUSH = datetime.timedelta(minutes=5)  # GitHub creates the run a few seconds after the push
# The paths infra's plan.yml `changes` job counts as plan-relevant (onepeek-infra .github/workflows/plan.yml, dev,
# 2026-11-05); a head whose changes since the last successful release touch none of them skips the release.
PLAN_RELEVANT = re.compile(r"(\.tf$|\.tfbackend$|\.hcl$|^config/|^images/|^policy/|^scripts/(env_identity|node_images|"
                           r"check_no_release_reads|check_provisioners|check_plan_policy)\.py$|^\.github/workflows/plan\.yml$)")


class BadLines(ValueError):
    """A merge commit message with a `release-` line that is not one of the two declared forms."""


def declared(message: str) -> dict[str, set[str]]:
    """The `release-independent-of` and `release-after` lines of a merge commit message, each alone on a line."""
    out: dict[str, set[str]] = {INDEPENDENT: set(), AFTER: set()}
    for raw in message.splitlines():
        line = raw.strip()
        if not line.lower().startswith("release-"):
            continue
        m = LINE.fullmatch(line)
        if not m or m.group(1) not in out:
            raise BadLines(f"not a release order line (only `{INDEPENDENT}: <repo>` and `{AFTER}: <repo>`): {line!r}")
        repo = m.group(2)
        if repo not in SCOPE:
            raise BadLines(f"{m.group(1)} names {repo!r}, which releases nothing")
        out[m.group(1)].add(repo)
    return out


def edges(scope: str, lines: dict[str, set[str]]) -> list[str]:
    """The earlier scopes this release waits for."""
    if scope == "infra":
        return []
    out = ["infra"]
    if scope in ("web", "mcp") and OWNER["backend"] in lines.get(AFTER, set()):
        out.append("backend")
    return out


def body_lines(body: str) -> list[str]:
    """The release order lines of a reviewed pull request body, as ship_change_set.py copies them into the merge commit."""
    lines = [raw.strip() for raw in (body or "").splitlines() if raw.strip().lower().startswith("release-")]
    declared("\n".join(lines))  # the same rule as the run: a malformed line stops the merge
    return lines


@dataclass
class Run:
    """A release.yml run of an earlier scope's repo on `release`."""
    head_sha: str
    status: str  # queued, in_progress, waiting, requested, pending, completed
    conclusion: str | None = None
    ended: datetime.datetime | None = None
    failed_at_cap: bool = False  # every failed job's error annotation starts "waiting for "


@dataclass
class Facts:
    """What settled() reads about one earlier scope (fakeable)."""
    key_held: bool
    runs: list[Run] = field(default_factory=list)  # newest first
    head: str = ""
    head_pushed: datetime.datetime | None = None
    last_success_head: str | None = None  # the head of the scope's latest successful release run
    changed_since_success: Callable[[], list[str]] | None = None  # infra: the paths changed since that head


def settled(scope: str, facts: Facts, independent: bool = False, now: datetime.datetime | None = None) -> tuple[str, str]:
    """('settled' | 'wait' | 'fail', why) for the earlier `scope`. `independent` is `release-independent-of: onepeek-infra`."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    repo = OWNER[scope]
    if facts.key_held:
        return "wait", f"{repo}'s key ({scope}.json) is held"
    live = [r for r in facts.runs if r.status != "completed"]
    if live:
        return "wait", f"a {repo} release run is {live[0].status}"
    capped = [r for r in facts.runs if r.conclusion == "failure" and r.failed_at_cap and r.ended and now - r.ended < QUEUED_AFTER_CAP]
    if capped:
        return "wait", f"a {repo} release run failed at a wait cap and is about to be re-run"
    if independent and scope == "infra":
        return "settled", "release-independent-of: onepeek-infra (nothing queued or running, the key free)"
    for_head = [r for r in facts.runs if r.head_sha == facts.head]
    if for_head:
        latest = for_head[0]
        if latest.conclusion == "success":
            return "settled", f"{repo}'s release head {facts.head[:7]} is released"
        return "fail", f"{repo}'s release of {facts.head[:7]} ended {latest.conclusion}: fix forward, or a reviewed release-independent-of line"
    if facts.head_pushed and now - facts.head_pushed < QUEUED_AFTER_PUSH:
        return "wait", f"{repo}'s release head {facts.head[:7]} was pushed moments ago; its run is about to start"
    if scope == "infra" and facts.last_success_head and facts.changed_since_success is not None:
        relevant = [p for p in facts.changed_since_success() if PLAN_RELEVANT.search(p)]
        if not relevant:
            return "settled", f"{repo}'s changes since {facts.last_success_head[:7]} skip the release (none plan-relevant)"
        return "fail", f"{repo}'s release head {facts.head[:7]} is not released ({relevant[0]} changed) and nothing is running"
    return "fail", f"{repo}'s release head {facts.head[:7]} is not released and nothing is queued or running"
