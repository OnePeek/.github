"""Tests for workspace/ship_change_set.py: the merge order, the input format and the pin rewrite (no network)."""
from __future__ import annotations

import importlib.util
import pathlib
import unittest

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


if __name__ == "__main__":
    unittest.main()
