"""Build a task packet for any repo, from onepeek-docs at the commit in that repo's docs-version.txt.

Run from inside a repo (the backend has the same thing as `uv run app context`):

    python3 ../.github/workspace/packet.py clients/onepeek-web --out .context/onepeek-web.md
    python3 ../.github/workspace/packet.py infra/aws-stack --out .context/aws-stack.md
    python3 ../.github/workspace/packet.py mcp-gateway --refine

It makes a temporary detached worktree of ../onepeek-docs at the pinned commit, runs that commit's tools/context.py, and
always removes the worktree. The packet therefore matches the docs the repo says it implements, never whatever happens
to be checked out. Standard library only; argument lists only (no shell).
"""

from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile


def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("unit", help="module ID, platform/<part>, clients/<client> or infra/<part>")
    ap.add_argument("--refine", action="store_true", help="packet for refining a minimum module LLD")
    ap.add_argument("--out", type=pathlib.Path, help="write the packet to this file instead of stdout")
    ap.add_argument("--repo", type=pathlib.Path, default=pathlib.Path.cwd(), help="the repo (default: current folder)")
    a = ap.parse_args()

    pin_file = a.repo / "docs-version.txt"
    pin = re.search(r"onepeek-docs@([0-9a-f]{40})", pin_file.read_text()) if pin_file.exists() else None
    if not pin:
        print(f"{pin_file}: no full onepeek-docs commit SHA (write onepeek-docs@<40-char sha>)", file=sys.stderr)
        return 1
    docs = (a.repo / ".." / "onepeek-docs").resolve()
    if not (docs / ".git").exists():
        print(f"{docs} is missing: git clone https://github.com/OnePeek/onepeek-docs.git {docs}", file=sys.stderr)
        return 1
    sha = pin.group(1)
    if run(["git", "-C", str(docs), "cat-file", "-e", f"{sha}:tools/context.py"]).returncode != 0:
        print(f"onepeek-docs {sha[:7]} has no tools/context.py (or is not fetched): `git -C {docs} fetch`, or move the "
              "pin in docs-version.txt to a newer docs commit", file=sys.stderr)
        return 1

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="onepeek-docs-pin-"))
    worktree = tmp / "docs"
    try:
        added = run(["git", "-C", str(docs), "worktree", "add", "--detach", str(worktree), sha])
        if added.returncode != 0:
            sys.stderr.write(added.stderr)
            return added.returncode
        result = run([sys.executable, str(worktree / "tools" / "context.py"), a.unit, *(["--refine"] if a.refine else [])])
    finally:
        run(["git", "-C", str(docs), "worktree", "remove", "--force", str(worktree)])
        shutil.rmtree(tmp, ignore_errors=True)
    if result.returncode != 0:
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        return result.returncode
    if a.out is None:
        sys.stdout.write(result.stdout)
    else:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(result.stdout)
        print(f"wrote {a.out} ({len(result.stdout.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
