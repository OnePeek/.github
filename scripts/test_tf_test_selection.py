import subprocess
import tempfile
import unittest
from pathlib import Path

import tf_test_selection as t

SUITES = ["bootstrap", "modules/aws/cache", "modules/aws/edge", "stacks/aws"]


class Select(unittest.TestCase):
    def pr(self, *changed):
        return t.select(SUITES, list(changed), "pull_request", "dev")

    def test_suite_dir_is_the_folder_above_tests(self):
        self.assertEqual(t.suite_dir("modules/aws/cache/tests/a.tftest.hcl"), "modules/aws/cache")
        self.assertEqual(t.suite_dir("stacks/aws/tests/deep/x/a.tftest.hcl"), "stacks/aws")
        self.assertEqual(t.suite_dir("modules/x/a.tftest.hcl"), "modules/x")
        self.assertEqual(t.suite_dir("tests/a.tftest.hcl"), ".")

    def test_only_the_changed_module_and_the_stack_run_for_a_module_change(self):
        self.assertEqual(self.pr("modules/aws/cache/main.tf"), ["modules/aws/cache", "stacks/aws"])

    def test_a_test_file_change_runs_its_suite(self):
        self.assertEqual(self.pr("modules/aws/edge/tests/x.tftest.hcl"), ["modules/aws/edge", "stacks/aws"])

    def test_bootstrap_alone_does_not_run_the_stack(self):
        self.assertEqual(self.pr("bootstrap/main.tf"), ["bootstrap"])
        self.assertEqual(self.pr("bootstrap/tests/a.tftest.hcl"), ["bootstrap"])

    def test_stack_config_policy_scripts_run_the_stack_suite(self):
        for f in ("stacks/aws/main.tf", "config/preprod.yaml", "policy/switches.yaml", "scripts/up.py", "modules/aws/other/main.tf"):
            self.assertEqual(self.pr(f), ["stacks/aws"], f)

    def test_unrelated_changes_run_nothing(self):
        self.assertEqual(self.pr("README.md", "docs/a.md", "envs/preprod/main.tf", ".github/workflows/x.yml"), [])
        self.assertEqual(self.pr(), [])

    def test_a_directory_prefix_is_not_a_match(self):
        self.assertEqual(self.pr("bootstrap-notes/a.md", "modules/aws/cache2/main.tf"), ["stacks/aws"])  # cache2: any modules/aws change, not the cache suite

    def test_release_pushes_and_schedules_run_everything(self):
        self.assertEqual(t.select(SUITES, ["README.md"], "pull_request", "release"), SUITES)
        self.assertEqual(t.select(SUITES, [], "push", ""), SUITES)
        self.assertEqual(t.select(SUITES, [], "schedule", ""), SUITES)

    def test_no_stack_suite_means_no_stack_run(self):
        self.assertEqual(t.select(["bootstrap"], ["scripts/up.py"], "pull_request", "dev"), [])


class Cli(unittest.TestCase):
    def test_git_diff_against_the_base(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)

            def git(*a):
                subprocess.run(["git", "-C", d, "-c", "user.email=a@b", "-c", "user.name=a", *a], check=True, capture_output=True)

            git("init", "-q", "-b", "dev")
            for f in ("bootstrap/tests/a.tftest.hcl", "stacks/aws/tests/a.tftest.hcl", "modules/aws/cache/tests/a.tftest.hcl"):
                (root / f).parent.mkdir(parents=True, exist_ok=True)
                (root / f).write_text("")
            git("add", "-A")
            git("commit", "-qm", "base")
            git("branch", "base")
            (root / "bootstrap/main.tf").write_text("x")
            git("add", "-A")
            git("commit", "-qm", "change")
            self.assertEqual(t.find_suites(root), ["bootstrap", "modules/aws/cache", "stacks/aws"])
            self.assertEqual(t.main(["--event", "pull_request", "--base-ref", "dev", "--diff-against", "base", "--root", d]), 0)

    def test_no_suites_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(t.main(["--event", "push", "--root", d]), 1)


if __name__ == "__main__":
    unittest.main()
