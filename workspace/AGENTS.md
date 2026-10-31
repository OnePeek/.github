# AGENTS.md: OnePeek workspace

This folder holds the OnePeek repositories side by side. It is not itself a repository: this file (and `CLAUDE.md`, which loads it) is linked here by `.github/workspace/setup.py`. It applies to people and every AI coding tool alike.

## Before any task
1. **Find the repo the task belongs to,** then read that repo's `AGENTS.md` and follow it. For example, backend work means `backend-service/AGENTS.md`. Each repo's rules apply to every file in it.
2. **The docs are the source of truth:** `onepeek-docs`. Start with its `standards/spec-to-code.md` and `standards/repo-map.md`.
3. **Building or changing code** follows `onepeek-docs/standards/spec-to-code.md` §6: task packet, build, gates, independent review, pull request.
   - The skills are linked into this folder by `setup.py`. The shared ones are written once in `.github/workspace/skills/` and synced into each repo: `build-from-lld`, `review-spec` and `fix-docs-gap`. The repo-specific ones are `build-module` (backend) and `refine-lld` (docs).
   - So are the agents: `builder` and `spec-reviewer` (shared), and `module-builder` (backend).
   - A packet for any repo: `python3 ../.github/workspace/packet.py <unit>` from inside that repo (the backend also has `uv run app context`).
4. **For work in one repo,** starting your tool inside that repo loads its rules from the first message. Use this root for work that spans repos.

## Never
- Commit, push, merge, release or deploy without an explicit instruction from a person.
- Guess when the docs are silent. Fix the docs first (`fix-docs-gap`).
