# Changelog

One line per change a caller can see. Versions are the `v*` tags `tag-main.yml` cuts on every merge to `dev` (a `feat`
raises the minor version); the release notes on each tag list every merged pull request.

## v1.28.4 (W1-GH: F589, C11)
- `release-ecs.yml`, `operate.yml`, `release-lock.yml` (the shared lock functions): the put-back of a `rolling` key no longer fails on a recorded revision that was deregistered since (an infra apply replacing a skeleton revision leaves it INACTIVE, and ECS refuses `UpdateService` onto it): it describes the revision, registers it again from its recorded definition and tags, and moves the service to the new revision. A revision it cannot describe, of another family, with no container, in any other status, or whose registration fails or answers another family, fails the put-back closed (the key stays `rolling`, as before). The deploy role needs `ecs:DescribeTaskDefinition` and `ecs:RegisterTaskDefinition` (it already has both for the rollout).
- `release-ecs.yml` verify: when none of the rolled services' deploy alarms exists, the 10-minute watch is skipped with the same warning (it could not detect a regression) instead of waiting 10 minutes; with any alarm it watches as before, and an unreadable alarm list still fails the step. Callers pick both up when their pin moves to the tag cut from this merge.

## v1.28.2 (F558)
- `spot-rerun.yml`: a run lost to a spot reclaim on attempt 2 is re-run once more (attempts 1 and 2 are re-run, so at most 3 attempts); a third loss stays failed and is reported in the summary of its own `workflow_run` call (scan mode lists attempts 1 and 2 only, so it skips a third loss silently, F562). Still gated on the reclaim markers and every existing guard (superseded run, failed jobs only). Scan mode now lists failed runs on attempt 1 or 2. Callers pick it up when their pin moves to the tag cut from this merge.

## v1.28.1 (SWEEP-B F474c)
- `spot-rerun.yml`: a failed job on an `onepeek-*` runner whose log has both a Docker socket EOF line and "The operation was canceled." now counts as a lost runner and is re-run once (the cancel line alone never does). Callers pick it up when their pin moves to the tag cut from this merge.

## v1.28.0 (PIPE-SIMPLE v2 A2b)
- Removed `depends-on.yml` and `scripts/check_depends_on.py` (DOC-3 final): no repo's `dev` calls them, and the merge tool (`vanguard ship`) reads a `Depends on:` line itself (`vanguard` `tools/depends_on.py`). A caller still naming `depends-on.yml` must drop that job before moving its pin to this tag; older tags keep the file. `workflow-lint.yml` loses its own `depends-on` job (`ready` needs `lint` and `fast`).

## v1.27.0 (PIPE-SIMPLE v2 GH-1)
- New `ready.yml`: the `ci / ready` summary a CI workflow ends in (fails on any failed, cancelled, unknown or unlisted-skipped job; table job | result | how to reproduce locally); new `actions/docs` and the `docs: true` input of `fast-checks.yml` (relative links, front matter, code fences); `spot-rerun.yml` gains a scheduled scan mode (`workflows` input) beside the `workflow_run` form; `pins` drops the `docs-version.txt` ancestor rule and `--docs` (DOC-1; inputs and secrets kept, unused); `depends-on.yml` and `check_depends_on.py` are deprecated and no longer read the docs pin (DOC-3; still working for callers on older tags).
