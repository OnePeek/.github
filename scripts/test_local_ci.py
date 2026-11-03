"""workspace/local_ci.py: it finds the pull-request workflows, fills a reusable workflow's inputs, and skips remote steps."""
import importlib.util
import os
import pathlib
import shutil
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

    def test_a_clone_from_github_is_skipped_as_remote(self):
        d = self.repo("on: [pull_request]\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: git clone --depth 1 https://github.com/OnePeek/.github.git ../x\n")
        self.assertEqual(lc.main([str(d), "--jobs", "a"]), 0)

    def test_terraform_init_against_a_backend_is_skipped_but_backend_false_runs(self):
        for cmd, skipped in (("terraform init -input=false -backend-config=x.tfbackend", True), ("terraform -chdir=r init -backend=false", False)):
            self.assertEqual(bool(lc.REMOTE.search(cmd)), skipped, cmd)

    def test_no_cloud_credentials_reach_a_step(self):
        d = self.repo("on: [pull_request]\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: test -z \"${AWS_ACCESS_KEY_ID}${AWS_PROFILE}\"\n")
        old = {k: os.environ.get(k) for k in ("AWS_ACCESS_KEY_ID", "AWS_PROFILE")}
        os.environ.update({"AWS_ACCESS_KEY_ID": "x", "AWS_PROFILE": "y"})
        try:
            self.assertEqual(lc.main([str(d), "--jobs", "a"]), 0)
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def test_github_env_written_by_a_step_reaches_the_next_step(self):
        d = self.repo("on: [pull_request]\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo FOO=bar >> \"$GITHUB_ENV\"\n      - run: test \"$FOO\" = bar\n")
        self.assertEqual(lc.main([str(d), "--jobs", "a"]), 0)

    def test_runner_temp_is_set_and_a_step_moving_a_skipped_checkout_is_skipped(self):
        d = self.repo("on: [pull_request]\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
                      "      - uses: actions/checkout@abc\n        with:\n          path: docs-checkout\n"
                      "      - run: mv docs-checkout ../nowhere/docs\n"
                      "      - run: test -d \"$RUNNER_TEMP\" && test \"$GITHUB_WORKSPACE\" = \"$PWD\"\n")
        self.assertEqual(lc.main([str(d), "--jobs", "a"]), 0)

    def test_skip_docker_also_skips_a_repo_script_that_calls_docker(self):
        d = self.repo("on: [pull_request]\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: ./boot.sh\n")
        (d / "boot.sh").write_text("#!/bin/sh\ndocker run x\nexit 9\n", encoding="utf-8")
        (d / "boot.sh").chmod(0o755)
        self.assertEqual(lc.main([str(d), "--jobs", "a"]), 1)
        self.assertEqual(lc.main([str(d), "--jobs", "a", "--skip-docker"]), 0)

    def test_a_step_using_an_env_value_only_ci_has_is_skipped(self):
        d = self.repo("on: [pull_request]\njobs:\n  a:\n    runs-on: ubuntu-latest\n    strategy:\n      matrix:\n        env: [x]\n    steps:\n"
                      "      - env:\n          E: ${{ matrix.env }}\n          F: ${{ github.event.pull_request.number }}\n        run: test -n \"${E}\"\n"
                      "      - env:\n          F: ${{ github.event.pull_request.number }}\n        run: 'true'\n")
        self.assertEqual(lc.main([str(d), "--jobs", "a"]), 0)

    def test_a_step_needing_a_service_a_skipped_docker_step_starts_is_skipped(self):
        d = self.repo("on: [pull_request]\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
                      "      - run: docker run -d -p 127.0.0.1:1:1 x\n      - run: curl http://127.0.0.1:1 && exit 4\n")
        self.assertEqual(lc.main([str(d), "--jobs", "a", "--skip-docker"]), 0)

    def test_a_failing_step_fails(self):
        d = self.repo("on: [pull_request]\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: exit 3\n")
        self.assertEqual(lc.main([str(d), "--jobs", "a"]), 1)

    def test_runner_contexts_are_filled_in_and_unknown_expressions_dropped(self):
        d = self.repo("on: [pull_request]\njobs: {}\n")
        ctx = lc.contexts(d, "onepeek-infra")
        env = lc.clean_env({"A": "${{ github.workspace }}/cache", "B": "${{ github.sha }}", "C": "${{ github.base_ref }}",
                            "D": "${{ runner.temp }}/x", "E": "${{ github.repository }}"}, ctx)
        self.assertEqual(env["A"], f"{d}/cache")
        self.assertNotIn("B", env)
        self.assertNotIn("N", lc.clean_env({"N": "${{ github.event.pull_request.number }}"}, ctx))
        self.assertEqual((env["C"], env["E"]), ("dev", "OnePeek/onepeek-infra"))
        self.assertTrue(pathlib.Path(env["D"]).parent.is_dir())
        self.assertTrue(str(env["D"]).startswith(str(lc.run_root())))  # removed at exit with the rest of the run's directory

    def test_a_step_sees_an_env_value_built_from_a_context(self):
        d = self.repo("on: [pull_request]\nenv:\n  CACHE: ${{ github.workspace }}/.c\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
                      "    steps:\n      - run: |\n          mkdir -p \"${CACHE}\"\n          test \"${CACHE}\" = \"${PWD}/.c\"\n")
        self.assertEqual(lc.main([str(d), "--jobs", "a"]), 0)
        self.assertTrue((d / ".c").is_dir())

    def make_origin_repo(self, name: str, root: pathlib.Path) -> pathlib.Path:
        """A sibling repo whose origin/dev holds a marker file, with a working tree that must stay untouched."""
        bare = root / f"{name}.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        src = root / name
        subprocess.run(["git", "clone", "-q", str(bare), str(src)], check=True, capture_output=True)
        for c in (["checkout", "-q", "-b", "dev"],):
            subprocess.run(["git", "-C", str(src), *c], check=True)
        (src / "marker").write_text("dev", encoding="utf-8")
        subprocess.run(["git", "-C", str(src), "add", "marker"], check=True)
        subprocess.run(["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x"], check=True)
        subprocess.run(["git", "-C", str(src), "push", "-q", "origin", "dev"], check=True, capture_output=True)
        return src

    def test_sibling_checkouts_are_provided_at_origin_dev_and_removed_even_on_failure(self):
        root = pathlib.Path(tempfile.mkdtemp())
        siblings = {n: self.make_origin_repo(n, root) for n in ("backend-service", "onepeek-web")}
        infra = self.make_origin_repo("onepeek-infra", root)
        (siblings["onepeek-web"] / "marker").write_text("edited, uncommitted", encoding="utf-8")
        for attempt in ("ok", "fail"):
            try:
                with lc.sibling_checkouts(infra, "onepeek-infra", root) as (ok, reason):
                    if not shutil.which("uv"):
                        self.assertFalse(ok)
                        self.assertIn("uv", reason)
                        continue
                    self.assertTrue(ok, reason)
                    self.assertEqual((infra / "_backend/marker").read_text(encoding="utf-8"), "dev")
                    self.assertEqual((infra / "_web/marker").read_text(encoding="utf-8"), "dev")  # origin/dev, not the edited file
                    self.assertTrue((infra / "_infra").is_symlink())
                    if attempt == "fail":
                        raise RuntimeError("step failed")
            except RuntimeError:
                pass
            for folder in ("_backend", "_web", "_infra"):
                self.assertFalse((infra / folder).exists() or (infra / folder).is_symlink(), folder)
            self.assertEqual((siblings["onepeek-web"] / "marker").read_text(encoding="utf-8"), "edited, uncommitted")
            self.assertNotIn("_web", subprocess.run(["git", "-C", str(siblings["onepeek-web"]), "worktree", "list"],
                                                    capture_output=True, text=True).stdout)

    def test_a_missing_sibling_skips_the_job_with_a_reason(self):
        root = pathlib.Path(tempfile.mkdtemp())
        infra = self.make_origin_repo("onepeek-infra", root)
        with lc.sibling_checkouts(infra, "onepeek-infra", root) as (ok, reason):
            self.assertFalse(ok)
            self.assertTrue(reason)
        self.assertFalse((infra / "_infra").is_symlink())

    def test_a_job_that_needs_the_siblings_is_skipped_not_failed_when_they_are_missing(self):
        d = self.repo("on: [pull_request]\njobs:\n  c:\n    runs-on: ubuntu-latest\n    steps:\n      - run: test -d _backend\n")
        old = lc.WORKSPACE
        lc.WORKSPACE = pathlib.Path(tempfile.mkdtemp())
        try:
            self.assertEqual(lc.main([str(d), "--jobs", "c"]), 0)
        finally:
            lc.WORKSPACE = old


if __name__ == "__main__":
    unittest.main()
