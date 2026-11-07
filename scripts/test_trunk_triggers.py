"""Decision 176 (one trunk, `dev`): this repo's trunk workflows run under either name until the rename is done."""
from __future__ import annotations

import pathlib
import unittest

import yaml

WF = pathlib.Path(__file__).resolve().parents[1] / ".github/workflows"


def load(name: str) -> dict:
    return yaml.safe_load((WF / name).read_text(encoding="utf-8"))


class TrunkTriggers(unittest.TestCase):
    def test_a_push_to_main_or_dev_cuts_the_tag(self) -> None:
        self.assertEqual(load("tag-main.yml")[True]["push"]["branches"], ["main", "dev"])

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
