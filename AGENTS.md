# AGENTS.md: .github

OnePeek's organisation-wide GitHub files: the shared CI workflows, the pull request template, the contributing and security guides, and the organisation profile. Every OnePeek repo uses these unless it has its own copy. This repo is public.

## Start here
- For any detail (what to build, how it behaves, names, data, limits), look in `onepeek-docs` first: the sibling folder `../onepeek-docs` or https://github.com/OnePeek/onepeek-docs. Begin with `standards/engineering-standards.md` and `standards/repo-map.md`, then grep and read only the files the task needs.
- If the docs are silent, unclear or disagree with these files, don't guess: ask, or open a docs pull request.

## Layout
- `.github/workflows/`: shared reusable workflows (`secret-scan`, `python-checks`, `node-checks`, `terraform-checks`, `release-tag`, `contract`, the contract between repos of decision 153, and `release-lock`, the cross-repo release lock of foundation-playbook §5.4 for releases outside `release-ecs`/`operate`) that every repo calls as `OnePeek/.github/.github/workflows/<name>.yml@<commit SHA>`. Every action is pinned to a commit SHA; downloaded tools are pinned and checksum-verified. Rules: `onepeek-docs/standards/branching-and-delivery.md`.
- **Runners.** Every reusable workflow takes `runner` (`onepeek`, `canary` or `github`, default `github`) and maps it, with the job's class, to `runs-on` itself; callers pass `runner: ${{ vars.RUNNER || 'github' }}` and never a label. Jobs that can hold a prod credential stay GitHub-hosted. `scripts/check_runner_labels.py` enforces the rules (`onepeek-docs` `architecture/lld/infra/ci-runners.md` §5.3-5.4); the prod-privileged job list is `.github/workflow-lint/github-only-jobs.yaml`.
- `PULL_REQUEST_TEMPLATE.md`: what changed, why, the docs link and how it was tested.
- `CONTRIBUTING.md` and `SECURITY.md`: how to contribute and how to report a vulnerability.
- `profile/`: the organisation's public profile page.
- `pins.yml` (every repo calls it): the repo's `OnePeek/.github` pins are all one `v*` tag's commit, and on pushes to `dev`/`release` and release pull requests `docs-version.txt` is on docs `main` (foundation-playbook §8a P2, S4; logic in `scripts/check_pins.py`). `depends-on.yml` (every repo's first pull-request job; the others `needs:` it): fails with "waiting for <repo>#<n>" while a `Depends on: OnePeek/<repo>#<n>` line in the body names an unmerged pull request (§8a P1). `package-cleanup.yml` (weekly, in each publishing repo): old `-next.*` and `0.0.0-pr<N>.*` npm versions go (§8a C3). Workflows that read other repos use the agents' App token (`AGENTS_APP_CLIENT_ID`/`AGENTS_APP_PRIVATE_KEY`, §8a S6); `ORG_READ_TOKEN` is a deprecated fallback.
- `workspace/`: the root setup for the folder holding all repos side by side, the shared Claude Code skills and agents (`skills/`, `agents/`, synced into each repo by `setup.py --sync-repos`), `packet.py` (a task packet for any repo at its pinned docs commit), `local_ci.py` (a repo's pull-request CI on the laptop), `preflight.py` (before every push and release: local CI, pins, tool versions, `--release` prerequisites; the `preflight` skill) and `ship_change_set.py`. `AGENTS.md` is the root instructions file for every tool (`CLAUDE.md` loads it for Claude Code); `setup.py` links every repo's Claude Code skills and agents into the root `.claude/` (run once after cloning, and again when a repo adds one; `--check` reports drift).

## Principles
- **Mirror the standards.** These files repeat `engineering-standards.md` in short form; when the standards change, these change with them.
- **Public by design.** Nothing here may hold a secret, an internal link or personal data.

These principles grow over time: when a review teaches us something, add it here in one line.

## Rules every OnePeek repo follows
Read `onepeek-docs/standards/engineering-standards.md` (https://github.com/OnePeek/onepeek-docs/blob/main/standards/engineering-standards.md) before changing anything. In short:
- **Names and language.** Canonical names from founder decision 97; plain English in anything a person reads; never name the prior internal product.
- **Pull requests.** Every change goes through a pull request; before launch, review is optional (decision 124). People and AI agents never commit, push, merge or deploy without an explicit instruction.
- **Repos pin the shared workflows by full commit SHA** with the version tag as a trailing comment (`@<sha> # v1.4.0`), never by a moving tag. `tag-main.yml` cuts a version tag on every merge here. Pins move by Dependabot pull request (proven in phase 2 of workstream W7); until then by `python3 scripts/bump_pins.py <repo-dir> <tag> <sha>`. Nobody edits a pin by hand.
