## Unit and step
<!-- onepeek-docs/standards/development-cycle.md: the unit (module, platform part, client piece, infra part), its step (2 design ready, 3 build, 5 ship), and confirmation that everything it depends on is finished. -->

## Change set
<!-- onepeek-docs/standards/development-cycle.md §2a: the unit's docs pull request and the pull request in every other repo it touches. They merge together: docs first (merge commit), then code. "Set of one" if this is the only one. -->

## What changed

## Why

## Docs
<!-- Link the onepeek-docs pull request or commit this implements (engineering standards §1). Write "none" only for a bug fix that changes no documented behaviour. -->

## Implements
<!-- Code pull requests (spec-to-code standard): the LLD (`onepeek-docs/architecture/lld/<area>/<unit>.md`) and the onepeek-docs commit pinned in `docs-version.txt`. Docs pull requests that change trd/, decisions/ or architecture/: "architecture map republished". Bug fixes with no documented change: "none". -->

## Docs read
<!-- Mandatory (code-guidelines §1): this repo's AGENTS.md and docs/ guide, onepeek-docs/standards/code-guidelines.md, and the spec sections for this change. -->

## How it was tested
<!-- Commands and results. Say so plainly if something failed or was skipped. -->

## Checklist
- [ ] Docs first: the LLD, contracts and any PRD/TRD/HLD updates are committed on the change set's docs pull request, which merges first (development-cycle §2a), or none is needed
- [ ] Follows `code-guidelines.md` and this repo's guide: layers, no hard-coded values, typed inputs, tests
- [ ] `docs-version.txt` points at the docs pull request's latest commit (or its merge on `main`)
- [ ] No secrets and no personal data in code, tests, fixtures or logs
- [ ] Tests and CI gates pass
- [ ] Title follows Conventional Commits (`feat:`, `fix:`, `chore:` …), targeting `dev` (or `release` for a release)
- [ ] New paid resource or service: monthly cost stated
- [ ] Terraform plan deletes or replaces a network, security, data or key resource: founder's go-ahead given
