"""workspace/local_ci.py: it finds the pull-request workflows, fills a reusable workflow's inputs, and skips remote steps."""
import importlib.util
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("local_ci", ROOT / "workspace/local_ci.py")
lc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lc)


class LocalCi(unittest.TestCase):
    def repo(self, workflow: str) -> pathlib.Path:
        d = pathlib.Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", str(d)], check=True)  # actionlint needs a repository
        (d / ".github/workflows").mkdir(parents=True)
        (d / ".github/workflows/checks.yml").write_text(workflow, encoding="utf-8")
        return d

    def test_only_pull_request_workflows_are_found(self):
        d = self.repo("on: [push]\njobs: {}\n")
        self.assertEqual(list(lc.pr_workflows(d)), [])
        (d / ".github/workflows/pr.yml").write_text("on:\n  pull_request:\njobs: {}\n", encoding="utf-8")
        self.assertEqual([p.name for p, _ in lc.pr_workflows(d)], ["pr.yml"])

    def test_inputs_are_substituted_and_expressions_dropped_from_env(self):
        self.assertEqual(lc.substitute("roots: ${{ inputs.roots }}", {"roots": "a b"}), "roots: a b")
        self.assertEqual(lc.substitute("${{ inputs.missing }}", {}), "${{ inputs.missing }}")
        self.assertEqual(lc.clean_env({"A": "1", "B": "${{ github.sha }}"}), {"A": "1"})

    def test_a_passing_step_passes_and_a_remote_step_is_skipped(self):
        d = self.repo("on: [pull_request]\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: 'true'\n      - run: gh api repos/x\n")
        self.assertEqual(lc.main([str(d), "--jobs", "a"]), 0)

    def test_the_workflow_working_directory_applies_to_its_jobs(self):
        d = self.repo("on: [pull_request]\ndefaults:\n  run:\n    working-directory: sub\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: test -f marker\n")
        (d / "sub").mkdir()
        (d / "sub/marker").write_text("x", encoding="utf-8")
        self.assertEqual(lc.main([str(d), "--jobs", "a"]), 0)

    def test_a_failing_step_fails(self):
        d = self.repo("on: [pull_request]\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: exit 3\n")
        self.assertEqual(lc.main([str(d), "--jobs", "a"]), 1)


if __name__ == "__main__":
    unittest.main()
