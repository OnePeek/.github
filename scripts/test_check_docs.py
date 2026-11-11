"""Tests for scripts/check_docs.py (the docs check of actions/docs; PIPE-SIMPLE v2 audit (b) §2c, §4c).
Run: python -m unittest discover -s scripts -p 'test_*.py'"""
from __future__ import annotations

import contextlib
import io
import pathlib
import shutil
import tempfile
import unittest

import check_docs as cd


class Case(unittest.TestCase):
    def repo(self, files: dict[str, str | bytes]) -> pathlib.Path:
        d = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, d, True)
        for name, text in files.items():
            (d / name).parent.mkdir(parents=True, exist_ok=True)
            (d / name).write_bytes(text if isinstance(text, bytes) else text.encode())
        return d

    def problems(self, files: dict[str, str | bytes]) -> list[str]:
        d = self.repo(files)
        return [p for f in cd.files(d) for p in cd.check_file(f, d.resolve())]


class Links(Case):
    def test_working_relative_links_pass(self) -> None:
        self.assertEqual(self.problems({
            "README.md": "[a](docs/a.md) ![img](docs/i.png) [dir](docs/) [anchor](docs/a.md#part) [q](docs/a.md?x=1)\n"
                         "[root](/docs/a.md) [spaced](<docs/b c.md>) [enc](docs/b%20c.md) [t](docs/a.md \"title\")\n"
                         "[ref]: docs/a.md\n",
            "docs/a.md": "[up](../README.md) [self](#top) [web](https://example.com/x.md) [mail](mailto:a@b.c)\n",
            "docs/b c.md": "x\n", "docs/i.png": "x"}), [])

    def test_a_missing_target_fails_with_file_and_line(self) -> None:
        got = self.problems({"docs/a.md": "ok\n[x](nope.md)\n", "README.md": "![i](gone.png)\n[r]: also-gone.md\n"})
        self.assertEqual(got, ["README.md:1: link 'gone.png' does not exist", "README.md:2: link 'also-gone.md' does not exist",
                               "docs/a.md:2: link 'nope.md' does not exist"])

    def test_a_link_out_of_the_repo_fails(self) -> None:
        self.assertEqual(self.problems({"README.md": "[x](../../etc/passwd)\n"}),
                         ["README.md:1: link '../../etc/passwd' leaves the repo"])

    def test_links_in_code_are_not_links(self) -> None:
        self.assertEqual(self.problems({"README.md": "`[x](nope.md)`\n```\n[y](nope.md)\n```\n~~~~\n[z](n.md)\n~~~~\n"}), [])

    def test_only_root_markdown_and_docs_are_read(self) -> None:
        self.assertEqual(self.problems({"src/README.md": "[x](nope.md)\n", "docs/n.txt": "[x](nope.md)\n"}), [])


class Hostile(Case):
    def test_a_newline_in_a_file_name_cannot_start_a_workflow_command(self) -> None:  # m2
        got = self.problems({"docs/a\n::warning::x.md": "[x](nope.md)\n"})
        self.assertEqual(len(got), 1)
        self.assertNotIn("\n", got[0])
        self.assertTrue(got[0].startswith("docs/a?::warning::x.md:1: "), got[0])


class FrontMatter(Case):
    def test_good_front_matter_passes_and_links_after_it_are_checked(self) -> None:
        text = "---\ntitle: A\ntags:\n  - x\n- y\n# note\n\nowner: me\n---\n[x](nope.md)\n"
        self.assertEqual(self.problems({"docs/a.md": text}), ["docs/a.md:10: link 'nope.md' does not exist"])

    def test_unclosed_or_unparsable_front_matter_fails(self) -> None:
        self.assertEqual(self.problems({"docs/a.md": "---\ntitle: A\n"}), ["docs/a.md:1: front matter has no closing `---` line"])
        self.assertEqual(self.problems({"docs/a.md": "---\njust words\n---\n"}),
                         ["docs/a.md:2: front matter line is not `key: value`: 'just words'"])

    def test_a_rule_line_later_is_not_front_matter(self) -> None:
        self.assertEqual(self.problems({"docs/a.md": "# T\n\n---\nwords\n"}), [])


class Fences(Case):
    def test_an_unclosed_fence_fails_naming_its_line(self) -> None:
        self.assertEqual(self.problems({"docs/a.md": "x\n```python\ncode\n"}), ["docs/a.md:2: code fence ``` is never closed"])

    def test_a_shorter_or_other_fence_does_not_close_it(self) -> None:
        self.assertEqual(self.problems({"docs/a.md": "````\n```\n~~~~\n"}), ["docs/a.md:1: code fence ```` is never closed"])
        self.assertEqual(self.problems({"docs/a.md": "````\n```\n`````\n"}), [])

    def test_a_closing_fence_with_text_after_it_does_not_close(self) -> None:
        self.assertEqual(self.problems({"docs/a.md": "```\n``` x\n"}), ["docs/a.md:1: code fence ``` is never closed"])

    def test_inline_triple_backticks_are_not_a_fence(self) -> None:
        self.assertEqual(self.problems({"docs/a.md": "``` a ``` b\n"}), [])


class Main(Case):
    def test_exit_codes_and_an_unreadable_file(self) -> None:
        good, bad = self.repo({"README.md": "ok\n"}), self.repo({"docs/a.md": b"\xff\xfe bad"})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cd.main([str(good)]), 0)
            self.assertEqual(cd.main([str(bad)]), 1)
            self.assertEqual(cd.main([str(good / "missing")]), 1)
        self.assertIn("::error::docs/a.md:1: cannot be read as UTF-8 text", out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cd.main([]), 2)


if __name__ == "__main__":
    unittest.main()
