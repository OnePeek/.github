---
name: ship-change-set
description: Merge a finished change set (one docs pull request plus the code pull requests across repos) in the fixed order, waiting only on required checks. Use when a unit's pull requests are reviewed and ready, or when asked to "merge", "ship" or "land" pull requests that span repos.
---

# Ship a change set in the right order

A change set is merged in one sitting, in one order (`onepeek-docs/standards/development-cycle.md` §2a). Merging by hand in the wrong order costs a CI round each time: a caller runs on a workflow that is not merged yet, or a check reads a file another repo has not merged. The script does the order, the waits and the pin moves, so nobody waits longer than the checks take.

## Steps
1. **Ready means ready:** the review said "ready" and every pull request in the set has its last fixes pushed. Minor review findings go to a follow-up set, not into this one.
2. **Dry run from the workspace root:**
   `python3 .github/workspace/ship_change_set.py <repo>#<n> <repo>#<n> ...` (any order).
   It prints the merge order and stops on any problem: a closed or draft pull request, the wrong base, two docs pull requests, or a code repo whose `docs-version.txt` is not at the docs pull request's last commit.
3. **Ship:** the same command with `--go`. It runs in order:
   1. docs into `main` with a merge commit;
   2. `.github` into `main` (squash), then waits for its release tag and moves every later pull request's `OnePeek/.github` workflow pins to that tag in one commit;
   3. infra, then the design system, then backend, then web, then the other repos, each squashed into `dev`.
   
   Conflicts come first: a pull request that conflicts with its base, or whose state GitHub cannot report yet, stops the run before any wait, and the ones still to merge are checked again after every merge and pin move.

   Before each merge it waits for that pull request's **required** checks (on a branch with none required yet, every check except CodeRabbit, so nothing merges unchecked). A check that failed before an earlier repo in the set merged is re-run once (it may have needed that repo, for example the contract check needs infra's `scripts/contract.py`). A second failure stops the run, and nothing after it is merged.
4. **On a stop:** read the failed check's log, fix it as a commit on that pull request, then run the same command again; merged pull requests are left out of the list.
5. **After:** the docs merge's follow-ups (republish the architecture pages, `status.yaml`), and the deploy where the repo deploys (`done` means deployed, decision 143).

## Never
- Merge past a red required check, or with `--admin`.
- Wait on checks for a pull request with merge conflicts: resolve them first (the script stops on them).
- Run it on a release pull request (`dev` into `release`); releases have their own flow.
