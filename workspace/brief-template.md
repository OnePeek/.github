# Agent brief template

The coordinator writes every builder or reviewer brief in this shape (AGENTS.md "Running agents"). The agent's context
comes from the packet and the files named here, never from the coordinator's summary of them. A brief that cannot fill
a field is not ready, so close the gap first.

The brief's first line, outside the block below, is the lane: exactly `Lane: feature`, `Lane: hotfix` or `Lane: docs`.
`packet.py --task` and the merge tools read only the first non-empty line of a brief or pull request body
(`development-cycle.md` §2b), so a task file copied from this template starts with that line, unfenced.

```
Role: builder | reviewer | docs drafter        Model: opus | sonnet (rule in AGENTS.md "Running agents" 3)
Repo: OnePeek/<repo>   Base: origin/<dev|main>   Branch: <type>/<topic> (existing PR: #<n>, or "new")

Env: shell state does not persist between commands; prefix every command that needs it with
  `export ONEPEEK_WORKSPACE=<the folder holding the repos> &&` (pitfalls.md 37). Read docs from origin/main or the docs
  commit named below (`git -C "$ONEPEEK_WORKSPACE/onepeek-docs" show <ref>:<path>`), never from a shared checkout that may be stale.

Symptom (unblock lane): the failing run's link, the log line, and the behaviour the docs expect (file §section).
  Never a guessed cause: the builder reproduces first and reports the cause it finds (2026-11-06, T-58).

Spec (read in full before anything else):
  - Packet: python3 "$ONEPEEK_WORKSPACE/.github/workspace/packet.py" <unit> --out .context/<unit>.md, from the worktree
    (backend: uv run app context <unit>); ONEPEEK_WORKSPACE is the folder holding all the repos (agent-operations.md §4.3)
  - Docs commit: onepeek-docs@<sha> (main, or the change set's open docs PR #<n>)
  - Sections that decide this slice: <file §section>, ...
  - Decisions that bind it: <founder decision numbers>
  - Gaps ruled: <each gap listed in the gap check or by an earlier round, with the docs commit that rules it> | none listed
    (agent-operations.md §6.1, F28: every listed gap is ruled before review starts)
Class: docs only | mechanical | normal code | high risk   (agent-operations.md §5.4a; it sets the done-gate and the review)
Read first (and state it with the commit as the report's first line):
  onepeek-docs origin/main:standards/agent-operations.md §1, §3, §4 (+ §5 builder, §5.4a and §6 reviewer);
  .github origin/main:workspace/pitfalls.md (all of it);
  the repo's AGENTS.md and docs/guidelines.md if it exists.

Slice (about 400 changed lines at most, generated files and merges aside):
  - Build: <behaviour 1>, <behaviour 2>, ...
  - Files expected to change: <paths>
  - Do NOT touch: <paths other open PRs own>

Known traps for this slice: <pitfalls.md numbers, or new ones>
Hostile inputs (when the slice adds or changes a pattern that decides a privilege or a gate: network, credentials,
  AWS, lane, review): the real inputs to derive cases from, and the near-misses to test (pitfalls.md 40).

Status: write <worktree>/.context/status.md at every phase change (at least every 10 min); never wait > 5 min on CI,
  a review or a dependency: push if the authority below allows it and local CI is green (else keep the worktree path),
  set phase: blocked, and hand back a BLOCKED report (agent-operations.md §5.5, §5.6).

Authority (what the person allowed for this task): commit + push to the branch: yes | no;  open PR: yes | no;  merge: never.

Done means (all of it, in the report):
  1. Gap check first: if the spec is silent or contradicts, stop and report the gap (file §section). Never invent.
  2. Every behaviour has a test; one mutation per behaviour shown failing.
  3. Gates: changed tests on macOS, and in Linux for shell, workflow and script tests; then local CI on the worktree path -> `LOCAL CI OK`.
  4. Self-review against the packet and pitfalls.md, listing what was checked.
  5. `python3 "$ONEPEEK_WORKSPACE/.github/workspace/done_check.py" <worktree> --pr <repo>#<n> --expect <files>` -> `DONE CHECK OK`
     (adding commits to an existing pull request: also `--since <its head before the first new commit>`;
     `--branch <branch>`, without `--expect` or `--since`, only while no pull request exists; once one exists, `--pr`).
A BLOCKED hand-back comes before the done-gate: it names what blocks, the done-gate steps not done, and the next step.
Report: the `Read:` line first; then files per commit; the last line of each gate; mutations; the DONE CHECK line; gaps; what is left. No narrative.
```

A reviewer brief fills the same fields, with the role `reviewer`, the slice as the change to review (the pull request
`<repo>#<n>`, or the pushed branch before one exists) and its focus areas, and the authority `edit, commit, push and
merge: never`. It gives the builder's `Read:` line on its own (agent-operations.md §4.2), and never the builder's summary
or the rest of its report. The reviewer reviews the pushed head and names it in its verdict.
