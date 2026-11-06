import sys
import tempfile
import io
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


class SettingsWithoutTheGuard(unittest.TestCase):
    """setup.py must not end green when the user's own .claude/settings.json leaves the P19 hook out (review of #55, 2026-11-06)."""

    def run_setup(self, own_settings: str):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            src = root / "src-settings.json"
            src.write_text("{}\n")
            dst = root / ".claude" / "settings.json"
            dst.parent.mkdir()
            dst.write_text(own_settings)
            out = io.StringIO()
            with mock.patch.object(s, "ROOT", root), mock.patch.object(s, "wanted", return_value={dst: src}), \
                    mock.patch.object(s, "sources", return_value=({}, {}, [])), mock.patch.object(s, "stale", return_value=[]), \
                    mock.patch.object(s, "install_hooks", return_value=0), mock.patch.object(s, "print_hygiene_hints"), \
                    mock.patch.object(sys, "argv", ["setup.py"]), redirect_stdout(out):
                return s.main(), out.getvalue(), dst.read_text()

    def test_an_own_settings_file_without_the_hook_fails_setup_and_is_kept(self):
        rc, text, kept = self.run_setup('{"model": "x"}')
        self.assertEqual(rc, 1)
        self.assertIn("FAIL", text)
        self.assertIn("P19", text)
        self.assertEqual(kept, '{"model": "x"}')

    def test_an_own_settings_file_that_registers_the_hook_passes(self):
        rc, text, _ = self.run_setup('{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 .github/workspace/hooks/p19_guard.py"}]}]}}')
        self.assertEqual(rc, 0, text)


if __name__ == "__main__":
    unittest.main()
