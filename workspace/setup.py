"""Link every repo's Claude Code skills and agents into the workspace root, so a session started in the root has them.

Run once after cloning the repos side by side, and again after a repo adds a skill or agent:

    python3 .github/workspace/setup.py            # from the workspace root (the folder holding the repos)
    python3 .github/workspace/setup.py --check    # report what is missing or stale; exit 1 if anything is
    python3 .github/workspace/setup.py --sync-repos   # copy the shared skills and agents into every code repo

Shared skills and agents (review-spec, fix-docs-gap, spec-reviewer) are written once, in .github/workspace/skills and
.github/workspace/agents. `--sync-repos` copies them into each repo in SYNC_REPOS with a "synced" note after the front
matter; `--check` also fails when a repo's copy differs from the shared one (edit the shared copy, then re-sync).

It links (or, where symlinks are not allowed, copies) into <root>/.claude/:
  skills/<name>    from <repo>/.claude/skills/<name>
  agents/<name>.md from <repo>/.claude/agents/<name>.md
and <root>/AGENTS.md and <root>/CLAUDE.md from .github/workspace/ (CLAUDE.md loads AGENTS.md, as in every repo). It then runs `lefthook install` in every sibling folder that is a git clone with a lefthook.yml (or says how to install lefthook). Your own files there (for example settings.local.json)
are never touched; a name used by two repos is refused, not overwritten.
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]  # <root>/.github/workspace → <root>
SHARED_SKILLS = HERE / "skills"
SHARED_AGENTS = HERE / "agents"
# Which shared skills and agents each repo carries. backend-service has its own builder (build-module,
# module-builder); marketing-website has no LLD, so only the docs-first skill applies.
_ALL = ("review-spec", "fix-docs-gap", "build-from-lld", "spec-reviewer", "builder")
SYNC_REPOS: dict[str, tuple[str, ...]] = {
    "backend-service": ("review-spec", "fix-docs-gap", "spec-reviewer"),
    "onepeek-infra": _ALL,
    "onepeek-web": _ALL,
    "onepeek-design-system": _ALL,
    "browser-extension": _ALL,
    "plugin-owl": _ALL,
    "mcp-athena": _ALL,
    "marketing-website": ("fix-docs-gap",),
}
NOTE = "<!-- Synced from OnePeek/.github workspace/{kind}/{name} by setup.py --sync-repos. Edit it there, not here. -->\n"


def shared(names: tuple[str, ...]) -> dict[pathlib.Path, str]:
    """Relative path inside a repo's .claude/ → the text it must hold, for the named shared skills and agents."""
    out: dict[pathlib.Path, str] = {}
    for s in sorted(SHARED_SKILLS.glob("*/SKILL.md")):
        if s.parent.name in names:
            out[pathlib.Path("skills") / s.parent.name / "SKILL.md"] = with_note(s.read_text(), "skills", s.parent.name)
    for a in sorted(SHARED_AGENTS.glob("*.md")):
        if a.stem in names:
            out[pathlib.Path("agents") / a.name] = with_note(a.read_text(), "agents", a.name)
    return out


def with_note(text: str, kind: str, name: str) -> str:
    """Insert the synced note right after the front matter (Claude Code reads the front matter first)."""
    end = text.index("\n---\n", 4) + 5
    return text[:end] + NOTE.format(kind=kind, name=name) + text[end:]


def sync_problems(write: bool) -> list[str]:
    problems: list[str] = []
    for repo, names in SYNC_REPOS.items():
        base = ROOT / repo / ".claude"
        if not (ROOT / repo / ".git").exists():
            problems.append(f"{repo}: not cloned beside .github")
            continue
        for rel, text in shared(names).items():
            dst = base / rel
            if dst.exists() and dst.read_text() == text:
                continue
            if write:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(text)
                print(f"synced   {repo}/.claude/{rel}")
            else:
                problems.append(f"{repo}/.claude/{rel}: {'differs from' if dst.exists() else 'missing'} the shared copy")
    return problems


def sources() -> tuple[dict[str, pathlib.Path], dict[str, pathlib.Path], list[str]]:
    skills: dict[str, pathlib.Path] = {s.parent.name: s.parent for s in SHARED_SKILLS.glob("*/SKILL.md")}
    agents: dict[str, pathlib.Path] = {a.stem: a for a in SHARED_AGENTS.glob("*.md")}
    shared_names = set(skills) | set(agents)
    clashes: list[str] = []
    for repo in sorted(p for p in ROOT.iterdir() if p.is_dir() and (p / ".git").exists()):
        for s in sorted((repo / ".claude" / "skills").glob("*/SKILL.md")):
            name = s.parent.name
            if name in shared_names:
                continue  # a synced copy of a shared skill: the root links the shared original
            if name in skills:
                clashes.append(f"skill {name}: in {skills[name].parent.parent.parent.name} and {repo.name}")
            skills.setdefault(name, s.parent)
        for a in sorted((repo / ".claude" / "agents").glob("*.md")):
            if a.stem in shared_names:
                continue
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


def hook_repos(root: pathlib.Path) -> list[pathlib.Path]:
    """Every folder beside .github that is a git clone with a lefthook.yml (not only the repos that carry skills)."""
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / ".git").exists() and (p / "lefthook.yml").exists())


def install_hooks() -> int:
    """Install each linked repo's lefthook git hooks (the pre-commit gitleaks step lives there). Returns 1 on failure."""
    repos = hook_repos(ROOT)
    if not shutil.which("lefthook"):
        print("lefthook is not installed, so no git hooks were set up. Install it once (macOS: brew install lefthook; "
              "or: npm i -g lefthook), then run this script again. Also install gitleaks (brew install gitleaks): "
              "the pre-commit hook fails without it.")
        return 1
    failed = 0
    for r in repos:
        res = subprocess.run(["lefthook", "install"], cwd=r, capture_output=True, text=True)
        print(f"{'hooks ok ' if res.returncode == 0 else 'hooks FAIL'} {r.name}")
        failed += res.returncode != 0
    if not shutil.which("gitleaks"):
        print("warning: gitleaks is not installed; commits will be refused until it is (brew install gitleaks).")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="report only; exit 1 if anything is missing or stale")
    ap.add_argument("--sync-repos", action="store_true", help="copy the shared skills and agents into every code repo")
    a = ap.parse_args()
    if a.sync_repos:
        problems = sync_problems(write=True)
        for p in problems:
            print(p)
        return 1 if problems else 0
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
        drift = sync_problems(write=False)
        for d in drift:
            print(f"drift   {d}")
        ok = not missing and not old and not clashes and not drift
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
    hooks = install_hooks()
    return 1 if clashes or hooks else 0


if __name__ == "__main__":
    sys.exit(main())
