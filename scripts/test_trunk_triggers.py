"""Decision 176 (one trunk, `dev`): this repo's trunk workflows run under either name until the rename is done."""
from __future__ import annotations

import pathlib
import re
import unittest

import yaml

WF = pathlib.Path(__file__).resolve().parents[1] / ".github/workflows"


def load(name: str) -> dict:
    return yaml.safe_load((WF / name).read_text(encoding="utf-8"))


def runs_if(expr: str | None, ctx: dict) -> bool:
    """A job's `if:` for the one form these jobs use, `<context path> == <context path>`; no `if:` runs the job. Any
    other form is refused, so a guard rewritten into something this cannot read fails the test instead of passing."""
    if expr is None:
        return True
    m = re.fullmatch(r"\s*(?:\$\{\{\s*)?([\w.]+)\s*==\s*([\w.]+)\s*(?:\}\}\s*)?", expr)
    if not m:
        raise AssertionError(f"unreadable if: {expr!r}")

    def get(path: str):
        v = ctx
        for k in path.split("."):
            v = v[k]
        return v
    return get(m.group(1)) == get(m.group(2))


class TrunkTriggers(unittest.TestCase):
    def test_a_push_to_main_or_dev_cuts_the_tag(self) -> None:
        self.assertEqual(load("tag-main.yml")[True]["push"]["branches"], ["main", "dev"])

    def test_only_a_push_to_the_default_branch_cuts_the_tag(self) -> None:  # .github#90 B1
        job = load("tag-main.yml")["jobs"]["tag"]
        cases = [  # (pushed branch, default branch, the tag job runs)
            ("main", "main", True),   # today: a merge to main
            ("dev", "main", False),   # dev created or pushed while main is still the default
            ("dev", "dev", True),     # after the rename
            ("main", "dev", False),   # a stray main after the rename
        ]
        for ref, default, runs in cases:
            ctx = {"github": {"ref_name": ref, "event": {"repository": {"default_branch": default}}}}
            self.assertEqual(runs_if(job.get("if"), ctx), runs, (ref, default))

    def test_workflow_lint_runs_on_pushes_and_pull_requests_into_main_or_dev(self) -> None:
        on = load("workflow-lint.yml")[True]
        self.assertEqual(on["push"]["branches"], ["main", "dev"])
        self.assertEqual(on["pull_request"]["branches"], ["main", "dev"])

    def test_the_image_bump_targets_the_default_branch_on_a_schedule_too(self) -> None:
        # A schedule event has no repository payload; it runs on the default branch, so ref_name is the fallback.
        base = load("ci-image-bump-caller.yml")["jobs"]["bump"]["with"]["base"]
        self.assertEqual(base, "${{ github.event.repository.default_branch || github.ref_name }}")


if __name__ == "__main__":
    unittest.main()
