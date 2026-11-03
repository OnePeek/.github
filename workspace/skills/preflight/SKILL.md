---
name: preflight
description: Run the preflight check before every push and before every release pull request in any OnePeek repo. Use whenever you are about to push a branch, open or update a pull request, or open, update or merge a dev -> release pull request, e.g. "push this", "open the PR", "cut the release".
---

# Preflight before every push and release

A check that runs only in CI, or only on a release pull request, costs a CI round each time it fails (onepeek-docs `architecture/lld/infra/foundation-playbook.md` §8a P4, lesson L21). Preflight runs those checks on the laptop first.

## Steps
1. **From the workspace root** (the folder holding the repos side by side), on the worktree you are about to push:
   - before a push: `python3 .github/workspace/preflight.py <repo or worktree dir>`
   - before opening, updating or merging a `dev` -> `release` pull request: add `--release`.
2. **It runs, in order:**
   1. Declared dependencies (§8a P1): the `Depends on: OnePeek/<repo>#<n>` lines of the branch's open pull request, or, before it has one, each `--depends-on OnePeek/<repo>#<n>` you pass. While one is unmerged, preflight refuses: keep the branch local, or open its pull request as a draft (a draft's push is only a warning; its CI waits in the `depends-on` job). When your change needs another repo's unmerged pull request, write that line in your pull request body.
   2. local CI (`local_ci.py`). If local CI does not end `LOCAL CI OK`, preflight refuses: it stops there and runs nothing else.
   3. The pin checks: every `OnePeek/.github` pin is the commit of **one** `v*` tag.
   4. The tool versions:
      - Terraform, Python and Node must match every tracked `.terraform-version`, `.python-version` and `.node-version`;
      - uv must be CI's pinned version;
      - tflint is CI's version, or a warning.
   5. With `--release`:
      - `docs-version.txt` is on onepeek-docs `main`;
      - the agents' App grants every permission the workflows ask it for and is installed on the repos they name;
      - the other repos the contract and the release lock read are in a clean release state: at most one open `dev` -> `release` pull request, and with none, `release` is an ancestor of `dev`.
3. **Read the result.** Each line starts with a status:
   - `PASS`: fine.
   - `FAIL`: fix it before you push. The line names the fix (install a version, move pins with one pin-move, merge the docs pull request first, rebuild `release` from `dev`, ask the founder to grant an App permission).
   - `WARN`: push is allowed. Say it in your report (for example, tflint is not CI's version, so local CI skipped it).
   - `UNVERIFIED`: the laptop cannot see it (repository secrets; the App's installed repositories without org-admin read). List these in your report so a person checks them. Never treat them as passed.
4. **Only push when the last line is `PREFLIGHT OK`.** Check that exact line in a separate step from the run, as with `LOCAL CI OK`.

## Never
- Push, or open a release pull request, past a `PREFLIGHT FAILED`.
- Change the App, its installations or repository secrets yourself. Report what is missing; a person changes them.
- Run preflight in a shared checkout another agent may be using. Run it on your own worktree.
