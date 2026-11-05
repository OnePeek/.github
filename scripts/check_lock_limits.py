#!/usr/bin/env python3
"""The limits test of the release-lock waits (onepeek-docs architecture/lld/infra/foundation-playbook.md §5.4a "Waits
that fit the runners"; ci-runners.md §5.4 rule 3).

  check_lock_limits.py --infra-config <onepeek-infra config/ci.yaml> [--workflows .github/workflows]

Fails when, for any waiting job of the shared workflows:
  - its job limit is below its wait cap plus its other steps' limits, or above its TIMEOUT_EXCEPTIONS entry or its
    class's lint cap;
  - its job limit is above its class's reaper lifetime less 20 minutes, read from onepeek-infra config/ci.yaml
    (runner.max_lifetime_minutes; on-demand for stateful, spot for every other class; the class from the job's runs-on
    mapping, as the lint reads it);
  - the wait cap the workflow sets is not the one in the table, or its re-mint interval is not below the shortest
    credential lifetime it holds (1 hour: the OIDC role session and the App installation token);
  - a job that waits (runs lock_take, lock_finish or lock_cancel) is missing from the table below, which this test holds as data.
Run by this repo's pull requests (workflow-lint service-list, with onepeek-infra at dev as _infra) and by
ship_change_set.py before it merges a pin move of these workflows.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_runner_labels import LIMITS, MAPPING_REPO, TIMEOUT_EXCEPTIONS, classify, timeout_minutes  # noqa: E402

CREDENTIAL_MINUTES = 60  # a deploy role's OIDC session and an App installation token (playbook §5.4a)
BOOT_AND_IDLE = 20  # the reaper lifetime less 20 minutes for boot and an idle machine
WAITING = ("lock_take", "lock_finish", "lock_cancel")
# Every waiting job (playbook §5.4a "Every wait and its limit"): its wait cap, the workflow env that sets it, and the
# sum of its other steps' limits, in minutes.
TABLE: dict[tuple[str, str], dict] = {
    ("release-ecs.yml", "prepare"): {"cap": 120, "cap_env": "LOCK_WAIT_CAP_SECONDS", "other": 15},  # order check, take, version
    ("release-ecs.yml", "finish"): {"cap": 15, "cap_env": "LOCK_MIGRATE_WAIT_SECONDS", "other": 30},  # put-back 20, deployment record 10
    ("operate.yml", "lock"): {"cap": 120, "cap_env": "LOCK_WAIT_CAP_SECONDS", "other": 15},
    ("operate.yml", "unlock"): {"cap": 15, "cap_env": "LOCK_MIGRATE_WAIT_SECONDS", "other": 20},  # put-back 20
    ("release-lock.yml", "lock"): {"cap": 120, "cap_env": "LOCK_WAIT_CAP_SECONDS", "other": 15},
    ("cancel-release.yml", "cancel"): {"cap": 95, "cap_env": "LOCK_WAIT_CAP_SECONDS", "other": 15},  # 75 for the run, 20 put-back
}


def lifetimes(infra_config: Path) -> dict[str, int]:
    """The reaper's machine lifetimes by capacity, from onepeek-infra config/ci.yaml."""
    doc = yaml.safe_load(infra_config.read_text(encoding="utf-8"))
    life = doc["runner"]["max_lifetime_minutes"]
    return {"spot": int(life["spot"]), "on-demand": int(life["on-demand"])}


BLOCK = re.compile(r"# --- release-lock begin.*?# --- release-lock end ---", re.S)


def waiting_jobs(doc: dict) -> list[str]:
    """Jobs with a step that calls a waiting lock function (outside the shared block, which only defines them)."""
    def calls(step: dict) -> bool:
        code = BLOCK.sub("", str(step.get("run", "")))
        return any(re.search(r"(^|[\s;|&(])" + w + r"\b", code) for w in WAITING)
    return [job_id for job_id, job in (doc.get("jobs") or {}).items() if any(calls(s) for s in job.get("steps") or [])]


def check(workflows: Path, life: dict[str, int], table: dict | None = None) -> list[str]:
    table = TABLE if table is None else table
    problems: list[str] = []
    seen: set[tuple[str, str]] = set()
    for path in sorted(workflows.glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        env = doc.get("env") or {}
        for job_id in waiting_jobs(doc):
            key = (path.name, job_id)
            seen.add(key)
            where = f"{path.name} job {job_id}"
            if key not in table:
                problems.append(f"{where} waits for a release lock but is missing from the limits table (playbook §5.4a).")
                continue
            row, job = table[key], doc["jobs"][job_id]
            limit = timeout_minutes(job.get("timeout-minutes"))
            cls = classify(job.get("runs-on", ""), "inputs.runner", True)
            if limit is None or cls is None:
                problems.append(f"{where}: no readable timeout-minutes or runs-on class ({job.get('timeout-minutes')!r}, {cls!r}).")
                continue
            set_cap = env.get(row["cap_env"])
            if str(set_cap) != str(row["cap"] * 60):
                problems.append(f"{where}: {row['cap_env']} is {set_cap!r}, but its wait cap is {row['cap']} minutes ({row['cap'] * 60}).")
            if limit < row["cap"] + row["other"]:
                problems.append(f"{where}: timeout-minutes {limit} is below its wait cap {row['cap']} plus its other steps {row['other']}.")
            exception = TIMEOUT_EXCEPTIONS.get((MAPPING_REPO, path.name, job_id))
            if exception is None:
                problems.append(f"{where}: not a named TIMEOUT_EXCEPTIONS entry (ci-runners.md §5.4 rule 3).")
            elif limit > exception:
                problems.append(f"{where}: timeout-minutes {limit} is above its TIMEOUT_EXCEPTIONS entry {exception}.")
            if limit > LIMITS.get(cls, 0):
                problems.append(f"{where}: timeout-minutes {limit} is above the {cls} lint cap {LIMITS.get(cls)}.")
            lifetime = life["on-demand"] if cls == "stateful" else life["spot"]
            if limit > lifetime - BOOT_AND_IDLE:
                problems.append(f"{where}: timeout-minutes {limit} is above the {cls} reaper lifetime {lifetime} less {BOOT_AND_IDLE}.")
        if waiting_jobs(doc):
            remint = env.get("LOCK_REMINT_SECONDS")
            if remint is None or not str(remint).isdigit() or int(remint) >= CREDENTIAL_MINUTES * 60:
                problems.append(f"{path.name}: LOCK_REMINT_SECONDS {remint!r} is not below the shortest credential lifetime ({CREDENTIAL_MINUTES * 60} s).")
    for key in sorted(set(table) - seen):
        problems.append(f"{key[0]} job {key[1]} is in the limits table but does not wait (or is gone).")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--infra-config", type=Path, required=True, help="onepeek-infra config/ci.yaml")
    ap.add_argument("--workflows", type=Path, default=Path(".github/workflows"))
    args = ap.parse_args(argv)
    problems = check(args.workflows, lifetimes(args.infra_config))
    for p in problems:
        print("::error::" + p)
    if not problems:
        print("release-lock limits: ok")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
