"""Tests for workspace/ship_change_set.py: order, input format, pin rewrite, checks, conflicts, errors (no network)."""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import unittest
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "ship_change_set", pathlib.Path(__file__).resolve().parents[1] / "workspace" / "ship_change_set.py"
)
ship = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ship)


class Order(unittest.TestCase):
    def test_docs_then_github_then_infra_then_callers(self) -> None:
        given = ["onepeek-web", "mcp-athena", "backend-service", ".github", "onepeek-infra", "onepeek-docs",
                 "onepeek-design-system"]
        self.assertEqual(
            sorted(given, key=ship.rank),
            ["onepeek-docs", ".github", "onepeek-infra", "onepeek-design-system", "backend-service", "onepeek-web",
             "mcp-athena"],
        )

    def test_bases(self) -> None:
        self.assertEqual(ship.BASE.get("onepeek-docs"), "main")
        self.assertEqual(ship.BASE.get(".github"), "main")
        self.assertEqual(ship.BASE.get("backend-service", "dev"), "dev")


class Parse(unittest.TestCase):
    def test_repo_and_number(self) -> None:
        self.assertEqual(ship.parse(".github#22"), (".github", 22))
        self.assertEqual(ship.parse("onepeek-web#7"), ("onepeek-web", 7))

    def test_rejects_other_shapes(self) -> None:
        for bad in ("onepeek-web", "#3", "onepeek-web#x", "OnePeek/onepeek-web#3"):
            with self.assertRaises(SystemExit):
                ship.parse(bad)


class Pin(unittest.TestCase):
    def test_rewrites_only_shared_workflow_pins(self) -> None:
        old_sha, new_sha = "a" * 40, "b" * 40
        text = (
            f"    uses: OnePeek/.github/.github/workflows/contract.yml@{old_sha} # v1.1.2\n"
            f"    uses: actions/checkout@{old_sha} # v7\n"
        )
        out = ship.PIN.sub(lambda m: f"{m.group(1)}@{new_sha} # v1.1.3", text)
        self.assertIn(f"contract.yml@{new_sha} # v1.1.3", out)
        self.assertIn(f"actions/checkout@{old_sha} # v7", out)



def done(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class Checks(unittest.TestCase):
    def test_pending_checks_are_read_although_gh_exits_non_zero(self) -> None:
        with mock.patch.object(ship.subprocess, "run", return_value=done(8, '[{"name":"a","bucket":"pending"}]')):
            self.assertEqual(ship.read_checks("r", 1, True), [{"name": "a", "bucket": "pending"}])

    def test_none_required_is_empty_only_from_gh_own_message(self) -> None:
        msg = done(1, "", "no required checks reported on the 'x' branch")
        with mock.patch.object(ship.subprocess, "run", return_value=msg):
            self.assertEqual(ship.read_checks("r", 1, True), [])

    def test_any_other_failure_stops(self) -> None:
        with mock.patch.object(ship.subprocess, "run", return_value=done(1, "", "HTTP 502")), \
                self.assertRaises(SystemExit):
            ship.read_checks("r", 1, True)

    def test_without_required_checks_waits_on_all_but_the_ai_review(self) -> None:
        every = [{"name": "ci", "bucket": "pass"}, {"name": "CodeRabbit", "bucket": "pending"}]
        with mock.patch.object(ship, "read_checks", side_effect=lambda r, n, req: [] if req else every):
            self.assertEqual(ship.checks("r", 1), [{"name": "ci", "bucket": "pass"}])


class Conflicts(unittest.TestCase):
    def test_unknown_is_not_clean(self) -> None:
        with mock.patch.object(ship, "mergeable", return_value="UNKNOWN"), self.assertRaises(SystemExit):
            ship.no_conflicts([("r", 1)])

    def test_mergeable_passes(self) -> None:
        with mock.patch.object(ship, "mergeable", return_value="MERGEABLE"):
            ship.no_conflicts([("r", 1)])


class Errors(unittest.TestCase):
    def test_command_failure_stops_with_its_error_text(self) -> None:
        failed = done(1, "", "Pull request is not mergeable")
        with mock.patch.object(ship.subprocess, "run", return_value=failed), self.assertRaises(SystemExit) as stop:
            ship.gh("pr", "merge", "1")
        self.assertIn("not mergeable", str(stop.exception))


if __name__ == "__main__":
    unittest.main()
