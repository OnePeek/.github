---
name: build-from-lld
description: Build or change code exactly to its LLD in any OnePeek repo other than backend-service (which has build-module). Use for any code task in onepeek-web, onepeek-design-system, browser-extension, plugin-owl, mcp-athena or onepeek-infra, e.g. "add the sign-in page", "build the autofill panel", "add the egress proxy module".
---

# Build to the LLD

`onepeek-docs/standards/spec-to-code.md` §6 is the source of these steps. The code implements the LLD exactly, and nothing it does not say. This repo's `AGENTS.md` section **"Build kit"** names two things: the unit (the LLD this repo implements) and the gate commands. Use exactly those.

## Steps
0. **Ready to start?** (`onepeek-docs/standards/development-cycle.md`, one stage at a time.) Everything this unit depends on is finished (§4 there) and the unit is yours (`owner: <github login>` on its entry in `architecture/lld/status.yaml`). If not, stop and say what is missing. Agree a time limit; on reaching it, stop and report what blocks you.
1. **Get the packet.** Run `python3 ../.github/workspace/packet.py <unit> --out .context/<name>.md`, with the unit from "Build kit", and read the whole file. It is built from the docs at the commit in `docs-version.txt`, and it is the complete spec for this task.
   - If it refuses because the LLD is not ready, stop. Refine the LLD in onepeek-docs first (`/refine-lld` there).
   - If `docs-version.txt` is older than the docs this task needs, move the pin in its own commit first.
   - If "Build kit" says the repo has a pending founder decision, stop and ask.
2. **Plan against the packet.** List every requirement this task touches: interfaces, routes, messages, contracts, settings, data, edge cases (each with its named test) and testing-plan levels. Each line of the plan cites the packet section it comes from. If you need something the packet does not say, use `/fix-docs-gap`; it never goes into the code.
3. **Build in this repo's layout,** exactly as the LLD §5 (and this repo's `docs/guidelines.md`) describe. Use only the interfaces the packet lists. Settings come from configuration, never literals. Nothing secret goes in the repo.
4. **Tests with the code.** Write every named test and cover every testing-plan level. Where the LLD names a merge gate (accessibility, privacy, no-evasion), it is a test that blocks merging.
5. **Gates.** Run every command in "Build kit". All must pass. Never weaken a check, add a skip without a reason a reviewer accepts, or lower a threshold to make it pass.
6. **Independent review.** Run `/review-spec <unit>`, or ask the `spec-reviewer` agent, giving it only the packet path and the diff. Fix every blocking and should-fix finding, then repeat steps 5 and 6 until the verdict is "ready" — **at most two rounds**. If a third would be needed, stop and report: an unclear design goes back to the docs (`/fix-docs-gap`); a change that needs reworking goes back to the build.
7. **Pull request** into the default branch, using the template.
   - "Implements" maps each requirement to the file and function that implements it.
   - List the docs read, paste the last line of each gate, and paste the review verdict.
   - Never commit, push, merge, apply or release without an explicit instruction from a person.

## Never
- Write code for something the packet does not say. Do not "improve" the design in code; change the docs first.
- Copy code from an earlier or archived codebase (decision 128).
- Add a dependency without a one-line reason, pinned, at least 7 days old (code-guidelines §10).
