"""Tests for scripts/check_pins.py and pins.yml (foundation-playbook §8a P2, S4): one tagged version (the docs rule is gone, DOC-1)."""
from __future__ import annotations

import contextlib
import io
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import yaml

import check_pins as cp

A, B, C, D = "a" * 40, "b" * 40, "c" * 40, "d" * 40
TAGS = f"{A}\trefs/tags/v1.7.0\n{B}\trefs/tags/v1.8.0\n{C}\trefs/tags/v1.8.1\n{D}\trefs/tags/v1.8.1^{{}}\n" \
       f"{A}\trefs/tags/not-a-version\n"
TRUNK = lambda token: "dev"  # noqa: E731  docs' trunk (decision 176)
WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / ".github/workflows/pins.yml"
ACTION = pathlib.Path(__file__).resolve().parents[1] / "actions/pins/action.yml"  # pins.yml's steps (decision 180)


def uses(sha: str, name: str = "secret-scan") -> str:
    return f"    uses: OnePeek/.github/.github/workflows/{name}.yml@{sha} # vX\n"


class Repo(unittest.TestCase):
    def repo(self, *files: str, docs: str | None = None) -> pathlib.Path:
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / ".github/workflows").mkdir(parents=True)
        for i, body in enumerate(files):
            (d / f".github/workflows/w{i}.yml").write_text("jobs:\n  j:\n" + body)
        if docs is not None:
            (d / "docs-version.txt").write_text(docs)
        return d


class Tags(unittest.TestCase):
    def test_v_tags_by_commit_and_an_annotated_tag_by_its_peeled_commit(self) -> None:
        by_sha = cp.parse_tags(TAGS)
        self.assertEqual(by_sha[A], ["v1.7.0"])
        self.assertEqual(by_sha[D], ["v1.8.1"])
        self.assertNotIn(C, by_sha)  # the annotated tag object is not a commit


class Pins(Repo):
    def problems(self, d: pathlib.Path) -> list[str]:
        return cp.pin_problems(cp.pins(d), cp.parse_tags(TAGS))

    def test_all_pins_at_one_tag_pass(self) -> None:
        self.assertEqual(self.problems(self.repo(uses(B) + uses(B, "contract"), uses(B, "pins"))), [])

    def test_a_pin_that_is_not_a_tag_commit_fails(self) -> None:  # S4
        out = self.problems(self.repo(uses("e" * 40)))
        self.assertTrue(any("w0.yml:3" in p and "not the commit of a v* tag" in p for p in out), out)

    def test_pins_on_two_tagged_versions_fail(self) -> None:  # P2
        out = self.problems(self.repo(uses(A), uses(B)))
        self.assertEqual(len(out), 1)
        self.assertIn("not one version", out[0])
        self.assertIn("v1.7.0", out[0])

    def test_other_actions_are_not_pins(self) -> None:
        d = self.repo(f"    uses: actions/checkout@{A} # v7\n", f"    uses: OnePeek/.github-other/x.yml@{A}\n")
        self.assertEqual(cp.pins(d), [])

    def test_a_pin_in_any_letter_case_counts(self) -> None:  # P2: every uses naming OnePeek/.github
        d = self.repo(f"    uses: onepeek/.GitHub/.github/workflows/secret-scan.yml@{'e' * 40}\n",
                      f"    - uses: 'ONEPEEK/.github/.github/workflows/pins.yml@{B}' # v1.8.0\n")
        self.assertEqual([(f, wf, ref) for f, _, wf, ref in cp.pins(d)],
                         [("w0.yml", "secret-scan.yml", "e" * 40), ("w1.yml", "pins.yml", B)])
        out = self.problems(d)
        self.assertTrue(any("w0.yml:3" in p and "not the commit of a v* tag" in p for p in out), out)

    def test_a_flow_style_pin_ends_the_ref_at_the_brace_or_comma(self) -> None:
        d = self.repo(f"    - {{uses: OnePeek/.github/.github/workflows/pins.yml@{B}}}\n",
                      f"    - {{uses: OnePeek/.github/.github/workflows/pins.yml@{B}, with: {{a: b}}}}\n")
        self.assertEqual([ref for _, _, _, ref in cp.pins(d)], [B, B])

    def test_a_branch_or_a_tag_name_fails(self) -> None:  # P2: a ref that is not a 40-character SHA
        for ref in ("main", "v1.8.0", B[:39], ""):
            body = f"    uses: OnePeek/.github/.github/workflows/secret-scan.yml{'@' + ref if ref else ''}\n"
            out = self.problems(self.repo(body))
            self.assertTrue(any("w0.yml:3" in p and "not a 40-character commit SHA" in p for p in out), (ref, out))

    def test_a_sha_that_is_not_a_tag_commit_fails_even_when_alone(self) -> None:  # S4
        out = self.problems(self.repo(uses("f" * 40) + uses("f" * 40, "pins")))
        self.assertEqual(len(out), 2)
        self.assertTrue(all("not the commit of a v* tag" in p for p in out), out)


class DocsRule(Repo):
    """The deprecated library functions Vanguard still imports (DOC-1 keeps them until VG-1); the check never runs them."""

    def test_an_ancestor_of_the_trunk_passes(self) -> None:
        for status in ("ahead", "identical"):
            d = self.repo(docs=f"# comment\nonepeek-docs@{A}\n")
            self.assertEqual(cp.docs_problems(d, "t", status=lambda sha, tok, s=status, **kw: s, trunk=TRUNK), [])

    def test_a_commit_off_the_trunk_fails(self) -> None:
        d = self.repo(docs=f"onepeek-docs@{A}\n")
        for status in ("diverged", "behind", "missing"):
            out = cp.docs_problems(d, "t", status=lambda sha, tok, s=status, **kw: s, trunk=TRUNK)
            self.assertEqual(len(out), 1, status)

    def test_the_message_names_the_trunk_the_check_read(self) -> None:  # .github#90 S1
        d = self.repo(docs=f"onepeek-docs@{A}\n")
        for branch in ("main", "dev"):
            seen: list = []
            out = cp.docs_problems(d, "t", status=lambda sha, tok, trunk: seen.append(trunk(tok)) or "diverged",
                                   trunk=lambda tok, b=branch: b)
            self.assertEqual(seen, [branch])  # the compare uses the same trunk the message names
            self.assertIn(f"not an ancestor of onepeek-docs {branch} (diverged)", out[0])
            self.assertIn(f"move the pin to a commit on {branch}", out[0])
            if branch == "dev":
                self.assertNotIn("main", out[0])

    def test_the_compare_reads_the_trunk_against_the_pinned_commit(self) -> None:
        seen = []
        cp.docs_problems(self.repo(docs=f"onepeek-docs@{A}\n"), "tok", status=lambda sha, tok, **kw: seen.append((sha, tok)) or "ahead",
                         trunk=TRUNK)
        self.assertEqual(seen, [(A, "tok")])

    def test_no_file_is_nothing_to_check_and_a_file_without_a_pin_fails(self) -> None:
        self.assertEqual(cp.docs_problems(self.repo(), "t"), [])
        self.assertEqual(len(cp.docs_problems(self.repo(docs="# Replace with the onepeek-docs commit SHA\n"), "t")), 1)

    def test_no_token_fails_instead_of_passing(self) -> None:
        self.assertEqual(len(cp.docs_problems(self.repo(docs=f"onepeek-docs@{A}\n"), "")), 1)



class Answer:
    """A fake urlopen answer: a context manager whose body json.load reads."""
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        import io
        return io.BytesIO(self.body)

    def __exit__(self, *exc) -> None:
        return None


def opener(body: bytes | None = None, error: Exception | None = None, seen: list | None = None):
    def _open(req, timeout):
        if seen is not None:
            seen.append(req.full_url)
        if error is not None:
            raise error
        return Answer(body)
    return _open


class DocsTrunk(unittest.TestCase):
    """Decision 176: the docs trunk is read from the API (`dev`), never assumed; an unreadable answer fails."""

    def test_whatever_branch_the_api_names_is_read_and_compared(self) -> None:
        seen: list = []
        self.assertEqual(cp.docs_trunk("t", opener=opener(b'{"default_branch": "trunk"}', seen=seen)), "trunk")
        self.assertEqual(seen, ["https://api.github.com/repos/OnePeek/onepeek-docs"])
        urls: list = []
        st = cp.compare_status(A, "t", trunk=lambda tok: "trunk", opener=opener(b'{"status": "ahead"}', seen=urls))
        self.assertEqual((st, urls), ("ahead", [f"https://api.github.com/repos/OnePeek/onepeek-docs/compare/{A}...trunk"]))

    def test_dev_is_the_compare_base(self) -> None:
        self.assertEqual(cp.docs_trunk("t", opener=opener(b'{"default_branch": "dev"}')), "dev")
        urls: list = []
        st = cp.compare_status(A, "t", trunk=lambda tok: "dev", opener=opener(b'{"status": "identical"}', seen=urls))
        self.assertEqual((st, urls), ("identical", [f"https://api.github.com/repos/OnePeek/onepeek-docs/compare/{A}...dev"]))

    def test_compare_status_reads_the_trunk_by_default(self) -> None:
        urls: list = []
        with mock.patch.object(cp, "docs_trunk", return_value="dev") as trunk:
            cp.compare_status(A, "tok", opener=opener(b'{"status": "ahead"}', seen=urls))
        trunk.assert_called_once_with("tok")
        self.assertTrue(urls[0].endswith(f"/compare/{A}...dev"), urls)

    def test_an_unreadable_answer_fails_naming_it_and_never_guesses_main(self) -> None:
        import urllib.error
        cases = {
            "404": opener(error=urllib.error.HTTPError("u", 404, "Not Found", {}, None)),
            "500": opener(error=urllib.error.HTTPError("u", 500, "err", {}, None)),
            "timeout": opener(error=TimeoutError("timed out")),
            "urlerror": opener(error=urllib.error.URLError("no route")),
            "not json": opener(b"<html>"),
            "a list": opener(b"[]"),
            "no key": opener(b'{"name": "onepeek-docs"}'),
            "null": opener(b'{"default_branch": null}'),
            "a number": opener(b'{"default_branch": 7}'),
        }
        for name, op in cases.items():
            with self.assertRaises(SystemExit, msg=name) as e:
                cp.docs_trunk("t", opener=op)
            self.assertIn("cannot read the default branch of onepeek-docs", str(e.exception.code), name)

    def test_a_hostile_branch_name_fails_closed(self) -> None:
        for value in ("../x", "..", "a..b", "dev main", " dev", "dev\n", "", "x/y", "-dev", "dev?x=1", "dev#x", "a" * 101):
            body = ('{"default_branch": %s}' % __import__("json").dumps(value)).encode()
            with self.assertRaises(SystemExit, msg=value):
                cp.docs_trunk("t", opener=opener(body))

    def test_a_failed_trunk_read_fails_the_docs_rule(self) -> None:
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "docs-version.txt").write_text(f"onepeek-docs@{A}\n")
        with mock.patch.object(cp, "docs_trunk", side_effect=SystemExit("cannot read the default branch of onepeek-docs: HTTP 404")):
            with self.assertRaises(SystemExit):
                cp.docs_problems(d, "t", status=cp.compare_status)

class Main(Repo):
    def test_exit_status_follows_the_problems(self) -> None:
        good, bad = self.repo(uses(B)), self.repo(uses(A), uses(B))
        with mock.patch.object(cp, "remote_tags", return_value=cp.parse_tags(TAGS)):
            self.assertEqual(cp.main([str(good)]), 0)
            self.assertEqual(cp.main([str(bad)]), 1)

    def test_the_docs_rule_never_runs_with_or_without_docs_version_txt(self) -> None:  # DOC-1
        for docs in (None, f"onepeek-docs@{A}\n", "garbage\n"):
            d = self.repo(uses(B), docs=docs)
            env = {"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/dev", "GH_TOKEN": ""}
            with mock.patch.object(cp, "remote_tags", return_value=cp.parse_tags(TAGS)), mock.patch.dict(cp.os.environ, env), \
                    mock.patch.object(cp, "docs_trunk", side_effect=AssertionError("no docs read")), \
                    mock.patch.object(cp, "compare_status", side_effect=AssertionError("no docs read")), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cp.main([str(d)]), 0, docs)
            self.assertNotIn("docs-version", out.getvalue())

    def test_the_docs_flags_are_gone(self) -> None:  # DOC-1
        d = self.repo(uses(B))
        for argv in ([str(d), "--docs", "auto"], ["--docs-needed"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as e:
                cp.main(argv)
            self.assertEqual(e.exception.code, 2, argv)

    def test_the_library_vanguard_imports_is_still_there(self) -> None:  # removed only after VG-1 stops importing it
        for name in ("USES", "SHA", "DOCS_PIN", "pins", "pin_problems", "remote_tags", "parse_tags", "docs_pin",
                     "docs_trunk", "compare_status", "docs_problems"):
            self.assertTrue(hasattr(cp, name), name)
        self.assertIn("trunk", cp.compare_status.__code__.co_varnames)


class Workflow(unittest.TestCase):
    def test_the_action_runs_no_docs_rule_and_keeps_its_inputs(self) -> None:  # DOC-1, no caller changes
        doc = yaml.safe_load(ACTION.read_text())
        self.assertEqual(sorted(doc["inputs"]), ["app-client-id", "app-private-key"])
        text = ACTION.read_text()
        for gone in ("--docs", "create-github-app-token", "docs-version.txt is on"):
            self.assertNotIn(gone, text)
        self.assertTrue(doc["runs"]["steps"][-1]["run"].rstrip().endswith('/scripts/check_pins.py" .'))
        wf = yaml.safe_load(WORKFLOW.read_text())
        self.assertEqual(sorted(wf[True]["workflow_call"]["secrets"]), ["AGENTS_APP_CLIENT_ID", "AGENTS_APP_PRIVATE_KEY"])

    def test_the_version_step_finds_the_callers_pin_of_pins_yml(self) -> None:
        step = next(s for s in yaml.safe_load(WORKFLOW.read_text())["jobs"]["pins"]["steps"] if s.get("id") == "version")
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / ".github/workflows").mkdir(parents=True)
        (d / ".github/workflows/c.yml").write_text(uses(A, "secret-scan") + uses(B, "pins"))
        out = d / "out"
        env = {"GITHUB_OUTPUT": str(out), "PATH": "/usr/bin:/bin", "FILE": "pins.yml", "RUN_SHA": "e" * 40}
        r = subprocess.run(["bash", "-eo", "pipefail", "-c", step["run"]], cwd=d, env={**env, "REPO": "OnePeek/onepeek-web"},
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(out.read_text().strip(), f"sha={B}")
        out.unlink()
        (d / ".github/workflows/c.yml").write_text("jobs: {}\n")  # a local call (this repo): no pin, this run's commit
        r = subprocess.run(["bash", "-eo", "pipefail", "-c", step["run"]], cwd=d, env={**env, "REPO": "OnePeek/.github"},
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(out.read_text().strip(), "sha=" + "e" * 40)


if __name__ == "__main__":
    unittest.main()
