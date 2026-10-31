"""Link every repo's Claude Code skills and agents into the workspace root, so a session started in the root has them.

Run once after cloning the repos side by side, and again after a repo adds a skill or agent:

    python3 .github/workspace/setup.py            # from the workspace root (the folder holding the repos)
    python3 .github/workspace/setup.py --check    # report what is missing or stale; exit 1 if anything is

It links (or, where symlinks are not allowed, copies) into <root>/.claude/:
  skills/<name>    from <repo>/.claude/skills/<name>
  agents/<name>.md from <repo>/.claude/agents/<name>.md
and <root>/AGENTS.md and <root>/CLAUDE.md from .github/workspace/ (CLAUDE.md loads AGENTS.md, as in every repo). Your own files there (for example settings.local.json)
are never touched; a name used by two repos is refused, not overwritten.
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]  # <root>/.github/workspace → <root>


def sources() -> tuple[dict[str, pathlib.Path], dict[str, pathlib.Path], list[str]]:
    skills: dict[str, pathlib.Path] = {}
    agents: dict[str, pathlib.Path] = {}
    clashes: list[str] = []
    for repo in sorted(p for p in ROOT.iterdir() if p.is_dir() and (p / ".git").exists()):
        for s in sorted((repo / ".claude" / "skills").glob("*/SKILL.md")):
            name = s.parent.name
            if name in skills:
                clashes.append(f"skill {name}: in {skills[name].parent.parent.parent.name} and {repo.name}")
            skills.setdefault(name, s.parent)
        for a in sorted((repo / ".claude" / "agents").glob("*.md")):
            if a.stem in agents:
                clashes.append(f"agent {a.stem}: in {agents[a.stem].parent.parent.parent.name} and {repo.name}")
            agents.setdefault(a.stem, a)
    return skills, agents, clashes


def wanted() -> dict[pathlib.Path, pathlib.Path]:
    skills, agents, _ = sources()
    links = {ROOT / "AGENTS.md": HERE / "AGENTS.md", ROOT / "CLAUDE.md": HERE / "CLAUDE.md"}
    links.update({ROOT / ".claude" / "skills" / n: p for n, p in skills.items()})
    links.update({ROOT / ".claude" / "agents" / f"{n}.md": p for n, p in agents.items()})
    return links


def ours(path: pathlib.Path) -> bool:
    """A link (or copy marker) this script made, so it may replace or remove it."""
    return path.is_symlink() or (path / ".workspace-copy").exists() or path.with_name(path.name + ".workspace-copy").exists()


def place(dst: pathlib.Path, src: pathlib.Path) -> str:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink() and dst.resolve() == src.resolve():
        return "ok"
    if dst.exists() or dst.is_symlink():
        if not ours(dst):
            return f"skipped: {dst} is your own file"
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst)
        else:
            dst.unlink()
        dst.with_name(dst.name + ".workspace-copy").unlink(missing_ok=True)
    try:
        dst.symlink_to(src, target_is_directory=src.is_dir())
        return "linked"
    except OSError:  # Windows without developer mode: copy, and mark it as ours
        if src.is_dir():
            shutil.copytree(src, dst)
            (dst / ".workspace-copy").write_text("copied by .github/workspace/setup.py; re-run it to refresh\n")
        else:
            shutil.copy2(src, dst)
            dst.with_name(dst.name + ".workspace-copy").write_text("copied by setup.py\n")
        return "copied"


def stale() -> list[pathlib.Path]:
    want = wanted()
    found = []
    for folder in (ROOT / ".claude" / "skills", ROOT / ".claude" / "agents"):
        for p in folder.glob("*") if folder.exists() else []:
            if p.name.endswith(".workspace-copy"):
                continue
            if p not in want and ours(p):
                found.append(p)
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="report only; exit 1 if anything is missing or stale")
    a = ap.parse_args()
    _, _, clashes = sources()
    for c in clashes:
        print(f"CLASH {c}: rename one; only the first is linked")
    want, old = wanted(), stale()
    if a.check:
        missing = [d for d, s in want.items() if not (d.is_symlink() and d.resolve() == s.resolve()) and not ours(d)]
        for d in missing:
            print(f"missing {d.relative_to(ROOT)}")
        for p in old:
            print(f"stale   {p.relative_to(ROOT)}")
        ok = not missing and not old and not clashes
        print("WORKSPACE OK" if ok else "WORKSPACE NEEDS setup.py")
        return 0 if ok else 1
    for p in old:  # a skill or agent its repo removed
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()
        print(f"removed {p.relative_to(ROOT)}")
    for dst, src in sorted(want.items()):
        print(f"{place(dst, src):8} {dst.relative_to(ROOT)} -> {src.relative_to(ROOT)}")
    return 1 if clashes else 0


if __name__ == "__main__":
    sys.exit(main())
