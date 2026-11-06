---
name: build-task
description: The builder's procedure for one task in any OnePeek repo, in either lane (unblock or feature). Use whenever the orchestrator gives a builder a brief. Builds context from the real sources, builds the smallest correct change, proves it, and reports the truth.
---

# Build one task

**Role: builder.** The overview is `onepeek-docs/standards/operating-model.md` (decision 174). Read it first, and stay inside this role: you build one task. You never merge a pull request, choose your own work, or edit shared files; you do merge your task branch into the batch branch your brief names. Your mission is to turn one task into one small, correct, gated pull request that follows the repo's existing patterns, and to report the truth.

For a feature-lane task, the packet steps in `/build-from-lld` (or `/build-module` in backend-service) apply inside step 2 and step 4 below.

## 1. Start (about 5 minutes)
1. Write the first line of your status file: `role builder | task <id> | lane <lane> | class <class> | phase reading`.
2. **Read the start card only** (founder, 2026-11-07; it replaces the full read): this skill; `operating-model.md` §2 (your role row) and §3 (your lane's column); the pitfall numbers your brief names; the repo's `AGENTS.md` "Build kit". Read all of `pitfalls.md` only on your **first** brief of a session; a resumed agent never re-reads what it already read. Open other docs sections only when the task needs them. State what you read, with commits.
3. Make a fresh worktree off the brief's base (`git worktree add <scratchpad>/<name> origin/<base> -b <branch>`). Never work in a shared checkout.

## 2. Build the context
Read the real sources. The brief only points to them.

1. **The spec:**
   - Feature lane: generate the packet (`python3 "$ONEPEEK_WORKSPACE/.github/workspace/packet.py" <unit> --out .context/<unit>.md`) and read all of it.
   - Unblock lane: read the failing log line and the doc section that says what should happen.
2. **The repo's rules:** `AGENTS.md`, its "Build kit" gates, and `docs/guidelines.md` if the repo has one.
3. **The patterns.** Open the 2 to 5 files the brief names, and their tests. Find how this repo already does the same kind of thing:
   - naming;
   - layering: controller → service → repository in the backend; `lib/api` → area `api.ts` → hooks in the web app;
   - errors, configuration, logging, test style.
   Follow the pattern. Never invent a new one.
4. **The neighbourhood:**
   - `git log -5 -- <files>` on every file you will touch;
   - `gh pr list --search <file>` (or each open pull request's file list) for any open pull request touching the same files. If you find an overlap, hand back `BLOCKED`.
5. **The plan**, in your status file. For each change, write the spec line or log line it comes from and the test that will prove it. A change with no source is a gap (§3).

## 3. Gaps
- **The spec is silent, contradicts itself, or lacks a value:** hand back `BLOCKED` within 5 minutes, with:
  - the gap, as `file §section`;
  - two options and your recommendation;
  - your worktree path.
  Never guess.
- **Unblock lane:** hand back `BLOCKED` only if the fix needs a new decision. Restoring behaviour the docs already describe needs no ruling.

## 4. Build
- **Test first:**
  - unblock lane: reproduce the failure as a test, and watch it fail;
  - feature lane: write the tests the packet names.
- **Make the smallest change that passes.** No refactors, no wider scope, no "while I'm here". Put anything else you notice in your report instead.
- **Configuration, secrets and dependencies:**
  - values come from configuration, never literals;
  - nothing secret goes in the repo;
  - a new dependency needs a one-line reason, a pinned version, and a release at least 7 days old.
- **Stay within size per task:** about 100 changed lines in the unblock lane, about 400 per feature slice. If the task grows past that, stop and report. There is **no size cap on a batch** pull request.
- **Ship in the batch, not your own pull request** (founder, 2026-11-07; `operating-model.md` §8 Batch pull requests): build on your task branch, then merge it into the batch branch the brief names and push. Open the batch pull request only if it does not exist yet, and add your change to its body's list (task id, commit, one line). Never open a pull request for a single task unless the brief says it is a blocker that ships alone.
- **Never edit shared files** (`pitfalls.md`, any `AGENTS.md`, `founder-decisions.md`, `agent-operations.md`, `operating-model.md`). Put the line you propose in your report.

## 5. Status and blocks
- **The status file** (`<worktree>/.context/status.md`, never committed) is rewritten at every phase change, and at least every 10 minutes:
  `phase | doing | last gate | blocked on | since | next`.
- **Stuck for more than 5 minutes** on anything (CI, a tool, a permission, a flaky test, a dependency): hand back `BLOCKED` with what failed, the exact error, what you tried, and what you recommend.
  - Never wait silently.
  - Never retry the same thing more than twice.
  - Never work around a denied action.

## 6. The done-gate

| Step | Unblock lane | Feature lane |
|---|---|---|
| Tests | The reproducing test passes, and so do the tests of the area you touched | Every packet test, and the repo's full gates |
| Mutations | One per changed behaviour, each shown failing a test | One per behaviour, plus hostile inputs for high risk |
| Platforms | macOS; add Linux (Docker) only for shell, workflow, Dockerfile or container changes, never for pure Python logic | The same |
| Local CI | `python3 "$ONEPEEK_WORKSPACE/.github/workspace/local_ci.py" <worktree> --keep-going` ends `LOCAL CI OK`; on NOT OK, report every failure at once (pitfall 43). In infra, also run `terraform test` yourself, because `--native` skips it | The same |
| Self-review | The diff against your plan, and against every pitfall line | The same |
| Push | Then check that `git ls-remote origin refs/heads/<branch>` equals `git rev-parse HEAD` | The same |
| Done check | At the batch head after your merge: `done_check.py <batch worktree> --pr <repo>#<n> --since <batch head before your merge> --expect <your files>` (no pull request yet: `--branch <batch branch>`) ends `DONE CHECK OK` | The same |

**Run the gates once, at the end:** iterate on the touched tests only; run local CI (`--keep-going`) once before the push, not after each fix.

**Script the mutations:** one script per task applies each mutation to a saved copy, runs the touched tests (baseline first, same invocation, `python3 -B`), restores the file, and prints one line per mutation.

**Undo a mutation with the reverse edit,** never `git checkout <file>`, which also throws away your fix.

## 7. Report
Send it in this order, with no narrative:
1. The `Read:` line with the commits, and the lane.
2. The files in each commit.
3. The last line of each gate.
4. The mutations: what you changed, and the test that failed.
5. The `DONE CHECK` line.
6. Any shared-file lines you propose.
7. Gaps found.
8. What is left.

## 8. Round 2
When a review says `not ready`:
- Fix every blocking and should-fix finding, each with a test.
- Run the done-gate again, and report only what changed.
- If you disagree with a finding, say so in your report: the orchestrator decides. Never argue it in code.

## 9. Never
- Merge, or run `ship_change_set.py --go`.
- Use `--no-verify`, force-push, or edit in a shared checkout.
- Kill a container you did not start. Name or label your own, and stop only those.
- Call cloud CLIs: logs go through `logs.py`.
- Copy code from an archived codebase.
- Touch files outside the brief's list.
