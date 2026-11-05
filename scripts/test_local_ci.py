"""workspace/local_ci.py: it finds the pull-request workflows, fills a reusable workflow's inputs, and skips remote steps."""
import contextlib
import importlib.util
import io
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import yaml

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

    def test_every_step_after_a_role_is_assumed_is_skipped(self):
        d = self.repo("on: [pull_request]\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: 'true'\n"
                      "      - uses: aws-actions/configure-aws-credentials@x\n        with: {role-to-assume: r}\n"
                      "      - run: python3 scripts/calls_aws_inside.py && exit 5\n")
        self.assertEqual(lc.main([str(d), "--jobs", "a"]), 0)

    def test_an_install_needing_only_the_package_token_runs_with_the_gh_login(self):
        wf = ("on: [pull_request]\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
              "      - env:\n          NODE_AUTH_TOKEN: ${{ secrets.GITHUB_TOKEN }}\n        run: npm ci; test \"$NODE_AUTH_TOKEN\" = tok\n"
              "      - run: npx eslint . || exit 6\n")
        d = self.repo(wf)
        stub = d / "bin"
        stub.mkdir()
        for tool in ("npm", "npx"):
            (stub / tool).write_text("#!/bin/sh\nexit 0\n")
            (stub / tool).chmod(0o755)
        path = f"{stub}:{os.environ['PATH']}"
        with mock.patch.object(lc, "packages_token", return_value="tok"), mock.patch.dict(os.environ, {"PATH": path}):
            self.assertEqual(lc.main([str(d), "--jobs", "a"]), 0)
        # Without a token that can read packages, the install and the Node steps after it are skipped as before.
        (d / ".github/workflows/checks.yml").write_text(wf, encoding="utf-8")
        (stub / "npx").write_text("#!/bin/sh\nexit 9\n")
        with mock.patch.object(lc, "packages_token", return_value=""), mock.patch.dict(os.environ, {"PATH": path}):
            self.assertEqual(lc.main([str(d), "--jobs", "a"]), 0)

    def test_a_step_reading_the_needs_context_through_a_script_is_skipped(self):
        d = self.repo("on: [pull_request]\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
                      "      - env:\n          NEEDS: ${{ toJSON(needs) }}\n        run: python3 -c 'import os; os.environ[\"NEEDS\"]'\n")
        self.assertEqual(lc.main([str(d), "--jobs", "a"]), 0)

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


class Versions(unittest.TestCase):
    """Playbook §8a P3: uv and tflint at CI's versions; P6: one shared provider cache."""

    def repo(self, workflow: str) -> pathlib.Path:
        return LocalCi.repo(self, workflow)

    def test_the_shared_workflows_pin_the_versions_local_ci_checks(self):
        wf = ROOT / ".github/workflows"
        uv_files = ["secret-scan.yml", "python-checks.yml", "terraform-checks.yml", "contract.yml"]
        for name in uv_files:
            text = (wf / name).read_text(encoding="utf-8")
            self.assertIn(f'version: "{lc.UV_VERSION}"', text, name)
        self.assertIn(f"tflint_version: v{lc.TFLINT_VERSION}", (wf / "terraform-checks.yml").read_text(encoding="utf-8"))
        self.assertIn(f"uv=={lc.UV_VERSION}", (wf / "release-ecs.yml").read_text(encoding="utf-8"))
        for f in wf.glob("*.yml"):  # no setup-uv or setup-tflint without its exact version
            doc = yaml.safe_load(f.read_text(encoding="utf-8"))
            for job in (doc.get("jobs") or {}).values():
                for st in job.get("steps") or []:
                    uses = str(st.get("uses") or "")
                    if uses.startswith("astral-sh/setup-uv"):
                        self.assertEqual((st.get("with") or {}).get("version"), lc.UV_VERSION, f.name)
                    if uses.startswith("terraform-linters/setup-tflint"):
                        self.assertEqual((st.get("with") or {}).get("tflint_version"), f"v{lc.TFLINT_VERSION}", f.name)

    def test_a_different_uv_fails_the_run_with_the_fix(self):
        d = self.repo("on: [pull_request]\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: 'true'\n")
        with mock.patch.object(lc, "tool_version", side_effect=lambda t: "0.9.7" if t == "uv" else None):
            self.assertIn(lc.UV_VERSION, lc.uv_problem())
            self.assertEqual(lc.main([str(d), "--jobs", "a"]), 1)
        with mock.patch.object(lc, "tool_version", side_effect=lambda t: lc.UV_VERSION if t == "uv" else None):
            self.assertEqual(lc.uv_problem(), "")
            self.assertEqual(lc.main([str(d), "--jobs", "a"]), 0)

    def test_a_different_tflint_skips_its_steps_with_the_reason(self):
        d = self.repo("on: [pull_request]\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: tflint --recursive\n")
        fake = pathlib.Path(tempfile.mkdtemp())
        (fake / "tflint").write_text("#!/bin/sh\necho 'TFLint version 0.50.0'\nexit 1\n")
        (fake / "tflint").chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": f"{fake}:{os.environ['PATH']}"}), \
                mock.patch.object(lc, "uv_problem", return_value=""), mock.patch.object(lc, "docker_ready", return_value=False):
            self.assertIn("CI pins 0.65.0", lc.tflint_problem())
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(lc.main([str(d), "--jobs", "a"]), 0)  # skipped, not run (the fake would fail)
        self.assertIn("tflint 0.50.0 is installed; CI pins 0.65.0", buf.getvalue())

    def _tf_repo(self) -> pathlib.Path:
        return self.repo("on: [pull_request]\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
                         "      - run: |\n          tflint --init\n          tflint --recursive\n")

    def _bin(self, **scripts: str) -> pathlib.Path:
        """A PATH folder of stub tools (name -> shell body) plus the system tools the steps need; no real tflint or docker."""
        b = pathlib.Path(tempfile.mkdtemp())
        for name, body in scripts.items():
            (b / name).write_text("#!/bin/sh\n" + body)
            (b / name).chmod(0o755)
        for t in ("bash", "sh", "env", "git", "python3", "cat", "echo"):
            if shutil.which(t) and not (b / t).exists():
                (b / t).symlink_to(shutil.which(t))
        return b

    def test_without_tflint_it_runs_ci_image_in_docker_with_the_same_commands(self):
        d = self._tf_repo()
        log = pathlib.Path(tempfile.mkdtemp()) / "docker.log"
        b = self._bin(docker=f'[ "$1" = info ] && exit 0\necho "$@" >> {log}\n')
        with mock.patch.dict(os.environ, {"PATH": str(b)}), mock.patch.object(lc, "uv_problem", return_value=""):
            self.assertEqual(lc.main([str(d), "--jobs", "a"]), 0)
        lines = log.read_text().splitlines()
        image = f"ghcr.io/terraform-linters/tflint:v{lc.TFLINT_VERSION}"
        self.assertEqual(len(lines), 2)
        for line, want in zip(lines, ("--init", "--recursive")):
            self.assertTrue(line.startswith(f"run --rm -v {d.resolve()}:/data -w /data {image} {want}"), line)

    def test_without_tflint_or_docker_it_warns_and_under_release_fails(self):
        d = self._tf_repo()
        with mock.patch.dict(os.environ, {"PATH": str(self._bin())}), mock.patch.object(lc, "uv_problem", return_value=""):
            err, out = io.StringIO(), io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
                self.assertEqual(lc.main([str(d), "--jobs", "a"]), 0)
            self.assertIn("WARN", err.getvalue())
            self.assertIn("Docker is not available", out.getvalue())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(lc.main([str(d), "--jobs", "a", "--release"]), 1)
            self.assertIn("FAIL", out.getvalue())

    def test_a_step_that_runs_npm_pkg_set_leaves_package_json_unchanged(self):
        d = self.repo("on: [pull_request]\njobs:\n  a:\n    runs-on: ubuntu-latest\n    defaults:\n      run:\n"
                      "        working-directory: clients/typescript\n    steps:\n      - name: Version as next snapshot\n"
                      "        run: |\n          npm pkg set version=9.9.9-next.1\n          grep -q 9.9.9 package.json\n")
        pkg = d / "clients/typescript/package.json"
        pkg.parent.mkdir(parents=True)
        pkg.write_text('{"name": "x", "version": "1.0.0"}\n')
        before = pkg.read_bytes()
        if not shutil.which("npm"):
            self.skipTest("npm is not installed")
        with mock.patch.object(lc, "uv_problem", return_value=""):
            self.assertEqual(lc.main([str(d), "--jobs", "a"]), 0)
        self.assertEqual(pkg.read_bytes(), before)

    def test_package_json_is_restored_even_when_the_step_fails(self):
        d = self.repo("on: [pull_request]\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
                      "      - run: |\n          printf '{}' > package.json\n          npm pkg set version=2.0.0 || true\n          false\n")
        pkg = d / "package.json"
        pkg.write_text('{"version": "1.0.0"}\n')
        before = pkg.read_bytes()
        with mock.patch.object(lc, "uv_problem", return_value=""), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(lc.main([str(d), "--jobs", "a"]), 1)
        self.assertEqual(pkg.read_bytes(), before)

    def test_conflict_markers_fail_before_any_step_and_clean_or_underlines_pass(self):
        d = self.repo("on: [pull_request]\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: 'true'\n")
        doc = d / "AGENTS.md"
        doc.write_text("Title\n" + "=" * 7 + "\ntext\n")  # a Markdown heading underline is not a marker
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
        with mock.patch.object(lc, "uv_problem", return_value=""):
            self.assertEqual(lc.conflict_markers(d), [])
            self.assertEqual(lc.main([str(d), "--jobs", "a"]), 0)
            doc.write_text("a\n" + "<" * 7 + " HEAD\nx\n" + "=" * 7 + "\ny\n" + ">" * 7 + " dev\n")
            subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
            self.assertEqual(lc.conflict_markers(d), ["AGENTS.md:2"])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(lc.main([str(d), "--jobs", "a"]), 1)
            self.assertIn("AGENTS.md:2", out.getvalue())
            with mock.patch.object(lc, "CONFLICT_MARKER_ALLOW", ("AGENTS.md",)):
                self.assertEqual(lc.conflict_markers(d), [])

    def test_steps_use_one_shared_provider_cache_unless_the_laptop_sets_one(self):
        home = pathlib.Path(tempfile.mkdtemp())
        d = self.repo("on: [pull_request]\nenv:\n  TF_PLUGIN_CACHE_DIR: ${{ github.workspace }}/.terraform-plugin-cache\njobs:\n  a:\n"
                      "    runs-on: ubuntu-latest\n    steps:\n      - run: test \"$TF_PLUGIN_CACHE_DIR\" = \"$WANT\" && test -d \"$WANT\"\n"
                      "        env:\n          WANT: " + str(home / ".cache/onepeek/terraform-plugins") + "\n")
        env = {k: v for k, v in os.environ.items() if k != "TF_PLUGIN_CACHE_DIR"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(lc, "SHARED_PLUGIN_CACHE", home / ".cache/onepeek/terraform-plugins"), \
                mock.patch.object(lc, "uv_problem", return_value=""):
            self.assertEqual(lc.main([str(d), "--jobs", "a"]), 0)
        own = home / "own"
        with mock.patch.dict(os.environ, {"TF_PLUGIN_CACHE_DIR": str(own)}), mock.patch.object(lc, "uv_problem", return_value=""):
            self.assertEqual(lc.plugin_cache(), own)
            self.assertTrue(own.is_dir())


class GithubRepoJobs(unittest.TestCase):
    def test_the_service_list_job_runs_locally(self):  # §8a P5
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), mock.patch.object(lc, "uv_problem", return_value=""):
            self.assertEqual(lc.main([str(ROOT), "--list", "--jobs", "service-list"]), 0)
        self.assertIn("workflow-lint.yml:service-list: Tests, then the drift check\n", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
