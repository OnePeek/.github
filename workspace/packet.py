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


LANE = re.compile(r"^[ \t]*(?:[-*+][ \t]+)?(?:#+[ \t]+)?(?:\*\*|__)?Lane:(?:\*\*|__)?[ \t]*(.*)$", re.M | re.I)
COMMENT = re.compile(r"<!--.*?-->", re.S)  # a template's `<!-- Lane: feature|hotfix -->` hint is not a statement
LANES = ("feature", "hotfix")


def lane_of(text: str | None) -> str:
    """The lane a task or pull request states in a line containing `Lane:` (a bullet and bold are fine:
    `- **Lane:** hotfix`), `feature` or `hotfix` (development-cycle §2b); feature when it says none. The first word after
    it is the value, so `Lane: hotfix (preprod down)` is hotfix; `hot fix`, an empty value or any other word is an
    error, never silently the default."""
    found = []
    for m in LANE.finditer(COMMENT.sub("", text or "")):
        words = m.group(1).replace("*", " ").replace("`", " ").split()
        found.append(words[0].lower() if words else "")
    if not found:
        return "feature"
    if len(set(found)) > 1 or found[0] not in LANES:
        raise ValueError(f"Lane must be one of {', '.join(LANES)}, got: {', '.join(repr(f) for f in found)}")
    return found[0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("unit", help="module ID, platform/<part>, clients/<client> or infra/<part>")
    ap.add_argument("--refine", action="store_true", help="packet for refining a minimum module LLD")
    ap.add_argument("--out", type=pathlib.Path, help="write the packet to this file instead of stdout")
    ap.add_argument("--task", type=pathlib.Path, help="the task text; a `Lane: feature|hotfix` line in it sets the lane")
    ap.add_argument("--pr", metavar="REPO#N", help="read the `Lane:` line from this OnePeek pull request's body (gh)")
    ap.add_argument("--repo", type=pathlib.Path, default=pathlib.Path.cwd(), help="the repo (default: current folder)")
    a = ap.parse_args()

    text = a.task.read_text() if a.task else ""
    if a.pr:
        repo, _, n = a.pr.partition("#")
        body = run(["gh", "pr", "view", n, "-R", f"OnePeek/{repo}", "--json", "body", "--jq", ".body"])
        if body.returncode != 0:
            sys.stderr.write(body.stderr)
            return body.returncode
        text += "\n" + body.stdout
    try:
        lane = lane_of(text)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1

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
    packet = f"lane: {lane}\n{result.stdout}"
    if a.out is None:
        sys.stdout.write(packet)
    else:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(packet)
        print(f"lane: {lane}\nwrote {a.out} ({len(packet.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
