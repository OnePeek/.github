import tempfile
import unittest
from pathlib import Path

import bump_pins as b

OLD = "a" * 40
NEW = "b" * 40


class T(unittest.TestCase):
    def test_bumps_sha_and_comment(self):
        t = f"    uses: OnePeek/.github/.github/workflows/node-checks.yml@{OLD} # shared workflows, main 2026-10-27\n"
        self.assertEqual(b.bump(t, "v1.2.0", NEW), f"    uses: OnePeek/.github/.github/workflows/node-checks.yml@{NEW} # v1.2.0\n")

    def test_adds_comment_when_missing_and_leaves_others(self):
        t = f"uses: OnePeek/.github/.github/workflows/x.yml@{OLD}\nuses: actions/checkout@{OLD} # v7\n"
        out = b.bump(t, "v1.2.0", NEW)
        self.assertIn(f"x.yml@{NEW} # v1.2.0\n", out)
        self.assertIn(f"actions/checkout@{OLD} # v7", out)

    def test_bumps_the_action_pin_too(self):
        t = f"      - uses: OnePeek/.github/actions/peer-ref@{OLD} # v1.1.0\n      - uses: OnePeek/.github/actions-x/y@{OLD} # v1\n"
        out = b.bump(t, "v1.2.0", NEW)
        self.assertIn(f"actions/peer-ref@{NEW} # v1.2.0\n", out)
        self.assertIn(f"actions-x/y@{OLD} # v1", out)  # only OnePeek/.github/actions/<name> moves

    def test_main_rewrites_files_and_rejects_bad_args(self):
        with tempfile.TemporaryDirectory() as d:
            w = Path(d) / ".github/workflows"
            w.mkdir(parents=True)
            (w / "c.yml").write_text(f"uses: OnePeek/.github/.github/workflows/x.yml@{OLD}\n")
            self.assertEqual(b.main([d, "v1.2.0", NEW]), 0)
            self.assertIn(NEW, (w / "c.yml").read_text())
            self.assertEqual(b.main([d, "main", NEW]), 2)
            self.assertEqual(b.main([d, "v1.2.0", "abc"]), 2)


if __name__ == "__main__":
    unittest.main()
