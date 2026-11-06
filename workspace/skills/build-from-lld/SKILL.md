---
name: build-from-lld
description: Build or change code exactly to its LLD in any OnePeek repo other than backend-service (which has build-module). Use for any code task in onepeek-web, onepeek-design-system, browser-extension, plugin-owl, mcp-athena or onepeek-infra, e.g. "add the sign-in page", "build the autofill panel", "add the egress proxy module".
---

# Build to the LLD

`onepeek-docs/standards/spec-to-code.md` §6 is the source of these steps. The code implements the LLD exactly, and nothing it does not say. This repo's `AGENTS.md` section **"Build kit"** names two things: the unit (the LLD this repo implements) and the gate commands. Use exactly those.

`ONEPEEK_WORKSPACE` is the folder holding all the repos side by side (`onepeek-docs/standards/agent-operations.md` §4.3). Every workspace path below starts from it, quoted, so it works from a worktree under the scratchpad. If it is unset, stop and report `BLOCKED`.

## Steps
- **First, read `.github` `origin/main:workspace/pitfalls.md` in full** (`git -C "$ONEPEEK_WORKSPACE/.github" fetch -q origin && git -C "$ONEPEEK_WORKSPACE/.github" show origin/main:workspace/pitfalls.md`), and work in your own worktree, never a shared checkout: off `origin/<base>` for new work, or off the pull request's branch for work on an existing pull request (`git worktree add --detach <path> origin/<branch>`, pushed with `git push origin HEAD:<branch>`).
0. **Ready to start?** (`onepeek-docs/standards/development-cycle.md`, one stage at a time.) Everything this unit depends on is finished (§4 there) and the unit is yours (`owner: <github login>` on its entry in `architecture/lld/status.yaml`). If not, stop and say what is missing. Agree a time limit; on reaching it, stop and report what blocks you.
1. **Get the packet.** From inside your worktree, run `python3 "$ONEPEEK_WORKSPACE/.github/workspace/packet.py" <unit> --out .context/<name>.md`, with the unit from "Build kit", and read the whole file. It is built from the docs at the commit in `docs-version.txt`, and it is the complete spec for this task.
   - If it refuses because the LLD is not ready, stop. Refine the LLD in onepeek-docs first (`/refine-lld` there).
   - If `docs-version.txt` is older than the docs this task needs, move the pin in its own commit first. While the change set is open, the pin is the latest commit of its docs pull request (`onepeek-docs/standards/development-cycle.md` §2a).
   - If "Build kit" says the repo has a pending founder decision, stop and ask.
2. **Plan against the packet.** List every requirement this task touches: interfaces, routes, messages, contracts, settings, data, edge cases (each with its named test) and testing-plan levels. Each line of the plan cites the packet section it comes from. If you need something the packet does not say, use `/fix-docs-gap`; it never goes into the code.
3. **Build in this repo's layout,** exactly as the LLD §5 (and this repo's `docs/guidelines.md`) describe. Use only the interfaces the packet lists. Settings come from configuration, never literals. Nothing secret goes in the repo.
4. **Tests with the code.** Write every named test and cover every testing-plan level. Where the LLD names a merge gate (accessibility, privacy, no-evasion), it is a test that blocks merging.
5. **Gates and done-gate**, in proportion to the class the brief names (`agent-operations.md` §5.4a).
   - Run every command in "Build kit" on macOS. Run the changed tests in Linux too when they are shell, workflow or script tests; CI runs plain application tests on Linux. Then local CI on the worktree path: `python3 "$ONEPEEK_WORKSPACE/.github/workspace/local_ci.py" <worktree>` ends `LOCAL CI OK`.
   - Break the code once per behaviour and confirm its test fails (not for a mechanical change). Self-review the diff against the packet and `pitfalls.md`.
   - Once the work is pushed (by you only when the person allowed it, otherwise by the person), `python3 "$ONEPEEK_WORKSPACE/.github/workspace/done_check.py" <worktree> --pr <repo>#<n> --expect <the files you report>` must end `DONE CHECK OK`. When you add commits to a pull request that already had others, add `--since <sha>`, the pull request's head before your first commit, so `--expect` is compared with your commits' files only. While no pull request exists, use `--branch <branch>`, without `--expect` or `--since`. Without push authority, report your worktree path instead.
   - All must pass. Never weaken a check, add a skip without a reason a reviewer accepts, or lower a threshold to make it pass.
6. **Independent review.** Run `/review-spec <unit>`, or ask the `spec-reviewer` agent, giving it only the packet path, the branch or pull request, and your report's `Read:` line on its own (never the rest of your report). Before the pull request exists, it reviews the pushed branch. Fix every blocking and should-fix finding, then repeat steps 5 and 6 until the verdict is "ready" — **at most two rounds**. If a third would be needed, stop and report: an unclear design goes back to the docs (`/fix-docs-gap`); a change that needs reworking goes back to the build.
7. **Pull request** into the default branch, using the template. It is part of the unit's change set: list the docs pull request and the other repos' pull requests under "Change set", keep it open, add review fixes as commits, and merge it only with the whole set, after the docs pull request (`development-cycle.md` §2a).
   - "Implements" maps each requirement to the file and function that implements it.
   - List the docs read, paste the last line of each gate, and paste the review verdict.
   - Never commit, push, merge, apply or release without an explicit instruction from a person.

## Never
- Write code for something the packet does not say. Do not "improve" the design in code; change the docs first.
- Copy code from an earlier or archived codebase (decision 128).
- Add a dependency without a one-line reason, pinned, at least 7 days old (code-guidelines §10).
