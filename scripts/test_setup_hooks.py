import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workspace"))
import setup as s  # noqa: E402


class HookRepos(unittest.TestCase):
    def test_finds_every_clone_with_a_lefthook_file(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name, git, hook in [("onepeek-docs", 1, 1), ("a", 1, 0), ("b", 0, 1), (".github", 1, 1)]:
                (root / name).mkdir()
                if git:
                    (root / name / ".git").mkdir()
                if hook:
                    (root / name / "lefthook.yml").write_text("x: 1\n")
            (root / "file").write_text("")
            self.assertEqual([p.name for p in s.hook_repos(root)], [".github", "onepeek-docs"])


class SyncCheck(unittest.TestCase):
    """`--sync-repos --check` reports drift and writes nothing (2026-11-07); `--sync-repos` alone writes."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        here = self.root / ".github" / "workspace"
        (here / "skills" / "x").mkdir(parents=True)
        (here / "agents").mkdir()
        (here / "skills" / "x" / "SKILL.md").write_text("---\nname: x\n---\nbody\n")
        (self.root / "repo" / ".git").mkdir(parents=True)
        self.copy = self.root / "repo" / ".claude" / "skills" / "x" / "SKILL.md"
        for name, value in (("ROOT", self.root), ("SHARED_SKILLS", here / "skills"), ("SHARED_AGENTS", here / "agents"),
                            ("SYNC_REPOS", {"repo": ("x",)})):
            p = mock.patch.object(s, name, value)
            p.start()
            self.addCleanup(p.stop)

    def main(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            return s.main(list(argv)), out.getvalue()

    def test_check_reports_drift_and_writes_nothing(self):
        for state in ("missing", "differs"):
            with self.subTest(state):
                if state == "differs":
                    self.copy.parent.mkdir(parents=True)
                    self.copy.write_text("old\n")
                rc, out = self.main("--sync-repos", "--check")
                self.assertEqual(rc, 1)
                self.assertIn(f"drift   repo/.claude/skills/x/SKILL.md: {state}", out.replace("differs from", "differs"))
                self.assertEqual(self.copy.read_text() if self.copy.exists() else None,
                                 "old\n" if state == "differs" else None)

    def test_check_passes_when_in_sync_and_sync_alone_writes(self):
        self.assertEqual(self.main("--sync-repos")[0], 0)
        self.assertIn("Synced from OnePeek/.github", self.copy.read_text())
        rc, out = self.main("--sync-repos", "--check")
        self.assertEqual((rc, out.strip()), (0, "REPOS IN SYNC"))


if __name__ == "__main__":
    unittest.main()
