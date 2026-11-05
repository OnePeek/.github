"""Tests for workspace/packet.py: the `lane:` line (development-cycle §2b)."""
from __future__ import annotations

import importlib.util
import pathlib
import unittest

_SPEC = importlib.util.spec_from_file_location("packet", pathlib.Path(__file__).resolve().parents[1] / "workspace" / "packet.py")
packet = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(packet)


class Lane(unittest.TestCase):
    def test_default_is_feature(self) -> None:
        self.assertEqual(packet.lane_of(""), "feature")
        self.assertEqual(packet.lane_of(None), "feature")
        self.assertEqual(packet.lane_of("Build identity.\nNo lane stated here"), "feature")

    def test_a_lane_line_sets_it_in_any_case(self) -> None:
        self.assertEqual(packet.lane_of("fix it\nLane: hotfix\n"), "hotfix")
        self.assertEqual(packet.lane_of("  lane: Feature  "), "feature")

    def test_a_bullet_bold_and_trailing_words_are_fine_and_the_first_word_is_the_value(self) -> None:
        for text in ("- **Lane:** hotfix", "* Lane: hotfix (preprod down)", "Lane: **hotfix**", "x\n  - lane: HOTFIX - restores the release"):
            self.assertEqual(packet.lane_of(text), "hotfix", text)
        self.assertEqual(packet.lane_of("- **Lane:** feature (default)"), "feature")

    def test_an_unknown_or_conflicting_lane_is_an_error_never_the_default(self) -> None:
        for text in ("Lane: urgent", "Lane: hot fix", "Lane:", "Lane: hotfix\nLane: feature"):
            with self.assertRaises(ValueError):
                packet.lane_of(text)


if __name__ == "__main__":
    unittest.main()
