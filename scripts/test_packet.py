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
    def test_default_is_feature(self) -> None:
        self.assertEqual(packet.lane_of(""), "feature")
        self.assertEqual(packet.lane_of(None), "feature")
        self.assertEqual(packet.lane_of("Build identity.\nNo lane stated here"), "feature")
        self.assertEqual(packet.lane_of("Build identity.\nNo lane here, Lane: hotfix mentioned inline"), "feature")

    def test_a_lane_line_sets_it_in_any_case(self) -> None:
        self.assertEqual(packet.lane_of("fix it\nLane: hotfix\n"), "hotfix")
        self.assertEqual(packet.lane_of("fix it\r\nlane: HOTFIX\r\n"), "hotfix")
        self.assertEqual(packet.lane_of("lane: Feature  "), "feature")

    def test_a_bullet_and_trailing_words_are_fine_and_the_first_word_is_the_value(self) -> None:
        for text in ("- Lane: hotfix", "* Lane: hotfix (preprod down)", "Lane: **hotfix**", "x\n- lane: HOTFIX - restores it"):
            self.assertEqual(packet.lane_of(text), "hotfix", text)
        self.assertEqual(packet.lane_of("- Lane: feature (default)"), "feature")

    def test_only_a_column_0_line_outside_code_and_details_counts(self) -> None:  # ruling T-53 round 2, pitfall 12
        for text in ("```markdown\nLane: hotfix\n```", "~~~\nLane: hotfix\n~~~", "  ```\nLane: hotfix\n  ```",
                     "````\n```\nLane: hotfix\n````", "```\nLane: hotfix", "    Lane: hotfix", "  Lane: hotfix",
                     "  - Lane: hotfix", "<details><summary>x</summary>\n\nLane: hotfix\n</details>",
                     "<DETAILS>\nLane: hotfix", "## Lane: hotfix", "# Lane: hotfix", "> Lane: hotfix",
                     "`Lane: hotfix`", "We use Lane: hotfix here", "Lane\uff1a hotfix", "<p>Lane: hotfix</p>",
                     "- **Lane:** hotfix", "**Lane:** hotfix", "+ Lane: hotfix", "<!-- x\nLane: hotfix"):
            with self.subTest(text=text):
                self.assertEqual(packet.lane_of(text), "feature")
        self.assertEqual(packet.lane_of("```\nLane: feature\n```\nLane: hotfix"), "hotfix")
        self.assertEqual(packet.lane_of("<details>\nLane: feature\n</details>\nLane: hotfix"), "hotfix")

    def test_an_html_comment_is_ignored_also_beside_a_real_line(self) -> None:
        hint = "<!-- Lane: feature|hotfix -->"
        self.assertEqual(packet.lane_of(hint), "feature")
        self.assertEqual(packet.lane_of(f"{hint}\nLane: hotfix"), "hotfix")
        self.assertEqual(packet.lane_of("<!--\nLane: feature|hotfix\n-->\nLane: hotfix"), "hotfix")

    def test_an_unknown_or_conflicting_lane_is_an_error_never_the_default(self) -> None:
        for text in ("Lane: urgent", "Lane: hot fix", "Lane:", "Lane: hotfix\nLane: feature"):
            with self.assertRaises(ValueError):
                packet.lane_of(text)


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

    def test_docs_dir_precedence(self) -> None:
        repo = pathlib.Path("/w/repo")
        self.assertEqual(packet.docs_dir(repo, pathlib.Path("/d"), {"ONEPEEK_WORKSPACE": "/ws"}), pathlib.Path("/d").resolve())
        self.assertEqual(packet.docs_dir(repo, None, {"ONEPEEK_WORKSPACE": "/ws"}), pathlib.Path("/ws/onepeek-docs").resolve())
        self.assertEqual(packet.docs_dir(repo, None, {}), pathlib.Path("/w/onepeek-docs").resolve())
        self.assertEqual(packet.docs_dir(repo, None, {"ONEPEEK_WORKSPACE": ""}), pathlib.Path("/w/onepeek-docs").resolve())


if __name__ == "__main__":
    unittest.main()
