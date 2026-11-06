---
name: build-task
description: The builder's procedure for one task in any OnePeek repo, in either lane (unblock or feature). Use whenever the orchestrator gives a builder a brief. Builds context from the real sources, builds the smallest correct change, proves it, and reports the truth.
---

# Build one task

**Role: builder.** The overview is `onepeek-docs/standards/operating-model.md` (decision 174). Read it first, and stay inside this role: you build one task. You never merge, choose your own work, or edit shared files. Your mission is to turn one task into one small, correct, gated pull request that follows the repo's existing patterns, and to report the truth.

For a feature-lane task, the packet steps in `/build-from-lld` (or `/build-module` in backend-service) apply inside step 2 and step 4 below.

## 1. Start (about 5 minutes)
1. Write the first line of your status file: `role builder | task <id> | lane <lane> | class <class> | phase reading`.
2. Read these, with the commit of each:
   - `operating-model.md` §1 and §2, and this skill;
   - `.github/workspace/pitfalls.md`, all of it;
   - the repo's `AGENTS.md`.
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
- **Stay within size:** about 100 changed lines in the unblock lane, about 400 in the feature lane. If the task is growing past that, stop and report.
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
| Platforms | macOS, and Linux for shell, workflow or script changes | The same |
| Local CI | `python3 "$ONEPEEK_WORKSPACE/.github/workspace/local_ci.py" <worktree>` ends `LOCAL CI OK`. In infra, also run `terraform test` yourself, because `--native` skips it | The same |
| Self-review | The diff against your plan, and against every pitfall line | The same |
| Push | Then check that `git ls-remote origin refs/heads/<branch>` equals `git rev-parse HEAD` | The same |
| Done check | `done_check.py <worktree> --pr <repo>#<n> --expect <your files>` ends `DONE CHECK OK` | The same |

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
