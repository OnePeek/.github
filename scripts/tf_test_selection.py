"""Choose which `terraform test` suites a pull request must run (used by .github/workflows/terraform-checks.yml).

A suite is a directory with a `*.tftest.hcl` file: the folder above its `tests/` folder (any depth under tests/), else
the folder holding the file. On a pull request into `dev` only the suites whose directory (or whose tests/) changed run,
plus the suite of the AWS stack (stacks/aws) when the change could reach it: anything under stacks/aws, config/, policy/, scripts/ or
modules/aws (any module may be consumed by the stack, so any module change counts). Release pull requests (base
`release`), pushes and schedules run every suite.

  python3 tf_test_selection.py --event pull_request --base-ref dev --diff-against origin/dev   # one suite per line
Prints nothing and exits 0 when no suite is selected; exits 1 when the repository has no suite at all.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path, PurePosixPath

STACK = "stacks/aws"
STACK_TRIGGERS = ("stacks/aws", "config", "policy", "scripts", "modules/aws")


def suite_dir(path: str) -> str:
    """The suite a *.tftest.hcl file belongs to."""
    p = PurePosixPath(path).parent
    parts = p.parts
    if "tests" in parts:
        p = PurePosixPath(*parts[: parts.index("tests")]) if parts.index("tests") else PurePosixPath(".")
    return p.as_posix()


def find_suites(root: Path) -> list[str]:
    found = {suite_dir(f.relative_to(root).as_posix()) for f in root.rglob("*.tftest.hcl") if ".terraform" not in f.parts}
    return sorted(found)


def under(path: str, directory: str) -> bool:
    return directory == "." or path == directory or path.startswith(directory.rstrip("/") + "/")


def select(suites: list[str], changed: list[str], event: str, base_ref: str) -> list[str]:
    """The suites to run. All of them unless this is a pull request into dev."""
    if event != "pull_request" or base_ref != "dev":
        return sorted(suites)
    chosen = {s for s in suites if any(under(c, s) for c in changed)}
    if STACK in suites and any(under(c, t) for c in changed for t in STACK_TRIGGERS):
        chosen.add(STACK)
    return sorted(chosen)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", required=True)
    ap.add_argument("--base-ref", default="")
    ap.add_argument("--diff-against", default="", help="git ref of the base branch, e.g. origin/dev")
    ap.add_argument("--root", default=".")
    args = ap.parse_args(argv)
    root = Path(args.root)
    suites = find_suites(root)
    if not suites:
        print("no *.tftest.hcl found: this check would test nothing", file=sys.stderr)
        return 1
    changed: list[str] = []
    if args.event == "pull_request" and args.base_ref == "dev":
        if not args.diff_against:
            print("--diff-against is required for a pull request into dev", file=sys.stderr)
            return 1
        out = subprocess.run(["git", "-C", str(root), "diff", "--name-only", args.diff_against, "HEAD"],
                             check=True, capture_output=True, text=True).stdout
        changed = [line for line in out.splitlines() if line]
    for s in select(suites, changed, args.event, args.base_ref):
        print(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
