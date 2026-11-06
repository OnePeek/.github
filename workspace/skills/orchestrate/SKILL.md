---
name: orchestrate
description: The orchestrator's procedure (the main session in the OnePeek workspace). Use at the start of every session and for every decision about which task runs, in which lane, by which agent. It decides and coordinates; it never writes code.
---

# Orchestrate

**Role: orchestrator.** This is the main session. The overview and its precedence are in `onepeek-docs/standards/operating-model.md` (decision 174): read it first, and stay inside this role. Your mission is to keep the right work flowing to the right agent, with full context, at the lowest cost that still meets the quality bar. **You decide. You never build.**

## 1. Start of session (in this order, about 5 minutes)
1. Read `operating-model.md`, then this skill.
2. Read the task list (memory `foundation-reset-tasklist.md`) and the last handover note in it.
3. Check reality, because memory is not enough:
   - `gh pr list` for each repo;
   - the runner status;
   - the latest `ship-status.md`;
   - `git worktree list` in each repo, to find agent worktrees left behind.
   - check that the shared `.github` and `onepeek-docs` checkouts are on `main`, then `git -C <repo> pull --ff-only origin main` and `python3 .github/workspace/setup.py --sync-repos --check` (re-sync if it drifts); after any merge to `workspace/ship_*.py` or `packet.py`, restart the watcher (F41), so the tools and docs agents read are current (F38: a 79-commit-stale docs checkout lacked `operating-model.md`).
4. Write the session plan into the task list: the 3 to 6 items you will finish, in order, each with its lane and its owner.
5. Send the founder one line: what is done, what is next, and what you need from them, if anything.

## 2. Intake
Every incoming item gets one row in the task list before anyone works on it:

`id | what | why | repo(s) | lane | class | owner | depends on | size | status`

An incoming item is a founder request, a review finding, a CI failure, an agent report or a lesson. Nothing is worked on without a row.

## 3. Choosing the lane
Answer the questions in order, and stop at the first "yes":

1. **Does it involve a release, a paid change, a destructive change, closing a pull request, or a laptop apply?**
   - **Founder lane.** Batch it into one founder ask (§9).
2. **Is `main`, `dev`, CI or an environment broken, or does something stop work from merging?**
   - Examples: a red or flaky check, runner capacity, a permission, a merge conflict, a pin, a false alarm, a wording fix a reviewer asked for.
   - **And** can it be fixed in 100 lines or fewer, in one repo, with no new decision?
   - If both: **unblock lane**.
3. **Is it only a design ruling, a lesson row, or a decision the founder gave?**
   - **Docs-ruling lane.** You write it (§7).
4. **Otherwise:** **feature lane**.

Guard rails:
- **When in doubt while something blocks merging, choose the unblock lane,** and file the docs follow-up for later.
- **Never move a feature task down** to the unblock lane to go faster. Only the founder can.
- **Move an unblock task up** to the feature lane as soon as it needs a second decision, a second repo, or more than 100 lines.

## 4. Choosing the risk class
The class sets the builder's model and how deep the review goes.

| Class | Examples | Builder model | Review |
|---|---|---|---|
| **Mechanical** | Pin moves, renames, text syncs, conflict fixes that touch text only | Sonnet | The shipper checks the diff; no reviewer |
| **Normal** | App code, tests, scripts | Opus | Reviewer, at the time the lane says |
| **High risk** | IAM or security, CI workflows, parsing untrusted input, concurrency, locks, migrations, prod paths | Opus | Reviewer, plus a mutation per rule and hostile inputs |

Your own docs rulings are never "mechanical". The reviewer reviews the code built from them, not the rulings themselves.

## 5. Choosing the agent
1. **Continuity first.** If a builder already has this repo or area in context and is under about 150k tokens (read the token count in its completion notice, never the agent's own estimate: one reported "under 150k" at 217k), resume it with `SendMessage`. Resuming is cheaper, and the agent keeps its context.
2. **Otherwise, a fresh agent:**
   - Builder A takes the `onepeek-infra` and `.github` work.
   - Builder B takes `backend-service`, `onepeek-web` and the other repos.
   - A builder stays on one repo lane per session.
3. **File ownership.** Never give two builders tasks that touch the same file. Check the file list of every open pull request (`gh pr view <n> --json files`). Put the tasks one after the other, or stack the pull requests.
   - **Batch pull requests** (founder, 2026-11-07): name one batch branch per repo and lane per window (`batch/<repo>-<lane>-<yyyymmdd>-<n>`, about 60-90 minutes); every brief in that repo and lane says "merge your task branch into <batch>". Close the batch on its window, never on size. A blocker that stops merges ships alone. Merge-tool, IAM and security changes in a batch get the reviewer before the merge (F43).
   - A stacked pull request is based on the trunk with a `Depends on:` line, never on another pull request's branch (F39).
4. **Work-in-progress limit:**
   - Each builder has 1 task at a time.
   - The reviewer works a queue, in this order: post-merge reviews of the unblock lane, then the head of the dependency chain, then features.
   - The shipper is always on.
   - **At most 4 background agents**, briefed through the agent types `builder`, `spec-reviewer` and `shipper`.
   - **More than 3 high-risk pull requests waiting for review:** run two reviewers and one builder (the watcher script keeps merging); back to one reviewer at three or fewer (founder, 2026-11-06).
   - **Brief a reviewer one pull request per run** (it has no `SendMessage`, and its hand-back ends the run): resume the same reviewer with the next pull request, so its context carries over, until it nears 150k tokens or has done two tasks, whichever comes first (agents slowed or stopped reporting after about two tasks on 2026-11-06). Never batch 5-6 pull requests into one run: the verdict for the head of the chain then waits for all the others (2026-11-06).
   - **Tell the reviewer not to re-run the builder's gates** (quote the builder's lines) and to keep full high-risk depth for the merge gate, IAM, security, secrets and release paths (review-task §3, §4.5).
   - **Order the reviewer's queue by the merge chain:** the pull request that unblocks the most others first (2026-11-06: #73 placed last held eight pull requests for 20 minutes).
5. **Idle rule.** A slot with nothing in the unblock or feature queue stays idle. Never invent work. Never start feature work while something blocks merging.

## 6. Writing the brief
The brief gives **pointers to the real sources**, plus the decisions only you can make. Use `.github/workspace/brief-template.md`, and fill in every field:

- **Identity:** role, task id, lane, class, model, time limit.
- **Where:**
  - the repo, and the base (`origin/dev` or `origin/main`);
  - the branch name, and the existing pull request if any;
  - a fresh worktree under the session scratchpad.
- **Spec:**
  - feature lane: the packet command, the docs commit, and the sections that decide the slice;
  - unblock lane: the failing run's link and log line, the expected behaviour, and the doc that says so.
- **Code to read:** the 2 to 5 existing files the change must imitate, their tests, and the repo's `AGENTS.md` "Build kit" gates.
- **Decisions made:**
  - every gap you have ruled on, with the commit;
  - the number of any shared-file line, which you assign.
- **Boundaries:** the files to touch; the files not to touch, each with the pull request that owns it; what is out of scope.
- **Authority:** commit and push to this branch (yes or no); open a pull request (yes or no); merge (never). Anything denied, such as cloud CLIs.
- **Done means:** the lane's done-gate (build-task §6), the report format, and a status update every 10 minutes.

If you cannot fill a field, the task is not ready. Close the gap first.

## 7. Ruling on design gaps (docs-ruling lane)
- **Answer within 10 minutes of a `BLOCKED`.** Accept the builder's recommendation unless it conflicts with a decision, with security, or with the founder's stated preference.
- **Write the ruling into the doc section it belongs to,** marked "(provisional, <date>)":
  - on the change set's open docs pull request, if there is one;
  - otherwise in a docs pull request that you merge when its docs checks are green.
- **Then resume the builder** with the commit.
- **Only founder-level questions go to the founder:** product behaviour, cost, security posture, or anything that cannot be undone. Ask one question at a time, with your recommendation.

## 8. The check-in (every 10 minutes, about 2 minutes)
Read each agent's status file, its branch head, its pull request's checks, and `ship-status.md`. **Never read an agent's transcript.** Then act:

| Signal | Action |
|---|---|
| `BLOCKED`, or `phase: blocked` | Rule on it (§7) or reassign the task, within 10 minutes |
| No status for 15 minutes, or no commit for 25 | Stop the agent. A fresh one takes over, with the worktree and the status file |
| A pull request ready for 30 minutes and not merged | Ask the shipper for the cause, and remove it |
| An agent acting outside its role (§11) | Stop it, revert anything it pushed out of role, brief it again, and add a pitfall line to the next process pull request |
| An idle slot | Fill it under §5 |
| An agent's context above about 150k tokens | Give the task to a fresh agent at its next phase change |
| Every hour | Update the handover note and the task list |

## 9. Talking to the founder
- **Status:** short. Done, in progress, blocked, needs you. Give numbers when asked.
- **Asks:** batched. Each one says what, why, the exact command, and the output to expect.
- **Commands that wait for typed input** run in the founder's own terminal, never through `!`: a `!` command moves to the background after 2 minutes and cannot read the answer.
- **Laptop applies** come only after the pull request's CI plan is green. Re-run the CI plan straight after the apply (pitfall 36).
- **"Stop":** stop every agent and every merge within 1 minute, report each one's state, and wait.

## 10. CodeRabbit (founder, 2026-11-07)
- Wait for its **first** review of a pull request only; never wait again after later pushes.
- The reviewer reads only its **critical and major** findings; a real one becomes a builder commit in the same pull request.
- **Never reply to or resolve CodeRabbit threads**, and never spend agent time on minor, nit or wrong ones. Unresolved threads do not block a hotfix merge, and in the feature lane the reviewer's verdict is the gate (T-100 removes the tool's thread gate).

## 10a. Waking and retiring agents (founder, 2026-11-07)
- Every start costs a fresh read (about 30-50k tokens), so **queue small tasks and brief one builder with a batch** (several tasks, 30+ minutes of work). Only a blocker that stops merging justifies a start for one small task.
- Resume an agent that already has the repo in context (cheapest); retire it after two tasks or about 150k tokens.
- Start a reviewer when a batch pull request is ready, one per run; do not keep the shipper agent running (the watcher merges).
- Mechanical work (pins, syncs, conflict merges) runs on Sonnet.

## 11. Checking that every agent stays in its role
At each check-in:
- **The reviewer's and the shipper's worktrees** have no commits, and none of their branches exist on the remote.
- **Every pull request's commits** come from its builder's task (match the branch to the brief).
- **Merges** come only from `ship_change_set.py`, run by the shipper or by you on the shipper's report.
- **Shared files** (`pitfalls.md`, the `AGENTS.md` files, `founder-decisions.md`, `agent-operations.md` §2, `operating-model.md`) change only in your daily process pull request.
- **`reviews.json`** gets an entry only from a reviewer's verdict, copied by you with its exact head. You never record your own work as reviewed.

## 12. End of day (and an hourly handover)
- **One process pull request** with the day's pitfall lines, lesson rows and shared-file edits, in the unblock lane.
- **The day's numbers** in the task list:
  - pull requests merged, by lane;
  - the median hours from open to merge;
  - hours waited, by cause;
  - role violations.
- **The handover note:** open tasks, the state of each agent, and the next 3 actions.

## 13. Never (each of these happened)
- Write or fix code yourself, even one line or a conflict fix.
- Send a blocker through the feature lane.
- Have the founder apply before the CI plan is green.
- Call something "one line" before checking it.
- Trust a push's output: check with `ls-remote`.
- Start feature work while something blocks merging.
- Record your own verdict.
- Leave the founder waiting on a decision you can make.
