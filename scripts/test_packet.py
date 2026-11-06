"""Tests for workspace/packet.py: the `lane:` line (development-cycle §2b) and where the docs clone is found."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

_SPEC = importlib.util.spec_from_file_location("packet", pathlib.Path(__file__).resolve().parents[1] / "workspace" / "packet.py")
packet = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(packet)


def setUpModule() -> None:
    """Clear Git's repository-local variables (`git rev-parse --local-env-vars`: GIT_DIR, GIT_INDEX_FILE and the rest),
    as a hook passes them on, so the fixtures and the code under test touch only the fixture repos (CodeRabbit on #60:
    an inherited GIT_DIR had the fixture rewrite another repo's config)."""
    names = subprocess.run(["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True, check=True).stdout.split()
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    for name in names:
        os.environ.pop(name, None)


class Lane(unittest.TestCase):
    """Ruling T-53 round 3 (pitfall 12): the first line after blank lines and leading whole-line HTML comments alone
    decides, and only when it is exactly `Lane: feature|hotfix|docs`; anything else is feature (fail closed)."""

    def test_a_plain_first_line_sets_the_lane(self) -> None:
        for text in ("Lane: hotfix", "Lane: hotfix\nfix it", "\n \n\t\nLane: hotfix\nx", "<!-- Lane: feature|hotfix -->\nLane: hotfix",
                     "<!--\nLane: feature\n-->\n\nLane: hotfix", "<!-- a -->  \n<!-- b\n-->\nLane: hotfix",
                     "Lane: hotfix\r\nfix it\r\n", "\r\nLane: hotfix\r", "lane: HOTFIX", "LANE:hotfix \t",
                     "Lane: hotfix\nLane: feature", "Lane: hotfix\n\n---"):
            with self.subTest(text=text):
                self.assertEqual(packet.lane_of(text), "hotfix")
        self.assertEqual(packet.lane_of("Lane: docs\nwording"), "docs")
        self.assertEqual(packet.lane_of("lane: Feature  "), "feature")

    def test_anything_else_is_feature(self) -> None:
        for text in ("", None, {"x": 1}, b"Lane: hotfix", "\n\n", "<!-- only a hint -->",
                     # review round 3: closing fence with trailing text, nested details, setext heading, lazy list line
                     "```\n```python\nLane: hotfix\n```\n```", "<details>\n<details>\nx\n</details>\nLane: hotfix\n</details>",
                     "Lane: hotfix\n---", "Lane: hotfix\n===", "Lane: hotfix\n   -", "- item\nLane: hotfix",
                     # round 2 forms: fence, indent, details, heading, quote, bold, bullet, inline code, prose first
                     "```markdown\nLane: hotfix\n```", "~~~\nLane: hotfix\n~~~", "    Lane: hotfix", "  Lane: hotfix", "\tLane: hotfix",
                     "<details><summary>x</summary>\n\nLane: hotfix\n</details>", "## Lane: hotfix", "# Lane: hotfix",
                     "> Lane: hotfix", "**Lane:** hotfix", "Lane: **hotfix**", "- Lane: hotfix", "* Lane: hotfix", "+ Lane: hotfix",
                     "`Lane: hotfix`", "We use Lane: hotfix here", "fix it\nLane: hotfix", "Lane\uff1a hotfix", "<p>Lane: hotfix</p>",
                     # a value that is not exactly a lane
                     "Lane: hotfix (preprod down)", "Lane: hot fix", "Lane: urgent", "Lane:", "Lane: hotfix.", "\ufeffLane: hotfix",
                     # comments that do not end cleanly before the first line
                     "<!-- x\nLane: hotfix", "<!-- x --> Lane: hotfix", "<!-->\nLane: hotfix\n-->", "<!--->\nLane: hotfix\n-->",
                     "<!-- x --!>\nLane: hotfix\n-->", " <!-- x -->\nLane: hotfix",
                     # each fail-closed comment rule, with a clean lane line after the comment (review of #73)
                     "<!-- x --> foo\nLane: hotfix", "<!-->\nfoo\n-->\nLane: hotfix", "<!--->\nfoo\n-->\nLane: hotfix",
                     "<!-- x --!>\nfoo\n-->\nLane: hotfix",
                     # Unicode case folding must not turn a near-lane into a lane (dotless i, dotted capital I, long s)
                     "Lane: hotf\u0131x", "Lane: hotf\u0130x", "Lane: doc\u017f"):
            with self.subTest(text=text):
                self.assertEqual(packet.lane_of(text), "feature")


def git(cwd: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


class DocsClone(unittest.TestCase):
    """`--docs`, else `$ONEPEEK_WORKSPACE/onepeek-docs`, else `../onepeek-docs`; always at the repo's own pin."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.docs = self.root / "clones" / "onepeek-docs"
        self.docs.mkdir(parents=True)
        git(self.docs, "init", "-q", "-b", "main")
        for k, v in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
            git(self.docs, "config", k, v)
        self.shas = []
        for version in ("pinned", "newer"):
            (self.docs / "tools").mkdir(exist_ok=True)
            (self.docs / "tools" / "context.py").write_text(
                f"import sys\nprint('docs {version} for', sys.argv[1])\n")
            git(self.docs, "add", "tools/context.py")
            git(self.docs, "commit", "-q", "-m", version)
            self.shas.append(git(self.docs, "rev-parse", "HEAD"))
        self.repo = self.root / "scratchpad" / "kit"  # a worktree with no onepeek-docs beside it
        self.repo.mkdir(parents=True)
        (self.repo / "docs-version.txt").write_text(f"onepeek-docs@{self.shas[0]}\n")

    def packet(self, *extra: str, env: dict | None = None) -> tuple[int, str]:
        out = self.repo / ".context" / "p.md"
        err = io.StringIO()
        with mock.patch.dict(os.environ, env or {}, clear=False), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            if env is None:
                os.environ.pop("ONEPEEK_WORKSPACE", None)
            code = packet.main(["infra/aws-stack", "--repo", str(self.repo), "--out", str(out), *extra])
        return code, out.read_text() if code == 0 else err.getvalue()

    def test_docs_flag_reads_the_worktrees_own_pin_not_the_clones_head(self) -> None:
        code, text = self.packet("--docs", str(self.docs))
        self.assertEqual(code, 0, text)
        self.assertEqual(text, "lane: feature\ndocs pinned for infra/aws-stack\n")
        self.assertEqual(git(self.docs, "worktree", "list").count("\n"), 0)  # the temporary docs worktree is removed

    def test_onepeek_workspace_finds_the_docs_clone(self) -> None:
        code, text = self.packet(env={"ONEPEEK_WORKSPACE": str(self.docs.parent)})
        self.assertEqual(code, 0, text)
        self.assertIn("docs pinned", text)

    def test_docs_flag_wins_over_onepeek_workspace(self) -> None:
        code, text = self.packet("--docs", str(self.docs), env={"ONEPEEK_WORKSPACE": str(self.root / "nowhere")})
        self.assertEqual(code, 0, text)

    def test_without_either_the_sibling_folder_is_used_and_its_absence_is_an_error(self) -> None:
        code, text = self.packet()
        self.assertEqual(code, 1)
        self.assertIn(str((self.root / "scratchpad" / "onepeek-docs").resolve()), text)
        sibling = self.root / "clones" / "repo"
        sibling.mkdir()
        (sibling / "docs-version.txt").write_text(f"onepeek-docs@{self.shas[1]}\n")
        self.repo = sibling
        code, text = self.packet()
        self.assertEqual(code, 0, text)
        self.assertIn("docs newer", text)

    def test_the_task_and_pr_first_line_is_the_packets_first_line(self) -> None:  # ruling T-53 round 3
        task = self.root / "task.md"
        for body, want in (("Lane: hotfix\nfix CI", "hotfix"), ("fix CI\nLane: hotfix", "feature"), ("Lane: docs", "docs")):
            task.write_text(body)
            code, text = self.packet("--docs", str(self.docs), "--task", str(task))
            self.assertEqual((code, text.splitlines()[0]), (0, f"lane: {want}"), body)
        view = subprocess.CompletedProcess([], 0, "<!-- Lane: feature|hotfix -->\nLane: hotfix\n", "")
        with mock.patch.object(packet, "run", side_effect=lambda a: view if a[0] == "gh" else subprocess.run(
                a, capture_output=True, text=True, check=False)):
            code, text = self.packet("--docs", str(self.docs), "--pr", "onepeek-web#3")
        self.assertEqual((code, text.splitlines()[0]), (0, "lane: hotfix"))

    def test_docs_dir_precedence(self) -> None:
        repo = pathlib.Path("/w/repo")
        self.assertEqual(packet.docs_dir(repo, pathlib.Path("/d"), {"ONEPEEK_WORKSPACE": "/ws"}), pathlib.Path("/d").resolve())
        self.assertEqual(packet.docs_dir(repo, None, {"ONEPEEK_WORKSPACE": "/ws"}), pathlib.Path("/ws/onepeek-docs").resolve())
        self.assertEqual(packet.docs_dir(repo, None, {}), pathlib.Path("/w/onepeek-docs").resolve())
        self.assertEqual(packet.docs_dir(repo, None, {"ONEPEEK_WORKSPACE": ""}), pathlib.Path("/w/onepeek-docs").resolve())


if __name__ == "__main__":
    unittest.main()
