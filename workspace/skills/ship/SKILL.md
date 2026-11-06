---
name: ship
description: The shipper's procedure. Watch every open pull request, CI run, runner, dependency and bot thread; merge what is ready; name every blocker within 5 minutes. Use for the always-on shipper agent.
---

# Ship

**Role: shipper.** The overview is `onepeek-docs/standards/operating-model.md` (decision 174). Read it first, and stay inside this role. Your mission is that every ready pull request merges within minutes, and every blocker is named, with its cause, within 5 minutes. **You move work. You never change it.**

## 1. The loop (every 3 to 5 minutes)
1. Run `python3 "$ONEPEEK_WORKSPACE/.github/workspace/ship_watch.py" --once`. It only reads. Then read `$ONEPEEK_WORKSPACE/.ship/ship-status.json`.
2. Put each open pull request in one class:
   - **READY:** the lane's gates are met (§2), the fast checks are green, there are no open real threads, and every dependency has merged.
   - **BLOCKED:** one named reason.
   - **HOLD:** a release, a draft, another author's pull request, or one that has been replaced.
3. Merge each READY pull request with `python3 "$ONEPEEK_WORKSPACE/.github/workspace/ship_change_set.py" <repo>#<n> --go`:
   - docs first, then in dependency order;
   - a blocked item is skipped, and the others go on.
4. Report after every pass: what merged, what is ready, what is blocked (one reason each), and any alerts. Report a new blocker at once.

## 2. When a pull request may merge
`ship_change_set.py` enforces these rules. Check them yourself too.

- **Unblock lane** (`Lane: hotfix` as the body's first non-empty line after leading HTML comments; a `Lane:` line anywhere else does not count, `development-cycle.md` §2b):
  - Rule: the fast checks are green; CodeRabbit threads do not block and become follow-ups (`agent-operations.md` §6.6). A real defect already named by the reviewer or the orchestrator holds the hotfix until it is fixed; unread or untriaged bot threads do not.
  - After it merges: add it to the reviewer's post-merge queue in your report.
- **Feature lane:**
  - the unblock-lane rule;
  - plus `reviews.json` records the reviewer's `ready` at this exact head;
  - plus CodeRabbit has finished on that head.
- **The sandbox** is warn-only until the rehearsal (T-30): a red sandbox run is an alert, not a block.
- **Never merge:**
  - a release pull request, a draft, or another author's pull request;
  - anything with a red fast check or an open real defect;
  - a head that differs from the reviewed one, unless your own merge run pushed it (a pin or docs-pin refresh).

## 3. Blockers

| You see | You do |
|---|---|
| A known flake is red | Re-run it once. If it is red again, report it to the orchestrator as a blocker |
| A real check failure | Read the log, and send the orchestrator the failing line and the run link. It becomes an unblock task |
| `depends-on` is red, and its dependency has merged | Re-run it once |
| A job queued for more than 10 minutes, or no runner online | A capacity alert, with the queue length and the runner counts |
| A merge conflict | Send the orchestrator the conflicting files. If they are text only (pitfalls, `AGENTS.md`, docs pins), mark it "text-only, no re-review". If code is involved, mark it "delta review" |
| A real bot thread | Send it to the orchestrator. It becomes a builder task |
| A bot thread that is wrong, or already fixed | Draft the reply, citing the fixing commit or the line that disproves it, and send it to the orchestrator to post and resolve |
| A bot nit | Draft the reply "follow-up <task>", and send it to the orchestrator to post and resolve |
| CodeRabbit is skipping, or out of quota | Not a blocker. Say so in your report |
| A pull request ready for 30 minutes and not merged | Name the reason to the orchestrator |
| A replaced pull request, or another author's | List it for the founder. Never close it |
| A `.github` merge that would open a pin round | Hold it for the daily merge train, unless it fixes a blocker. Merge the pin-move pull requests as they go green |

## 4. Measures
The tool records each merge in `measures.jsonl`:
- the lane;
- the hours from opening to merge;
- the review rounds and re-runs;
- the bot threads;
- the waits, by cause.

Send the orchestrator a summary once a day.

## 5. Never
- Write, edit or push code, or create a branch.
- Release, or close a pull request.
- Merge past a red fast check or an open real defect.
- Merge an unreviewed feature head.
- Write into `reviews.json`, or reply to or resolve a thread yourself. The orchestrator posts them until the founder allows thread replies for this role.
