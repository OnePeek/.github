#!/usr/bin/env python3
"""lock-status: who holds each release lock of an environment (onepeek-docs architecture/lld/infra/foundation-playbook.md
§5.4a "lock-status"; provisional, 2026-11-05).

  python3 .github/workspace/lock_status.py <env> [--aws-profile <profile>]

Read-only, as the Identity Center permission set LockStatusReader: `list-objects-v2` on the prefix locks/ of
s3://onepeek-<env>-deploy, and `get-object` of each key into a temporary file (a new private directory, mode 0600) that
is deleted at the end. For each scope (infra, backend, web, mcp) it prints `free`, or the holder's repo, run link, commit,
phase, age, any repair_run, and the services in `previous` when the phase is rolling; for frozen.json who froze, the
branch, infra_head, infra_settled and the age; during the move off release.json, release.json too. It prints a fixed list
of fields, never the raw object, so never the token. It fails closed: a key it cannot read is an error, never `free`.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCOPES = ("infra", "backend", "web", "mcp")
FROZEN, LEGACY = "locks/frozen.json", "locks/release.json"
ENVIRONMENTS = ("preprod", "prod")
# The only fields ever printed (cancel-release.yml prints the same ones in its run summary).
LOCK_FIELDS = ("repo", "run", "commit", "phase", "age", "repair_run", "previous")
FROZEN_FIELDS = ("who", "branch", "infra_head", "infra_settled", "age")


class Unreadable(Exception):
    """A key that could not be listed or read: never reported as free."""


def aws(args: list[str], profile: str | None) -> str:
    cmd = ["aws", *args, *(["--profile", profile] if profile else [])]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise Unreadable(f"{' '.join(args[:2])}: {res.stderr.strip() or res.returncode}")
    return res.stdout


def age(started: object, now: datetime.datetime) -> str:
    try:
        then = datetime.datetime.fromisoformat(str(started).replace("Z", "+00:00"))
    except ValueError:
        return "?"
    return str(now - then).split(".")[0]


def lock_line(record: dict, now: datetime.datetime) -> dict[str, str]:
    fields = {"repo": str(record.get("repo", "?")), "run": str(record.get("run", "?")), "commit": str(record.get("commit", "?")),
              "phase": str(record.get("phase", "locked")), "age": age(record.get("started"), now),
              "repair_run": str(record.get("repair_run") or "-")}  # fmt: skip
    if fields["phase"] == "rolling":
        fields["previous"] = ", ".join(sorted((record.get("previous") or {}).keys())) or "-"
    return {k: fields[k] for k in LOCK_FIELDS if k in fields}


def frozen_line(record: dict, now: datetime.datetime) -> dict[str, str]:
    fields = {"who": str(record.get("who", "?")), "branch": f"{record.get('repo', '?')} {record.get('ref', '?')}",
              "infra_head": str(record.get("infra_head", "-")), "infra_settled": str(record.get("infra_settled", "-")),
              "age": age(record.get("started"), now)}  # fmt: skip
    return {k: fields[k] for k in FROZEN_FIELDS}


def status(env: str, profile: str | None, now: datetime.datetime | None = None) -> tuple[list[str], int]:
    """The lines to print and the exit status (1 when any key could not be read)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    bucket = f"onepeek-{env}-deploy"
    lines: list[str] = []
    rc = 0
    try:
        listing = json.loads(aws(["s3api", "list-objects-v2", "--bucket", bucket, "--prefix", "locks/", "--output", "json"], profile) or "{}")
    except (Unreadable, ValueError) as error:
        return [f"ERROR locks/ in s3://{bucket} cannot be listed ({error}); nothing is reported free."], 1
    present = {o["Key"] for o in listing.get("Contents") or []}
    wanted = [f"locks/{s}.json" for s in SCOPES] + [FROZEN] + ([LEGACY] if LEGACY in present else [])
    folder = tempfile.mkdtemp(prefix="lock-status-")   # mode 0700: only this user can read the records
    try:
        for key in wanted:
            if key not in present:
                lines.append(f"{key}: free" if key != FROZEN else f"{key}: not frozen")
                continue
            path = Path(folder) / key.split("/")[-1]
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                os.close(fd)
                aws(["s3api", "get-object", "--bucket", bucket, "--key", key, str(path)], profile)
                record = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(record, dict):
                    raise ValueError("not a record")
            except (Unreadable, ValueError, OSError) as error:
                lines.append(f"ERROR {key}: cannot be read ({type(error).__name__}); it is not free.")
                rc = 1
                continue
            fields = frozen_line(record, now) if key == FROZEN else lock_line(record, now)
            lines.append(f"{key}: " + "; ".join(f"{k} {v}" for k, v in fields.items()))
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return lines, rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("env", choices=ENVIRONMENTS)
    ap.add_argument("--aws-profile")
    args = ap.parse_args(argv)
    lines, rc = status(args.env, args.aws_profile)
    for line in lines:
        print(line)
    return rc


if __name__ == "__main__":
    sys.exit(main())
