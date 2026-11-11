# Changelog

One line per change a caller can see. Versions are the `v*` tags `tag-main.yml` cuts on every merge to `dev` (a `feat`
raises the minor version); the release notes on each tag list every merged pull request.

## v1.27.0 (PIPE-SIMPLE v2 GH-1)
- New `ready.yml`: the `ci / ready` summary a CI workflow ends in (fails on any failed, cancelled, unknown or unlisted-skipped job; table job | result | how to reproduce locally); new `actions/docs` and the `docs: true` input of `fast-checks.yml` (relative links, front matter, code fences); `spot-rerun.yml` gains a scheduled scan mode (`workflows` input) beside the `workflow_run` form; `pins` drops the `docs-version.txt` ancestor rule and `--docs` (DOC-1; inputs and secrets kept, unused); `depends-on.yml` and `check_depends_on.py` are deprecated and no longer read the docs pin (DOC-3; still working for callers on older tags).
