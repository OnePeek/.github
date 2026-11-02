"""Rewrite `OnePeek/.github/.github/workflows/<name>.yml@<sha> # <tag>` pins in a repo to one tag's commit.

Usage: bump_pins.py <repo-dir> <tag> <sha>   (the 40-character commit the tag points at; look it up with
`git rev-parse <tag>^{commit}`). Used until Dependabot's bump of these refs is proven (decision 146).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PIN = re.compile(r"(OnePeek/\.github/\.github/workflows/[\w.-]+\.yml)@[0-9a-f]{40}(?:[ \t]*#[^\n]*)?")


def bump(text: str, tag: str, sha: str) -> str:
    return PIN.sub(lambda m: f"{m.group(1)}@{sha} # {tag}", text)


def main(argv: list[str]) -> int:
    if len(argv) != 3 or not re.fullmatch(r"[0-9a-f]{40}", argv[2]) or not re.fullmatch(r"v\d+\.\d+\.\d+", argv[1]):
        print(__doc__, file=sys.stderr)
        return 2
    repo, tag, sha = Path(argv[0]), argv[1], argv[2]
    changed = 0
    for f in sorted((repo / ".github" / "workflows").glob("*.y*ml")):
        old = f.read_text()
        new = bump(old, tag, sha)
        if new != old:
            f.write_text(new)
            changed += 1
            print(f"updated {f}")
    print(f"{changed} file(s) updated")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
