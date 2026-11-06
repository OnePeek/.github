# Agent brief template

The coordinator writes every builder or reviewer brief in this shape (AGENTS.md "Running agents"). The agent's context
comes from the packet and the files named here, never from the coordinator's summary of them. A brief that cannot fill
a field is not ready, so close the gap first.

```
Lane: feature | hotfix
Role: builder | reviewer | docs drafter        Model: opus | sonnet (rule in AGENTS.md "Running agents" 3)
Repo: OnePeek/<repo>   Base: origin/<dev|main>   Branch: <type>/<topic> (existing PR: #<n>, or "new")

Spec (read in full before anything else):
  - Packet: python3 ../.github/workspace/packet.py <unit> --out .context/<unit>.md   (backend: uv run app context <unit>)
  - Docs commit: onepeek-docs@<sha> (main, or the change set's open docs PR #<n>)
  - Sections that decide this slice: <file §section>, ...
  - Decisions that bind it: <founder decision numbers>
Class: docs only | mechanical | normal code | high risk   (agent-operations.md §5.4a; it sets the done-gate and the review)
Read first (and state it with the commit as the report's first line):
  onepeek-docs/standards/agent-operations.md §1, §3, §4 (+ §5 builder, §6 reviewer); .github/workspace/pitfalls.md (all of it);
  the repo's AGENTS.md and docs/guidelines.md if it exists.

Slice (about 400 changed lines at most, generated files and merges aside):
  - Build: <behaviour 1>, <behaviour 2>, ...
  - Files expected to change: <paths>
  - Do NOT touch: <paths other open PRs own>

Known traps for this slice: <pitfalls.md numbers, or new ones>

Authority (what the person allowed for this task): commit + push to the branch: yes | no;  open PR: yes | no;  merge: never.

Done means (all of it, in the report):
  1. Gap check first: if the spec is silent or contradicts, stop and report the gap (file §section). Never invent.
  2. Every behaviour has a test; one mutation per behaviour shown failing.
  3. Gates: changed tests on macOS and in Linux; then local CI on the worktree path -> `LOCAL CI OK`.
  4. Self-review against the packet and pitfalls.md, listing what was checked.
  5. `python3 .github/workspace/done_check.py <worktree> --pr <repo>#<n>` -> `DONE CHECK OK`.
Report: the `Read:` line first; then files per commit; the last line of each gate; mutations; the DONE CHECK line; gaps; what is left. No narrative.
```

A reviewer brief names only the packet, the docs commit, the diff (`gh pr diff <n>` or the branch), and the focus areas.
It never includes the builder's summary.
