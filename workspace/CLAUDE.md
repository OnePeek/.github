@AGENTS.md

## Claude-specific notes
- The skills and agents in `.claude/` here are links to each repo's own (`setup.py`); a repo's `AGENTS.md` still governs work in that repo.
- Never commit, push, merge, release or deploy without an explicit instruction from a person.
- Read `onepeek-docs/standards/agent-operations.md` (all of it) and `workspace/pitfalls.md` at the start of every session, and run agents exactly as they and `AGENTS.md` "Running agents" say:
  - briefs from `workspace/brief-template.md`, with the gate class;
  - three background agents (two builders and one reviewer);
  - Opus unless the work is purely mechanical;
  - an independent Opus review as `agent-operations.md` §5.4a says, my own work always included;
  - at most two review rounds;
  - status files and `BLOCKED` hand-backs, never silent waiting;
  - short reports.
- Ship each unit as one change set merged together (`onepeek-docs/standards/development-cycle.md` §2a).
- **The learning loop is not optional** (founder, 2026-11-06; `agent-operations.md` §8a). On any failure in development, mine included: pause that slice, record it in the same pull request as the fix (a §2 row, a `pitfalls.md` line, and an automated check where possible), then watch the next passes until it is `holding`. A lesson that lives only in memory or chat does not count.
