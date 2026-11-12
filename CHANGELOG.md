# Changelog

One line per change a caller can see. Versions are the `v*` tags `tag-main.yml` cuts on every merge to `dev` (a `feat`
raises the minor version); the release notes on each tag list every merged pull request.

## v1.28.0 (PIPE-SIMPLE v2 A2b)
- Removed `depends-on.yml` and `scripts/check_depends_on.py` (DOC-3 final): no repo's `dev` calls them, and the merge tool (`vanguard ship`) reads a `Depends on:` line itself (`vanguard` `tools/depends_on.py`). A caller still naming `depends-on.yml` must drop that job before moving its pin to this tag; older tags keep the file. `workflow-lint.yml` loses its own `depends-on` job (`ready` needs `lint` and `fast`).

## v1.27.0 (PIPE-SIMPLE v2 GH-1)
- New `ready.yml`: the `ci / ready` summary a CI workflow ends in (fails on any failed, cancelled, unknown or unlisted-skipped job; table job | result | how to reproduce locally); new `actions/docs` and the `docs: true` input of `fast-checks.yml` (relative links, front matter, code fences); `spot-rerun.yml` gains a scheduled scan mode (`workflows` input) beside the `workflow_run` form; `pins` drops the `docs-version.txt` ancestor rule and `--docs` (DOC-1; inputs and secrets kept, unused); `depends-on.yml` and `check_depends_on.py` are deprecated and no longer read the docs pin (DOC-3; still working for callers on older tags).
