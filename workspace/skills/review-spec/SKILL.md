---
name: review-spec
description: Independently review a change against its spec (the task packet from onepeek-docs) and return findings with evidence and a ready / not-ready verdict. Use before opening any pull request that adds or changes code built from an LLD, or when asked to review code against the LLD, the HLD or the architecture.
---

# Review a change against its spec

`onepeek-docs/standards/spec-to-code.md` §6 step 5. The reviewer must not be the builder. It is given **the packet, the change and the builder's `Read:` line only**, never the builder's summary or the rest of its report, so it cannot be steered by the builder's view of the work. `ONEPEEK_WORKSPACE` is the folder holding all the repos side by side (`onepeek-docs/standards/agent-operations.md` §4.3); the paths below start from it, quoted.

## Run it
Start the `spec-reviewer` agent with three things:
- **The packet:** `.context/<unit>.md`. If it is missing, generate it with this repo's packet command, given in its `AGENTS.md`. In `backend-service` that is `uv run app context <unit> --out .context/<unit>.md`; elsewhere it is `python3 "$ONEPEEK_WORKSPACE/.github/workspace/packet.py" <unit> --out .context/<unit>.md`, which reads the docs at the commit in `docs-version.txt`.
- **The change:** the pull request (`<repo>#<n>`), or, before one exists, the pushed branch. The reviewer reads the diff from its own worktree of that pushed head (`git diff origin/<base>...HEAD`), so it reviews the same revision the gates run on. For a change set that spans repos, give it every repo's change and the docs pull request (`development-cycle.md` §2a), so the set is reviewed as one.
- **The builder's `Read:` line**, on its own (`agent-operations.md` §4.2).

Then wait for its report and act on every blocking and should-fix finding: a design answer is a commit on the change set's docs pull request, a code fix a commit on that repo's pull request; never a new pull request. Review the **final** change only, and at most twice (`onepeek-docs/standards/development-cycle.md` step 4); if a third would be needed, stop and report whether the design is unclear or the change needs reworking.

## What the reviewer checks (the `spec-reviewer` agent holds the full list)
1. **Traceability.** Every requirement in the packet is implemented, and every line of code traces to a packet section. Anything that does not trace is drift.
2. **Contracts.** Names, types, routes, events, jobs, settings and messages match the LLD exactly.
3. **Boundaries.** The change respects this repo's rules from its `AGENTS.md` and the HLD rules quoted in the packet.
4. **Tests.** Every test the LLD names exists and tests the behaviour, not internals. Every testing-plan level is covered.
5. **Bypasses.** The reviewer tries to break each rule the change relies on, using throwaway fixtures only.
6. **Gates.** The reviewer runs every gate listed in this repo's `AGENTS.md` on a throwaway worktree.
7. **Done check, mutations and pitfalls.** The reviewer runs `python3 "$ONEPEEK_WORKSPACE/.github/workspace/done_check.py" <worktree>` with `--pr <repo>#<n>`, or `--branch <branch>` before a pull request exists, breaks at least one behaviour per area, and checks the change against every line of `.github` `origin/main:workspace/pitfalls.md`.

## The result
Findings ranked **blocking**, **should-fix** and **nit**. Each has its `file:line`, the evidence, and the packet section it breaks. The report ends with a verdict and the head commit it reviewed: **ready at `<sha>`** or **not ready at `<sha>`**. A pull request needs "ready" at its current head; a verdict on an older head does not cover the commits after it.
