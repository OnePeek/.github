import sys
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
