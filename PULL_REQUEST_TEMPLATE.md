## Unit and step
<!-- onepeek-docs/standards/development-cycle.md: the unit (module, platform part, client piece, infra part), its step (2 design ready, 3 build, 5 ship), and confirmation that everything it depends on is finished. -->

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
- [ ] Docs first: the LLD, contracts and any PRD/TRD/HLD updates are merged, or none is needed (spec-to-code)
- [ ] Follows `code-guidelines.md` and this repo's guide: layers, no hard-coded values, typed inputs, tests
- [ ] `docs-version.txt` moved if contracts changed
- [ ] No secrets and no personal data in code, tests, fixtures or logs
- [ ] Tests and CI gates pass
- [ ] Title follows Conventional Commits (`feat:`, `fix:`, `chore:` …), targeting `dev` (or `release` for a release)
- [ ] New paid resource or service: monthly cost stated
- [ ] Terraform plan deletes or replaces a network, security, data or key resource: founder's go-ahead given
