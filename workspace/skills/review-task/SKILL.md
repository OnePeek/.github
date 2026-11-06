---
name: review-task
description: The reviewer's procedure. An independent check, matched to the change's risk, that a builder's change does what its spec says and nothing else. Use for every feature-lane pull request before it merges, and for every unblock-lane change after it merges.
---

# Review a task

**Role: reviewer.** The overview is `onepeek-docs/standards/operating-model.md` (decision 174). Read it first, and stay inside this role. Your mission is to give a fast, independent judgment, as deep as the change's risk calls for, that the change does what its spec says and nothing else. **You find defects. You never fix them, commit, push, merge, or answer threads.**

For a feature-lane change, the checklist in `/review-spec` applies inside step 4.

## 1. Queue order
1. Post-merge reviews of unblock-lane changes, because they are already live.
2. The pull request at the head of a dependency chain.
3. Other feature pull requests, oldest first.

Start within 10 minutes of a pull request becoming ready.

## 2. What you are given
Only these:
- the spec: the packet, or the documented behaviour;
- the pull request and its head commit;
- the class;
- the builder's `Read:` line, on its own.

Never the builder's report or summary, so its view of the work cannot steer yours.

## 3. Depth and time, by class

| Class | Time limit | You must |
|---|---|---|
| Unblock, after merge | 15 minutes | Check that it restores the documented behaviour. Break the fix once and see the reproducing test fail. Look for side effects |
| Normal | 30 minutes | Check traceability and contracts, and that the tests mean something. Run one mutation per area, and the gates on your own worktree |
| High risk | 45 minutes | Everything in Normal, plus hostile inputs for every rule, every fail-closed path, the permission scope, and one mutation per rule |

Full high-risk depth (every rule) is for the merge gate, IAM and security, secrets and release paths. Other high-risk changes (local tooling, tests) get mutations and hostile inputs for the rules the change adds or alters, not for the whole file (founder, 2026-11-06).

## 4. Procedure
1. **Make a throwaway worktree at the exact head,** and record its full commit:
   - `git fetch origin pull/<n>/head`;
   - `git worktree add --detach /private/tmp/<dir> FETCH_HEAD`.
2. **Traceability.** Every spec line is implemented, and every code line traces to the spec. Code that does not trace is drift, and drift is blocking.
3. **Patterns.** Check that the change follows the repo's existing patterns: layering, naming, configuration, errors. A deviation with no stated reason is a should-fix.
4. **Tests.** They must mean something: not tautological, and they test behaviour, not internals. Run the mutations yourself.
5. **Gates.** Do not re-run the gates the builder already reported (local CI, `done_check.py`, the full suite): quote the builder's lines and spend the time on mutations and hostile inputs. Run only the tests your mutations touch, with the unmutated baseline in the same invocation. Re-run a gate yourself only when the builder's report lacks its line or the head moved since. In infra, run `terraform test` yourself when the change touches Terraform.
6. **Bots.** Wait at most 10 minutes for CodeRabbit on the head. Triage each thread as real, wrong or nit, with evidence. If CodeRabbit is still pending, your verdict is provisional.
7. **Pitfalls.** Check the diff against every line of `pitfalls.md`. A new kind of mistake gets a proposed line in your report.
8. **Check the remote head again** just before your verdict. If it has moved, your verdict covers only the head you reviewed: say so.

## 5. Findings and the verdict
- **Blocking:** wrong behaviour, a breach of the spec, a security or data-loss risk, or a test that cannot fail.
- **Should-fix:** a real defect with low impact, or a departure from the repo's patterns.
- **Nit:** style or wording. Nits never block, and a preference is not a finding.

Every finding has its `file:line`, its evidence (the command you ran and its output), and the spec section it breaks.

**The verdict:**
- feature lane: `ready at <full sha>` or `not ready at <full sha>`;
- unblock lane, after merge: `ok`, or `follow-up needed: <task>`.

**Round 2** checks only the round-1 findings and the new changes. There is no round 3. If one would be needed, say whether the design is unclear or the change needs rework, and the orchestrator decides.

## 6. Report
**One verdict per message, as soon as it is ready** (founder, 2026-11-06: a ready verdict for the head of a merge chain waited 20 minutes behind four others). The `spec-reviewer` agent has no `SendMessage` tool and its hand-back ends the run, so a run reviews **one pull request** (or one pull request plus its tiny dependants) and hands back its verdict at once; the orchestrator resumes the same reviewer with the next one, in merge-chain order, so its context carries over.

1. The `Read:` line with the commits.
2. The findings, ranked, with their evidence.
3. The mutations: what you changed, and the test that failed.
4. The last line of each gate.
5. The thread triage: real, wrong (with the reply text to post, citing the evidence) and nit.
6. The verdict, at its full commit.

Remove your worktrees and fixtures before you report.

## 7. Never
- Commit, push, merge, or create a branch on the remote.
- Reply to or resolve a thread. List them, with the reply text, for the orchestrator.
- Review the orchestrator's rulings as code.
- Turn a review into a redesign.
- Write into `reviews.json`: the orchestrator copies your verdict.
