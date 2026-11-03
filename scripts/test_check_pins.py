"""Tests for scripts/check_pins.py and pins.yml (foundation-playbook §8a P2, S4): one tagged version, docs on main."""
from __future__ import annotations

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
WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / ".github/workflows/pins.yml"


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
        d = self.repo(f"    uses: actions/checkout@{A} # v7\n")
        self.assertEqual(cp.pins(d), [])


class DocsRule(Repo):
    def test_runs_on_pushes_to_dev_and_release_and_release_pull_requests_only(self) -> None:
        yes = [{"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/dev"},
               {"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/release"},
               {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_BASE_REF": "release"}]
        no = [{"GITHUB_EVENT_NAME": "pull_request", "GITHUB_BASE_REF": "dev"},
              {"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main"},
              {"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/feat/x"},
              {"GITHUB_EVENT_NAME": "schedule"}]
        for env in yes:
            self.assertTrue(cp.docs_needed(env), env)
        for env in no:
            self.assertFalse(cp.docs_needed(env), env)

    def test_an_ancestor_of_main_passes(self) -> None:
        for status in ("ahead", "identical"):
            d = self.repo(docs=f"# comment\nonepeek-docs@{A}\n")
            self.assertEqual(cp.docs_problems(d, "t", status=lambda sha, tok, s=status: s), [])

    def test_a_commit_off_main_fails(self) -> None:
        d = self.repo(docs=f"onepeek-docs@{A}\n")
        for status in ("diverged", "behind", "missing"):
            out = cp.docs_problems(d, "t", status=lambda sha, tok, s=status: s)
            self.assertEqual(len(out), 1, status)

    def test_the_compare_reads_main_against_the_pinned_commit(self) -> None:
        seen = []
        cp.docs_problems(self.repo(docs=f"onepeek-docs@{A}\n"), "tok", status=lambda sha, tok: seen.append((sha, tok)) or "ahead")
        self.assertEqual(seen, [(A, "tok")])

    def test_no_file_is_nothing_to_check_and_a_file_without_a_pin_fails(self) -> None:
        self.assertEqual(cp.docs_problems(self.repo(), "t"), [])
        self.assertEqual(len(cp.docs_problems(self.repo(docs="# Replace with the onepeek-docs commit SHA\n"), "t")), 1)

    def test_no_token_fails_instead_of_passing(self) -> None:
        self.assertEqual(len(cp.docs_problems(self.repo(docs=f"onepeek-docs@{A}\n"), "")), 1)


class Main(Repo):
    def test_exit_status_follows_the_problems(self) -> None:
        good, bad = self.repo(uses(B)), self.repo(uses(A), uses(B))
        with mock.patch.object(cp, "remote_tags", return_value=cp.parse_tags(TAGS)):
            self.assertEqual(cp.main([str(good)]), 0)
            self.assertEqual(cp.main([str(bad)]), 1)

    def test_auto_skips_the_docs_rule_on_a_pull_request_into_dev(self) -> None:
        d = self.repo(uses(B), docs=f"onepeek-docs@{A}\n")
        env = {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_BASE_REF": "dev", "GH_TOKEN": ""}
        with mock.patch.object(cp, "remote_tags", return_value=cp.parse_tags(TAGS)), \
                mock.patch.dict(cp.os.environ, env), mock.patch.object(cp, "compare_status") as compare:
            self.assertEqual(cp.main([str(d), "--docs", "auto"]), 0)
        compare.assert_not_called()

    def test_auto_runs_the_docs_rule_on_a_release_pull_request(self) -> None:
        d = self.repo(uses(B), docs=f"onepeek-docs@{A}\n")
        env = {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_BASE_REF": "release", "GH_TOKEN": "t"}
        with mock.patch.object(cp, "remote_tags", return_value=cp.parse_tags(TAGS)), \
                mock.patch.dict(cp.os.environ, env), mock.patch.object(cp, "docs_problems", return_value=["off main"]):
            self.assertEqual(cp.main([str(d), "--docs", "auto"]), 1)


class Workflow(unittest.TestCase):
    def test_the_app_token_reads_only_onepeek_docs_and_only_when_the_docs_rule_runs(self) -> None:
        steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["pins"]["steps"]
        app = next(s for s in steps if s.get("id") == "app")
        self.assertEqual(app["with"]["repositories"], "onepeek-docs")
        self.assertEqual(app["with"]["permission-contents"], "read")
        self.assertIn("steps.docs.outputs.needed == 'true'", app["if"])
        last = steps[-1]["run"]
        self.assertIn("--docs auto", last)

    def test_the_version_step_finds_the_callers_pin_of_pins_yml(self) -> None:
        step = next(s for s in yaml.safe_load(WORKFLOW.read_text())["jobs"]["pins"]["steps"] if s.get("id") == "version")
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / ".github/workflows").mkdir(parents=True)
        (d / ".github/workflows/c.yml").write_text(uses(A, "secret-scan") + uses(B, "pins"))
        out = d / "out"
        r = subprocess.run(["bash", "-eo", "pipefail", "-c", step["run"]], cwd=d, env={"GITHUB_OUTPUT": str(out), "PATH": "/usr/bin:/bin"},
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(out.read_text().strip(), f"sha={B}")
        (d / ".github/workflows/c.yml").write_text("jobs: {}\n")  # a local call (this repo): no pin, local scripts
        r = subprocess.run(["bash", "-eo", "pipefail", "-c", step["run"]], cwd=d, env={"GITHUB_OUTPUT": str(out), "PATH": "/usr/bin:/bin"},
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(out.read_text().strip().endswith("sha="))


if __name__ == "__main__":
    unittest.main()
