"""Decide an OSV scan (foundation-playbook §12a slice 4 item 1): read osv-scanner's JSON results and the caller's waivers.

Blocks on: a finding of high or critical severity (CVSS 7.0 or more, or the database's HIGH/CRITICAL), or any finding
with an available fix (an affected range that names a `fixed` version). Waivers live in the caller's `osv-scanner.toml`
(osv-scanner itself skips a waived id) as `[[IgnoredVulns]]` with `id`, `ignoreUntil` (a date) and `reason`; a waiver
with no reason, no valid date, or a date in the past fails, so a waiver always ends. A missing results file fails: a
scan that wrote none did not run.

Usage: check_osv_results.py [--results osv-results.json] [--today YYYY-MM-DD] [ROOT]
Exit status 1 when anything blocks.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import tomllib
from pathlib import Path

SKIP_DIRS = {"node_modules", ".git"}
HIGH = 7.0
CRITICAL = 9.0


def waiver_errors(root: Path, today: datetime.date) -> list[str]:
    errors: list[str] = []
    for path in sorted(root.rglob("osv-scanner.toml")):
        if SKIP_DIRS.intersection(path.parts):
            continue
        try:
            doc = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as e:
            errors.append(f"{path}: cannot read the waivers ({type(e).__name__}).")
            continue
        for entry in doc.get("IgnoredVulns") or []:
            who = f"{path}: waiver {entry.get('id', '?')}"
            if not str(entry.get("id") or "").strip():
                errors.append(f"{who}: needs an id.")
            if not str(entry.get("reason") or "").strip():
                errors.append(f"{who}: needs a reason.")
            until = entry.get("ignoreUntil")
            if isinstance(until, datetime.datetime):
                until = until.date()
            if not isinstance(until, datetime.date):
                errors.append(f"{who}: needs an ignoreUntil date (YYYY-MM-DD).")
            elif until < today:
                errors.append(f"{who}: expired on {until}; fix the finding or renew the waiver with a new reason and date.")
    return errors


def _score(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def severity(vuln: dict, group_scores: dict[str, float]) -> str:
    """critical, high or other, from the group's max CVSS score (osv-scanner) or the database's severity word."""
    ids = [vuln.get("id"), *(vuln.get("aliases") or [])]
    score = max([group_scores[i] for i in ids if i in group_scores], default=None)
    word = str((vuln.get("database_specific") or {}).get("severity") or "").upper()
    if word == "CRITICAL" or (score is not None and score >= CRITICAL):
        return "critical"
    if word == "HIGH" or (score is not None and score >= HIGH):
        return "high"
    return "other"


def has_fix(vuln: dict, package: dict) -> bool:
    name = str(package.get("name") or "").lower()
    affected = vuln.get("affected") or []
    mine = [a for a in affected if str((a.get("package") or {}).get("name") or "").lower() == name] or affected
    return any("fixed" in event for a in mine for r in a.get("ranges") or [] for event in r.get("events") or [])


def findings(results: dict) -> list[str]:
    blocked: list[str] = []
    for result in results.get("results") or []:
        source = (result.get("source") or {}).get("path", "?")
        for pkg in result.get("packages") or []:
            package = pkg.get("package") or {}
            scores: dict[str, float] = {}
            for group in pkg.get("groups") or []:
                score = _score(group.get("max_severity"))
                if score is not None:
                    for i in [*(group.get("ids") or []), *(group.get("aliases") or [])]:
                        scores[i] = max(score, scores.get(i, 0.0))
            for vuln in pkg.get("vulnerabilities") or []:
                level, fix = severity(vuln, scores), has_fix(vuln, package)
                if level != "other" or fix:
                    why = ", ".join(filter(None, [level if level != "other" else "", "a fix is available" if fix else ""]))
                    blocked.append(f"{source}: {package.get('name')}@{package.get('version')} {vuln.get('id')} ({why})")
    return blocked


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", type=Path, default=Path("osv-results.json"))
    ap.add_argument("--today", type=datetime.date.fromisoformat, default=datetime.date.today())
    ap.add_argument("root", nargs="?", type=Path, default=Path("."))
    a = ap.parse_args(argv)
    errors = waiver_errors(a.root, a.today)
    if not a.results.is_file():
        errors.append(f"{a.results}: osv-scanner wrote no results, so the scan did not run (is there a lockfile? see the scan step).")
        blocked: list[str] = []
    else:
        blocked = findings(json.loads(a.results.read_text(encoding="utf-8")))
    for line in errors:
        print(f"::error::{line}")
    for line in blocked:
        print(f"::error::{line}: fix it, or waive it in osv-scanner.toml with a reason and an expiry date.")
    if not errors and not blocked:
        print("osv: no blocking findings")
    return 1 if errors or blocked else 0


if __name__ == "__main__":
    sys.exit(main())
