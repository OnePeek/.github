Agents: first run `vanguard install` (OnePeek/vanguard), then read its `cards/<your role>.md`. Nothing else in this repo says how agents work.

# AGENTS.md: .github

OnePeek's organisation-wide GitHub files: the shared CI workflows, their scripts and tests, the pull request template, the contributing and security guides, and the organisation profile. Every OnePeek repo uses these unless it has its own copy. This repo is public. It implements the CI parts of `onepeek-docs` `standards/branching-and-delivery.md` and `architecture/lld/infra/` (foundation-playbook, delivery-pipelines, ci-runners).

## Start here
- For any detail (what to build, how it behaves, names, data, limits), look in `onepeek-docs` first: begin with `standards/engineering-standards.md` and `standards/repo-map.md`, then grep and read only the files the task needs.
- Layout:
  - `.github/workflows/`: shared reusable workflows (`secret-scan`, `python-checks`, `node-checks`, `terraform-checks`, `release-tag`, `contract`, the contract between repos of decision 153, and `release-lock`, the release lock of foundation-playbook §5.4 for releases outside `release-ecs`/`operate`; since §5.4a one key per scope (`locks/{infra,backend,web,mcp}.json`, plus `locks/release.json` during the expand step), taken try-or-release, with a phase in each record that `finish` acts on, every wait capped by `scripts/check_lock_limits.py` against the runners' lifetimes) that every repo calls as `OnePeek/.github/.github/workflows/<name>.yml@<commit SHA>`. Every action is pinned to a commit SHA; downloaded tools are pinned and checksum-verified. Rules: `onepeek-docs/standards/branching-and-delivery.md`.
  - **Supply chain (foundation-playbook §12a slice 4).** `osv-scan.yml` (every repo with a lockfile, on every pull request: Google's osv-scanner image pinned by digest; blocks on a scanner exit other than 0 or 1, on high or critical, or on any finding with a fix; waivers in the caller's `osv-scanner.toml` with `reason` and `ignoreUntil`; logic in `scripts/check_osv_results.py`); `ci-image-bump.yml` (weekly, caller-scheduled: moves digest-pinned CI container `image:` lines to their tag's current digest, failing on a pinned line it cannot move; pushes and opens a pull request only with the optional `CI_BUMP_APP_ID`/`CI_BUMP_APP_PRIVATE_KEY` App secrets (the onepeek-ci-bump App, docs PR #183); logic in `scripts/bump_ci_images.py`); workflow-lint rule 6 in `scripts/check_runner_labels.py` refuses a job `services:`/`container:` image that is not `image: <name>@sha256:<digest> # <tag>` on its own line (an exact `${{ inputs.* }}` or `${{ matrix.* }}` image passes: the caller pins it).
  - **Runners.** Every reusable workflow takes `runner` (`onepeek`, `canary` or `github`, default `github`) and maps it, with the job's class, to `runs-on` itself; callers pass `runner: ${{ vars.RUNNER || 'github' }}` and never a label. Jobs that can hold a prod credential stay GitHub-hosted. `scripts/check_runner_labels.py` enforces the rules (`onepeek-docs` `architecture/lld/infra/ci-runners.md` §5.3-5.4); the prod-privileged job list is `.github/workflow-lint/github-only-jobs.yaml`.
  - `PULL_REQUEST_TEMPLATE.md`: what changed, why, the docs link and how it was tested.
  - `CONTRIBUTING.md` and `SECURITY.md`: how to contribute and how to report a vulnerability.
  - `profile/`: the organisation's public profile page.
  - `pins.yml` (every repo calls it): the repo's `OnePeek/.github` pins are all one `v*` tag's commit (logic in `scripts/check_pins.py`; the docs-pin rule is gone, DOC-1). `depends-on.yml` and `scripts/check_depends_on.py` are gone (PIPE-SIMPLE v2 DOC-3, v1.28.0): the merge tool (`vanguard ship`, vanguard `tools/depends_on.py`) reads a `Depends on:` line itself. `spot-rerun.yml` (R1 of `ci-runners.md` §4; a repo's `spot-rerun-caller.yml` runs it on `workflow_run: completed` with conclusion failure for its CI workflows): when a failed job's log shows a spot reclaim and the run is on attempt 1, it re-runs the failed jobs once and writes the job names to the run summary (logic in `scripts/spot_rerun.py`). `ready.yml` (`ci / ready`): the one summary job every CI workflow ends in; it fails on any failed, cancelled, unknown or unlisted-skipped job and prints a table of job, result and how to reproduce locally. `fast-checks.yml` `docs: true` input: relative links, front matter, code fences (`actions/docs`). `package-cleanup.yml` (weekly, in each publishing repo): old `-next.*` and `0.0.0-pr<N>.*` npm versions go (§8a C3). Workflows that read other repos use the agents' App token (`AGENTS_APP_CLIENT_ID`/`AGENTS_APP_PRIVATE_KEY`, §8a S6); there is no fallback token (`ORG_READ_TOKEN` and `INFRA_READ_TOKEN` are retired).
  - `scripts/check_caller_inputs.py` (delivery-pipelines §6.4a, F35): every job-level call of a `OnePeek/.github` reusable workflow (read at its pin in the `.github` checkout) or a local `./.github/workflows/<file>` passes each `required: true` input (a default does not excuse it) and secret (or `secrets: inherit`), and no undeclared input or secret; a pin, file or workflow it cannot read fails. `pins.yml` runs it on every pull request (`--shared _shared`, or `.` inside `.github`); `vanguard local-ci` runs it in `shared_checks()` with `--shared $ONEPEEK_WORKSPACE/.github`.
- Principles: **mirror the standards** (these files repeat `engineering-standards.md` in short form; when the standards change, these change with them). **Public by design** (nothing here may hold a secret, an internal link or personal data).

## Build kit
- Units: none (no Build-kit unit); builds work from `onepeek-docs` `architecture/lld/infra/foundation-playbook.md` §8a directly.
- Packet: none.
- Repo-specific skills and agents: none.
- Pending founder decisions: none.

## Gates (run all before reporting; CI runs the same, `workflow-lint.yml`)
- `actionlint` — workflow syntax and expressions.
- `zizmor --min-severity medium .github/workflows` — workflow security lint.
- `python scripts/check_runner_labels.py --kind public --repo OnePeek/.github --list .github/workflow-lint/github-only-jobs.yaml .github/workflows` — runner label rules.
- `python -m unittest discover -s scripts -p 'test_*.py'` — every script test.
- `python scripts/check_service_list.py` and `python scripts/check_lock_limits.py` — the service list and the release-lock limits against `onepeek-infra` config.

## Branches and CI
- Trunk: `dev` (decision 176). Merge style: squash into `dev`; `tag-main.yml` (the name kept) cuts a `v*` tag on every merge.
- CI workflows: `workflow-lint.yml` (the gates above, plus `pins.yml`, `fast-checks.yml` called locally and `ready.yml` on itself).
- Deploys: none; callers pin a tag's commit.

## Status
The shared workflows are live; open work is in the orchestrator's task list.

## Repo traps
- Repos pin the shared workflows by full commit SHA with the tag as a trailing comment (`@<sha> # v1.4.0`), all pins one `v*` tag's commit (`scripts/check_pins.py`); never a moving tag, never a pin edited by hand (`python3 scripts/bump_pins.py <repo-dir> <tag> <sha>`).
- A tag is cut on every push to `dev`, so a merge with no workflow change still needs no pin round (pins need one `v*` tag, not the newest).
- Script copies inlined into workflows (for example `no-prerelease-deps.yml`) stay verbatim copies of `scripts/`; tests keep them identical.
- A new `required: true` input of a reusable workflow means updating every caller in the same change set (`scripts/check_caller_inputs.py`).

## Docs gaps
When the docs are silent or disagree, stop and fix the docs first (Vanguard `playbooks/build.md`, docs gaps).

## Never
- Put a secret, an internal link or personal data in this public repo.
- Name the prior internal product; canonical names come from founder decision 97.
- Commit, push, merge or deploy without an explicit instruction from a person.
