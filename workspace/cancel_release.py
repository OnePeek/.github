#!/usr/bin/env python3
"""The one way agents start a release cancel (onepeek-docs architecture/lld/infra/foundation-playbook.md §5.4a
"cancel-release.yml", option A; delivery-pipelines.md UC-DEL-009; provisional, 2026-11-05).

  python3 .github/workspace/cancel_release.py <repo> <env> --reason <text>

It dispatches the repo's cancel-release.yml on `release` with the person's own `gh` login and prints the run link. In an
agent session it refuses environment prod: a prod cancel is the founder's only, and the agents use the founder's login,
so the workflow's actor check cannot tell them apart. The P19 hook (hooks/p19_guard.py) refuses every other way to
dispatch or cancel from an agent session.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import time

ORG = "OnePeek"
REPOS = ("onepeek-infra", "backend-service", "onepeek-web", "mcp-athena")  # the repos that release, each with a caller
ENVIRONMENTS = ("preprod", "prod")
WORKFLOW = "cancel-release.yml"
# Markers an agent tool sets in the shells it starts (Claude Code sets CLAUDECODE and CLAUDE_CODE_ENTRYPOINT).
AGENT_MARKERS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")


def agent_session(env: dict[str, str] | None = None, interactive: bool | None = None) -> bool:
    """True when this runs for an agent: an agent tool's marker is set, or there is no terminal (fails closed)."""
    env = os.environ if env is None else env
    if any(env.get(m) for m in AGENT_MARKERS):
        return True
    if interactive is None:
        interactive = sys.stdin.isatty()
    return not interactive


def gh(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def dispatch(repo: str, env: str, reason: str, wait_seconds: int = 30) -> tuple[int, str]:
    started = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=5)
    res = gh(["workflow", "run", WORKFLOW, "--repo", f"{ORG}/{repo}", "--ref", "release", "-f", f"environment={env}", "-f", f"reason={reason}"])
    if res.returncode != 0:
        return 1, f"cancel-release.yml could not be dispatched in {ORG}/{repo}: {res.stderr.strip()}"
    deadline = time.monotonic() + wait_seconds
    while True:
        listed = gh(["run", "list", "--repo", f"{ORG}/{repo}", "--workflow", WORKFLOW, "--branch", "release", "--event", "workflow_dispatch",
                     "--limit", "5", "--json", "url,createdAt"])  # fmt: skip
        if listed.returncode == 0:
            for r in json.loads(listed.stdout or "[]"):
                created = datetime.datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00"))
                if created >= started:
                    return 0, r["url"]
        if time.monotonic() >= deadline:
            return 0, f"dispatched; the run is not listed yet: https://github.com/{ORG}/{repo}/actions/workflows/{WORKFLOW}"
        time.sleep(3)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("repo", choices=REPOS)
    ap.add_argument("env", choices=ENVIRONMENTS)
    ap.add_argument("--reason", required=True)
    args = ap.parse_args(argv)
    if not args.reason.strip():
        print("REFUSED: --reason must say why.")
        return 2
    if args.env == "prod" and agent_session():
        print("REFUSED: a prod cancel is the founder's only (foundation-playbook §5.4a). Ask the founder to run cancel-release.yml "
              f"in {ORG}/{args.repo} himself; an agent session may cancel preprod only.")
        return 2
    rc, said = dispatch(args.repo, args.env, args.reason)
    print(said)
    return rc


if __name__ == "__main__":
    sys.exit(main())
