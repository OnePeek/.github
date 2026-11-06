#!/usr/bin/env python3
"""The caller workflow test for release skips (onepeek-docs architecture/lld/infra/delivery-pipelines.md §8.1,
"Slice 2 rulings" S1 to S7).

A promotion pull request (dev -> release) skips the tree-only jobs, and `release-gate` proves that is safe only if
the caller's checks.yml is shaped right. In a release repo (RELEASE_REPOS) this checks:
  - every checks.yml job other than `certified` is release-specific (S3), or carries the canonical skip condition as
    its whole `if` or one top-level `&&` operand (S4) and is in `certified.needs`;
  - `certified.needs` is exactly the jobs that are not release-specific, minus `certified` (S5);
  - `certified`'s `if` is exactly the canonical certification condition (S6: the string only);
  - every release-please action step, in any workflow file, passes `token: ${{ steps.<id>.outputs.token }}`, where
    <id> is an earlier step of the same job using actions/create-github-app-token (S7: the step shape only).
Any other repo passes with a notice. Anything unreadable fails (fail closed).

    python3 check_release_skips.py --repo OnePeek/<repo> [<repo dir>]

Exit 0 when every rule holds, 1 otherwise. Needs PyYAML.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

import yaml

RELEASE_REPOS = ("backend-service", "onepeek-web", "onepeek-infra")  # S2; mcp-athena joins when it releases
RELEASE_SPECIFIC = ("depends-on", "pins", "release-gate", "contract")  # S3, every release repo
RELEASE_SPECIFIC_BY_REPO = {"backend-service": ("release-checks",), "onepeek-web": ("no-prerelease-deps",)}
CERTIFIED = "certified"
SKIP = "!(github.event_name == 'pull_request' && github.base_ref == 'release' && github.head_ref == 'dev')"
CERTIFIED_IF = ("always() && github.ref == 'refs/heads/dev' && "
                "(github.event_name == 'push' || github.event_name == 'workflow_dispatch')")
RELEASE_PLEASE = "googleapis/release-please-action@"
APP_TOKEN = "actions/create-github-app-token@"
TOKEN_RE = re.compile(r"\$\{\{ steps\.([A-Za-z_][A-Za-z0-9_-]*)\.outputs\.token \}\}", re.ASCII)


def normalize(expr: str) -> str:
    """The `${{ }}` removed and whitespace collapsed to one space (S4)."""
    s = expr.strip()
    if s.startswith("${{") and s.endswith("}}"):
        s = s[3:-2]
    return " ".join(s.split())


def top_level_and_operands(expr: str) -> list[str] | None:
    """The top-level `&&` operands of a normalized expression, or None when it cannot be read as an `&&` chain:
    unbalanced parentheses or quotes, or a top-level `||` (`||` binds looser, so `a || b && c` is not an `&&` chain)."""
    parts: list[str] = []
    depth = 0
    quoted = False
    start = 0
    i = 0
    while i < len(expr):
        ch = expr[i]
        if ch == "'":
            quoted = not quoted
        elif not quoted:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth < 0:
                    return None
            elif depth == 0 and expr.startswith("||", i):
                return None
            elif depth == 0 and expr.startswith("&&", i):
                parts.append(expr[start:i].strip())
                i += 2
                start = i
                continue
        i += 1
    if depth != 0 or quoted:
        return None
    parts.append(expr[start:].strip())
    return parts


def carries_skip(job_if: object) -> bool:
    """S4: the job's `if` is exactly the skip condition, or has it as one top-level `&&` operand."""
    if not isinstance(job_if, str):
        return False
    expr = normalize(job_if)
    if expr == SKIP:
        return True
    operands = top_level_and_operands(expr)
    return operands is not None and SKIP in operands


def _needs(job: dict) -> list[str] | None:
    n = job.get("needs", [])
    if isinstance(n, str):
        return [n]
    if isinstance(n, list) and all(isinstance(x, str) for x in n):
        return n
    return None


def check_checks(repo: str, data: object) -> list[str]:
    """The checks.yml rules (S3 to S6)."""
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), dict):
        return ["checks.yml: no jobs"]
    jobs: dict = data["jobs"]
    if not all(isinstance(j, dict) for j in jobs.values()):
        return ["checks.yml: a job that is not a mapping"]
    specific = set(RELEASE_SPECIFIC) | set(RELEASE_SPECIFIC_BY_REPO.get(repo, ()))
    errors: list[str] = []
    cert = jobs.get(CERTIFIED)
    if cert is None:
        return [f"checks.yml: no `{CERTIFIED}` job (§8.1 Certification)"]
    needs = _needs(cert)
    if needs is None:
        errors.append(f"checks.yml: `{CERTIFIED}.needs` is not a job name or a list of them")
        needs = []
    if CERTIFIED in needs:
        errors.append(f"checks.yml: `{CERTIFIED}` lists itself in its needs")
    for name, job in jobs.items():
        if name == CERTIFIED or name in specific:
            continue
        if not carries_skip(job.get("if")):
            errors.append(f"checks.yml: job `{name}` is not release-specific and its `if` does not carry the skip "
                          f"condition {SKIP} (as the whole `if` or one top-level `&&` operand)")
        if name not in needs:
            errors.append(f"checks.yml: job `{name}` is tree-only but not in `{CERTIFIED}.needs`")
    expected = set(jobs) - specific - {CERTIFIED}
    extra = sorted(set(needs) - expected - {CERTIFIED})
    if extra:
        errors.append(f"checks.yml: `{CERTIFIED}.needs` holds jobs that are not tree-only: {', '.join(extra)}")
    cert_if = cert.get("if")
    if not isinstance(cert_if, str) or normalize(cert_if) != CERTIFIED_IF:
        errors.append(f"checks.yml: `{CERTIFIED}`'s `if` must be exactly ${{{{ {CERTIFIED_IF} }}}}")
    return errors


def check_release_please(name: str, data: object) -> list[str]:
    """S7: every release-please action step takes its token from an earlier App token step of the same job."""
    if not isinstance(data, dict):
        return [f"{name}: not a workflow"]
    jobs = data.get("jobs")
    if not isinstance(jobs, dict):
        return []
    errors: list[str] = []
    for job_name, job in jobs.items():
        steps = job.get("steps") if isinstance(job, dict) else None
        if not isinstance(steps, list):
            continue
        for i, step in enumerate(steps):
            if not isinstance(step, dict) or not str(step.get("uses", "")).startswith(RELEASE_PLEASE):
                continue
            token = (step.get("with") or {}).get("token") if isinstance(step.get("with") or {}, dict) else None
            m = TOKEN_RE.fullmatch(token) if isinstance(token, str) else None
            minted = m and any(isinstance(s, dict) and s.get("id") == m.group(1)
                               and str(s.get("uses", "")).startswith(APP_TOKEN) for s in steps[:i])
            if not minted:
                errors.append(f"{name}: job `{job_name}`: the release-please step must pass "
                              "token: ${{ steps.<id>.outputs.token }} from an earlier "
                              "actions/create-github-app-token step (§8.1 release-please)")
    return errors


def _load(path: pathlib.Path) -> object:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def check_repo(repo: str, root: pathlib.Path) -> tuple[list[str], str]:
    """(errors, notice) for the repo at root."""
    name = repo.rsplit("/", 1)[-1]
    if name not in RELEASE_REPOS:
        return [], f"{name} has no release branch: nothing to check"
    wf_dir = root / ".github" / "workflows"
    errors: list[str] = []
    checks = wf_dir / "checks.yml"
    try:
        errors += check_checks(name, _load(checks))
    except (OSError, yaml.YAMLError, UnicodeDecodeError) as e:
        errors.append(f"checks.yml: cannot read it: {e}")
    try:
        files = sorted(p for p in wf_dir.iterdir() if p.suffix in (".yml", ".yaml"))
    except OSError as e:
        return errors + [f"{wf_dir}: cannot list it: {e}"], ""
    for path in files:
        try:
            errors += check_release_please(path.name, _load(path))
        except (OSError, yaml.YAMLError, UnicodeDecodeError) as e:
            errors.append(f"{path.name}: cannot read it: {e}")
    return errors, ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, help="OnePeek/<repo> or <repo>")
    ap.add_argument("root", nargs="?", default=".", help="the repo's checkout (default: .)")
    args = ap.parse_args(argv)
    errors, notice = check_repo(args.repo, pathlib.Path(args.root))
    if notice:
        print(f"::notice::release skips: {notice}")
    for e in errors:
        print(f"::error::release skips: {e}")
    if errors:
        return 1
    print(f"release skips: {args.repo} OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
