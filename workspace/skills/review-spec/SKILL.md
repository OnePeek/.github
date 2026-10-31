---
name: review-spec
description: Independently review a change against its spec (the task packet from onepeek-docs) and return findings with evidence and a ready / not-ready verdict. Use before opening any pull request that adds or changes code built from an LLD, or when asked to review code against the LLD, the HLD or the architecture.
---

# Review a change against its spec

`onepeek-docs/standards/spec-to-code.md` §6 step 5. The reviewer must not be the builder. It is given **the packet and the diff only**, never the builder's summary, so it cannot be steered by the builder's view of the work.

## Run it
Start the `spec-reviewer` agent with two things:
- **The packet:** `.context/<unit>.md`. If it is missing, generate it with this repo's packet command, given in its `AGENTS.md`. In `backend-service` that is `uv run app context <unit> --out .context/<unit>.md`; elsewhere it is `python3 ../onepeek-docs/tools/context.py <unit> --out .context/<unit>.md`, run against the docs commit in `docs-version.txt`.
- **The diff:** `git diff dev...HEAD`, or a branch name.

Then wait for its report and act on every blocking and should-fix finding. Review the **final** change only, and at most twice (`onepeek-docs/standards/development-cycle.md` step 4); if a third would be needed, stop and report whether the design is unclear or the change needs reworking.

## What the reviewer checks (the `spec-reviewer` agent holds the full list)
1. **Traceability.** Every requirement in the packet is implemented, and every line of code traces to a packet section. Anything that does not trace is drift.
2. **Contracts.** Names, types, routes, events, jobs, settings and messages match the LLD exactly.
3. **Boundaries.** The change respects this repo's rules from its `AGENTS.md` and the HLD rules quoted in the packet.
4. **Tests.** Every test the LLD names exists and tests the behaviour, not internals. Every testing-plan level is covered.
5. **Bypasses.** The reviewer tries to break each rule the change relies on, using throwaway fixtures only.
6. **Gates.** The reviewer runs every gate listed in this repo's `AGENTS.md` on a throwaway worktree.

## The result
Findings ranked **blocking**, **should-fix** and **nit**. Each has its `file:line`, the evidence, and the packet section it breaks. The report ends with a verdict: **ready** or **not ready**. A pull request needs "ready".
