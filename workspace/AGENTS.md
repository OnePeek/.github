# AGENTS.md: OnePeek workspace

This folder holds the OnePeek repositories side by side. It is not itself a repository: this file (and `CLAUDE.md`, which loads it) is linked here by `.github/workspace/setup.py`. It applies to people and every AI coding tool alike.

## Before any task
0. **Every person and every AI agent working in any OnePeek repo, teammates and the main session (the coordinator) included, reads `onepeek-docs/standards/agent-operations.md` and `.github/workspace/pitfalls.md` first, and follows them strictly** (decision 171). That covers background builders and reviewers, docs drafters, and the session the founder talks to, in every repo.
   - The coordinator reads all of the standard at the start of each session, and runs every agent as its §3 to §7 say: briefs from `workspace/brief-template.md`, gates in proportion to the change, independent review, at most two rounds, three agents.
   - A background agent reads its role's sections and states what it read on its report's first line.
1. **Find the repo the task belongs to,** then read that repo's `AGENTS.md` and follow it. For example, backend work means `backend-service/AGENTS.md`. Each repo's rules apply to every file in it.
2. **The docs are the source of truth:** `onepeek-docs`. Start with its `standards/spec-to-code.md` and `standards/repo-map.md`.
3. **Building or changing code** follows `onepeek-docs/standards/spec-to-code.md` §6 (task packet, build, gates, independent review, pull request), **one stage at a time** (`standards/development-cycle.md`): start only when everything the unit depends on is finished, close every design gap before building, at most two review rounds. A unit ships as **one change set** (`standards/development-cycle.md` §2a, decision 136): one docs pull request plus one pull request per code repo it touches, all kept open, with gaps and review fixes added as commits to them, then merged together, docs first (merge commit), then code (squash into `dev`).
   - **Merge it with the `ship-change-set` skill** (`python3 .github/workspace/ship_change_set.py <repo>#<n> ... --go` from this root), never by hand: it merges in the fixed order (docs, `.github` then its pin moves, infra, design system, backend, web, then the other repos), waits only on required checks, re-runs a check once if it needed an earlier repo, and stops at the first red check.
   - The skills are linked into this folder by `setup.py`. The shared ones are written once in `.github/workspace/skills/` and synced into each repo: `build-from-lld`, `review-spec` and `fix-docs-gap`; `ship-change-set` is linked into this root only. The repo-specific ones are `build-module` (backend) and `refine-lld` (docs).
   - So are the agents: `builder` and `spec-reviewer` (shared), and `module-builder` (backend).
   - A packet for any repo: `python3 ../.github/workspace/packet.py <unit>` from inside that repo (the backend also has `uv run app context`).
   - **State the lane** (`standards/development-cycle.md` §2b): a `Lane: feature|hotfix` line in the task or pull request body, feature when absent. The packet prints it as its first line, `lane: <lane>` (`packet.py --task <file>` or `--pr <repo>#<n>` reads the line), and the builder's first report line repeats it.
4. **For work in one repo,** starting your tool inside that repo loads its rules from the first message. Use this root for work that spans repos.

## Running agents (AI tools)
How AI agents are run, so work is fast and token use stays low (founder, 2026-11-01; hardened by decision 171, 2026-11-06). The full rules, gates G0 to G8 and the edge cases are `onepeek-docs/standards/agent-operations.md`; these are the short form.
1. **Gap check first.** Before a build, a short read-only pass over the task packet lists every gap. All of them are closed on the change set's docs pull request before the builder starts, so the build runs straight through.
2. **Size by scope.** Each task is one coherent slice that fits in one run: about 400 changed lines at most, generated files and merges aside. A bigger slice is split before it starts (2026-11-06). The time limit (about 25 minutes) is a safety net against a stuck agent, not a way to split work.
3. **The right model for the job** (revised 2026-11-06, founder).
   - **Opus:** anything touching security, parsing, error handling, concurrency, CI workflows or infra; every design draft; every independent review.
   - **Sonnet:** only truly mechanical work, such as pin moves, renames, restyling to tokens and text syncs.
4. **One agent per repo lane, resumed.** Follow-ups in the same repo (the next slice, review fixes, a closed gap) resume that lane's agent so it keeps its context. Start it fresh when its context passes about 150k tokens or the work moves to an unrelated area. Reviewers start fresh for round 1 and are resumed for round 2.
5. **Three agents at once: two builders and one independent reviewer** (founder, 2026-11-06), on non-overlapping files.
   - Every normal-code and high-risk pull request, every design and anything touching security gets an independent Opus review before it merges. Mechanical and wording-only changes follow `agent-operations.md` §5.4a, and the coordinator's own work is never classed below normal code.
   - The coordinator does docs and design decisions itself, checks every design against the code before a builder starts, reviews every report, and takes over an agent that stalls.
6. **Context comes from the packet, not from a summary.** Every brief follows `workspace/brief-template.md`: the packet command, the docs commit, the sections that decide the slice, the files not to touch, and the authority given. Every builder and reviewer reads `workspace/pitfalls.md` first.
7. **Done is proven, not claimed.** A builder reports done only after the done-gate (a `BLOCKED` hand-back, `agents/builder.md`, is not a done report and comes before it) in `agents/builder.md`: mutations shown, macOS gates (and Linux for shell, workflow and script tests), a self-review, and `workspace/done_check.py` ending `DONE CHECK OK`. Every new kind of mistake a review finds becomes one line in `pitfalls.md`, in the same pull request as its fix.
8. **Local CI before every push.** `python3 .github/workspace/local_ci.py <repo>` runs the repo's own pull-request workflow steps (and the shared ones they call) on the laptop, with actionlint and shellcheck; push only when it ends `LOCAL CI OK`. Better, run `python3 .github/workspace/preflight.py <repo>` (`--release` before a release pull request), which runs local CI first and adds the pin, tool-version and release checks; push only when it ends `PREFLIGHT OK` (the `preflight` skill). CI then confirms rather than debugs.
9. **Gates once.** While iterating, run only the tests for the code being changed; run the full gates (and slow ones such as Docker-based Terraform tests) once before review.
10. **Short reports.** A builder reports: files per commit, the last line of each gate, the mutations run, the `DONE CHECK` line, gaps found, what is left. A reviewer reports ranked findings with `file:line` and a verdict. No narrative.

## Never
- Commit, push, merge, release or deploy without an explicit instruction from a person.
- Guess when the docs are silent. Fix the docs first (`fix-docs-gap`).
