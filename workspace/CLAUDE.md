@AGENTS.md

## Claude-specific notes
- The skills and agents in `.claude/` here are links to each repo's own (`setup.py`); a repo's `AGENTS.md` still governs work in that repo.
- Never commit, push, merge, release or deploy without an explicit instruction from a person.
- Read `onepeek-docs/standards/agent-operations.md` (all of it) and `workspace/pitfalls.md` at the start of every session, and run agents exactly as they and `AGENTS.md` "Running agents" say:
  - briefs from `workspace/brief-template.md`, with the gate class;
  - three background agents (two builders and one reviewer);
  - Opus unless the work is purely mechanical;
  - an independent review of every change, my own included;
  - at most two review rounds;
  - status files and `BLOCKED` hand-backs, never silent waiting;
  - short reports.
- Ship each unit as one change set merged together (`onepeek-docs/standards/development-cycle.md` §2a).
