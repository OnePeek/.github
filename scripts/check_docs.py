#!/usr/bin/env python3
"""The docs check of a repo (PIPE-SIMPLE v2 audit (b) §2c and §4c; R-DOCS-LOCAL: a repo's docs ride in its own PR).

    python3 check_docs.py <repo-dir>

Reads every markdown file under `docs/` and every `*.md` at the repo root, and fails (exit 1, one `::error` line per
problem, file:line) when:
1. a relative link does not resolve: `[text](target)`, `![alt](target)` and `[label]: target` whose target has no
   scheme (http:, https:, mailto: and the like) and is not only an `#anchor`; the target, without its `#anchor` or
   `?query` and URL-decoded, is a file or folder relative to the linking file (a leading `/` means the repo root).
   Links inside code fences and inline code are not links. A target that leaves the repo fails;
2. front matter (a first line `---`) has no closing `---` line, or a line in it is not `key: value`, a list item, an
   indented continuation, a comment or blank;
3. a code fence (``` or ~~~, three or more) is still open at the end of the file.
A file that cannot be read as UTF-8 is a problem too (fail closed). Standard library only: our runner image has no pip.
"""
from __future__ import annotations

import pathlib
import re
import sys
import urllib.parse

FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
INLINE_CODE = re.compile(r"(`+)(?:(?!\1).)+?\1")
INLINE_LINK = re.compile(r"!?\[(?:[^\]\\]|\\.)*\]\(\s*(<[^>\n]*>|[^)\s]+)(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*\)")
REF_DEF = re.compile(r"^ {0,3}\[(?:[^\]\\]|\\.)+\]:\s*(<[^>\n]*>|\S+)")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
FRONT_KEY = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ .-]*:(?:\s.*)?$")


def files(repo: pathlib.Path) -> list[pathlib.Path]:
    out = sorted(p for p in repo.glob("*.md") if p.is_file())
    docs = repo / "docs"
    if docs.is_dir():
        out += sorted(p for p in docs.rglob("*.md") if p.is_file())
    return out


def front_matter(lines: list[str]) -> tuple[list[str], int]:
    """(problems, the index of the first body line)."""
    if not lines or lines[0].rstrip() != "---":
        return [], 0
    for i in range(1, len(lines)):
        if lines[i].rstrip() in ("---", "..."):
            problems = []
            for n in range(1, i):
                line = lines[n]
                if (not line.strip() or line.lstrip().startswith("#") or line[:1] in (" ", "\t")
                        or line.startswith("- ") or line.rstrip() == "-" or FRONT_KEY.match(line)):
                    continue
                problems.append(f"{n + 1}: front matter line is not `key: value`: {line.strip()[:80]!r}")
            return problems, i + 1
    return ["1: front matter has no closing `---` line"], len(lines)


def target_ok(target: str, here: pathlib.Path, repo: pathlib.Path) -> str:
    """'' when the link resolves (or is not a relative link), else why not."""
    t = target.strip()
    if t.startswith("<") and t.endswith(">"):
        t = t[1:-1].strip()
    if not t or t.startswith("#") or SCHEME.match(t) or t.startswith("//"):
        return ""
    path = urllib.parse.unquote(t.split("#", 1)[0].split("?", 1)[0])
    if not path:
        return ""
    dest = (repo / path.lstrip("/")) if path.startswith("/") else (here.parent / path)
    try:
        dest = dest.resolve()
    except (OSError, RuntimeError):
        return "cannot be resolved"
    if not dest.is_relative_to(repo.resolve()):
        return "leaves the repo"
    return "" if dest.exists() else "does not exist"


def check_file(path: pathlib.Path, repo: pathlib.Path) -> list[str]:
    rel = path.relative_to(repo)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as e:
        return [f"{rel}:1: cannot be read as UTF-8 text ({type(e).__name__})"]
    found, start = front_matter(lines)
    problems = [f"{rel}:{p}" for p in found]
    fence: tuple[str, int, int] | None = None  # (char, length, line number)
    for n in range(start, len(lines)):
        line = lines[n]
        m = FENCE.match(line)
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1] and not m.group(2).strip():
                fence = None
            continue
        if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            fence = (m.group(1)[0], len(m.group(1)), n + 1)
            continue
        text = INLINE_CODE.sub("", line)
        targets = [mm.group(1) for mm in INLINE_LINK.finditer(text)]
        ref = REF_DEF.match(text)
        if ref:
            targets.append(ref.group(1))
        for t in targets:
            why = target_ok(t, path, repo)
            if why:
                problems.append(f"{rel}:{n + 1}: link {t[:100]!r} {why}")
    if fence:
        problems.append(f"{rel}:{fence[2]}: code fence {fence[0] * fence[1]} is never closed")
    return problems


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: check_docs.py <repo-dir>", file=sys.stderr)
        return 2
    repo = pathlib.Path(args[0]).resolve()
    if not repo.is_dir():
        print(f"::error::{args[0]} is not a folder")
        return 1
    checked = files(repo)
    problems = [p for f in checked for p in check_file(f, repo)]
    for p in problems:
        print(f"::error::{p}")
    print(f"{'FAIL' if problems else 'OK'}  {len(checked)} markdown file(s), {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
