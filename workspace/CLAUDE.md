@AGENTS.md

## Claude-specific notes
- The skills and agents in `.claude/` here are links to each repo's own (`setup.py`); a repo's `AGENTS.md` still governs work in that repo.
- Never commit, push, merge, release or deploy without an explicit instruction from a person.
- Run agents as `AGENTS.md` "Running agents" says: gap check first, slices sized to one run, Sonnet for mechanical work and Opus for design and review, resumed repo lanes (fresh at about 150k tokens), at most two lanes, full gates once, short reports.
- Ship each unit as one change set merged together (`onepeek-docs/standards/development-cycle.md` §2a).
