"""workspace/local_ci.py: it finds the pull-request workflows, fills a reusable workflow's inputs, and skips remote steps."""
import contextlib
import importlib.util
import io
import re
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("local_ci", ROOT / "workspace/local_ci.py")
lc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lc)



def plugin_download_unreachable(text: str) -> bool:
    """The tflint plugin download (`tflint --init` fetching a GitHub release) could not reach GitHub's API: rate limited, a
    network timeout, DNS or connection failure. Environment trouble, not a defect in the image or the config, so the
    test skips (the network is not under test); any other failure is still a failure."""
    if "rate limit" in text:
        return True
    return bool(re.search(r"Failed to fetch GitHub releases.*?(timeout|timed out|deadline exceeded|no such host|connection (reset|refused)|\bEOF\b)", text, re.S | re.I))

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

    def test_without_tflint_each_command_runs_in_its_own_container_with_the_images_entrypoint(self):
        d = self.repo("on: [pull_request]\njobs:\n  a:\n    runs-on: ubuntu-latest\n    defaults:\n      run:\n        working-directory: sub\n"
                      "    steps:\n      - env:\n          TFLINT_LOG: debug\n        run: |\n          tflint --init\n          cd . && tflint --recursive\n")
        (d / "sub").mkdir()
        cache = pathlib.Path(tempfile.mkdtemp()) / "tflint.d"
        log = pathlib.Path(tempfile.mkdtemp()) / "docker.log"
        b = self._bin(docker=f'[ "$1" = info ] && exit 0\necho "$@" >> {log}\n')
        with mock.patch.dict(os.environ, {"PATH": str(b)}), mock.patch.object(lc, "uv_problem", return_value=""), \
                mock.patch.object(lc, "TFLINT_CACHE", cache):
            self.assertEqual(lc.main([str(d), "--jobs", "a"]), 0)
        lines = log.read_text().splitlines()
        image = f"ghcr.io/terraform-linters/tflint:v{lc.TFLINT_VERSION}"
        self.assertEqual(len(lines), 2)
        for line, want in zip(lines, ("--init", "--recursive")):
            self.assertTrue(line.startswith(f"run --rm -v {d.resolve()}:/data -w /data/sub -v {cache}:/root/.tflint.d -e TFLINT_LOG {image} {want}"), line)
            self.assertNotIn("debug", line)  # passed by name, never by value
            self.assertNotIn(" sh ", line)  # the image's entrypoint is tflint; there is no shell to start
        self.assertTrue(cache.is_dir())

    def test_a_tflint_step_with_other_commands_needs_native_tflint(self):
        d = self.repo("on: [pull_request]\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: |\n          echo hi\n          tflint --init\n")
        with mock.patch.dict(os.environ, {"PATH": str(self._bin(docker="exit 0\n"))}), mock.patch.object(lc, "uv_problem", return_value=""):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(lc.main([str(d), "--jobs", "a"]), 1)
        self.assertIn("needs tflint", out.getvalue())

    def test_a_failed_plugin_download_is_a_skip_only_when_github_was_unreachable(self):
        base = 'Failed to install a plugin; Failed to fetch GitHub releases: Get "https://api.github.com/x": '
        for why in ("net/http: TLS handshake timeout", "dial tcp: lookup api.github.com: no such host", "context deadline exceeded (Client.Timeout exceeded)",
                    "read: connection reset by peer", "unexpected EOF"):
            self.assertTrue(plugin_download_unreachable(base + why), why)
        self.assertTrue(plugin_download_unreachable("API rate limit exceeded for 1.2.3.4"))
        # A real problem with the plugin or the config stays a failure.
        for text in ("", "Failed to install a plugin; checksum mismatch", base + "404 Not Found", "rule violation: terraform_unused_declarations"):
            self.assertFalse(plugin_download_unreachable(text), text)

    def test_the_pinned_image_really_runs_on_a_plugin_fixture_and_a_clean_one(self):
        if not lc.docker_ready():
            self.skipTest("docker is not available")
        aws = ('plugin "aws" {\n  enabled = true\n  version = "0.40.0"\n  source = "github.com/terraform-linters/tflint-ruleset-aws"\n}\n')
        bundled = 'plugin "terraform" {\n  enabled = true\n  preset  = "all"\n}\nrule "terraform_standard_module_structure" {\n  enabled = false\n}\n'  # ships inside tflint: no download
        # The aws plugin needs --init's download to reach --recursive; GitHub's API may rate-limit that download (then it is skipped).
        for hcl in (bundled, "", aws):
            d = self._tf_repo()
            (d / "main.tf").write_text('terraform {\n  required_version = ">= 1.0"\n}\n\nvariable "x" {\n  description = "x"\n  type        = string\n}\n\noutput "x" {\n  description = "x"\n  value       = var.x\n}\n')
            (d / ".tflint.hcl").write_text(hcl)
            cache = pathlib.Path(tempfile.mkdtemp()) / "tflint.d"
            out = io.StringIO()
            with mock.patch.object(lc, "tflint_mode", return_value="docker"), mock.patch.object(lc, "TFLINT_CACHE", cache), \
                    mock.patch.object(lc, "uv_problem", return_value=""), contextlib.redirect_stdout(out):
                rc = lc.main([str(d), "--jobs", "a"])
            if hcl is aws and plugin_download_unreachable(out.getvalue()):
                self.skipTest("GitHub's API could not be reached for the plugin download (rate limit or network)")
            self.assertEqual(rc, 0, out.getvalue())
            self.assertIn("PASS", out.getvalue())
            self.assertEqual(hcl is aws, any(cache.rglob("tflint-ruleset-aws*")), out.getvalue())

    def test_tflint_calls_are_found_anywhere_in_a_step_but_not_other_tools(self):
        for yes in ("tflint --init", "  tflint", "cd x && tflint -f json", "if tflint; then :; fi", "a; tflint", "(tflint)", "a | tflint"):
            self.assertTrue(lc.TFLINT_CALL.search(yes), yes)
        for no in ("tflint-foo --x", "echo tflint", "mytflint", "tflint.sh"):
            self.assertFalse(lc.TFLINT_CALL.search(no), no)

    def test_a_working_directory_outside_the_repo_fails_the_tflint_step(self):
        d = self.repo("on: [pull_request]\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - working-directory: ../elsewhere\n        run: tflint\n")
        with mock.patch.dict(os.environ, {"PATH": str(self._bin(docker="exit 0\n"))}), mock.patch.object(lc, "uv_problem", return_value=""):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(lc.main([str(d), "--jobs", "a"]), 1)
        self.assertIn("outside the repository", out.getvalue())

    def test_native_tflint_at_ci_version_runs_without_docker(self):
        d = self._tf_repo()
        log = pathlib.Path(tempfile.mkdtemp()) / "t.log"
        b = self._bin(tflint=f'[ "$1" = --version ] && echo "TFLint version {lc.TFLINT_VERSION}" && exit 0\necho "$@" >> {log}\n',
                      docker="exit 9\n")
        with mock.patch.dict(os.environ, {"PATH": str(b)}), mock.patch.object(lc, "uv_problem", return_value=""):
            self.assertEqual(lc.tflint_mode(), "native")
            self.assertEqual(lc.main([str(d), "--jobs", "a"]), 0)
        self.assertEqual(log.read_text().splitlines(), ["--init", "--recursive"])

    def test_skip_docker_leaves_tflint_to_ci_but_release_still_fails(self):
        d = self._tf_repo()
        b = self._bin(docker=f"exit 0\n")  # a working docker that --skip-docker must not use
        with mock.patch.dict(os.environ, {"PATH": str(b)}), mock.patch.object(lc, "uv_problem", return_value=""):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(lc.main([str(d), "--jobs", "a", "--skip-docker"]), 0)
            self.assertIn("WARN", err.getvalue())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(lc.main([str(d), "--jobs", "a", "--skip-docker", "--release"]), 1)
            self.assertIn("FAIL", out.getvalue())

    def test_release_fails_on_a_missing_trivy_too(self):
        d = self.repo("on: [pull_request]\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: trivy config .\n")
        with mock.patch.dict(os.environ, {"PATH": str(self._bin())}), mock.patch.object(lc, "uv_problem", return_value=""):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(lc.main([str(d), "--jobs", "a"]), 0)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(lc.main([str(d), "--jobs", "a", "--release"]), 1)
            self.assertIn("trivy", out.getvalue())

    def test_conflict_marker_forms_and_exclusions(self):
        d = self.repo("on: [pull_request]\njobs: {}\n")
        L, R = "<" * 7, ">" * 7
        cases = {"open.md": f"a\n{L} HEAD\n", "close.md": f"a\n{R} dev\n", "bare_open.md": f"a\n{L}\n", "bare_close.md": f"a\r\n{R}\r\n"}
        for n, body in cases.items():
            (d / n).write_text(body)
        (d / "midline.md").write_text(f"see {L} here\n")
        (d / "eight.md").write_text("<" * 8 + "x\n")
        (d / "bin.dat").write_bytes(b"\0" + f"\n{L} HEAD\n".encode())
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
        got = lc.conflict_markers(d)
        self.assertEqual(sorted(g.split(":")[0] for g in got), ["bare_close.md", "bare_open.md", "close.md", "open.md"])

    def test_an_unmerged_file_is_reported_once(self):
        d = self.repo("on: [pull_request]\njobs: {}\n")
        home = tempfile.mkdtemp()  # no git settings of the machine (identity, default branch, conflict style) reach the fixture
        env = {**os.environ, "HOME": home, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
        flags = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main", "-c", "merge.conflictStyle=merge",
                 "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]
        g = lambda *a, check=True: subprocess.run(["git", "-C", str(d), *flags, *a], check=check, capture_output=True, text=True, env=env)
        (d / "f.txt").write_text("base\n"); g("add", "-A"); g("commit", "-qm", "base"); g("branch", "-M", "main")
        g("checkout", "-qb", "other"); (d / "f.txt").write_text("other\n"); g("commit", "-qam", "o")
        g("checkout", "-q", "main"); (d / "f.txt").write_text("main\n"); g("commit", "-qam", "m")
        self.assertNotEqual(g("merge", "other", check=False).returncode, 0)
        self.assertEqual(len(g("ls-files", "-u").stdout.splitlines()), 3)  # really mid-merge: three index stages
        self.assertIn("<" * 7, (d / "f.txt").read_text())
        self.assertEqual(lc.conflict_markers(d), ["f.txt:1"])

    def test_conflict_scan_falls_back_when_git_lacks_deduplicate(self):
        d = self.repo("on: [pull_request]\njobs: {}\n")
        (d / "f.txt").write_text("<" * 7 + " HEAD\n")
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
        real = subprocess.run

        def fake(argv, *a, **k):
            if "--deduplicate" in argv:
                return subprocess.CompletedProcess(argv, 129, b"", b"unknown option")
            return real(argv, *a, **k)
        with mock.patch.object(lc.subprocess, "run", side_effect=fake):
            self.assertEqual(lc.conflict_markers(d), ["f.txt:1"])

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


# --- Foundation playbook §8c: L1 to L4 ---------------------------------------------------------------------------------
def make_repo_with(workflow: str) -> pathlib.Path:
    return LocalCi.repo(None, workflow)


def job_yaml(*steps: str) -> str:
    body = "".join(f"      - run: |\n{''.join('          ' + ln + chr(10) for ln in st.splitlines())}" for st in steps)
    return f"on: [pull_request]\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n{body}"


class CommentsL1(unittest.TestCase):
    """L1: skip patterns are matched with shell comments removed; a skipped `terraform test` is never silent."""

    def bin_with_terraform(self, marker: pathlib.Path) -> str:
        b = pathlib.Path(tempfile.mkdtemp())
        (b / "terraform").write_text(f"#!/bin/sh\necho \"$@\" >> {marker}\n", encoding="utf-8")
        (b / "terraform").chmod(0o755)
        return f"{b}:{os.environ['PATH']}"

    def run_main(self, steps, *args, path=None):
        d = make_repo_with(job_yaml(*steps))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"PATH": path or os.environ["PATH"]}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = lc.main([str(d), "--jobs", "a", *args])
        return rc, out.getvalue(), err.getvalue(), d

    def test_strip_comments_cases(self):
        self.assertEqual(lc.strip_comments("# aws s3 ls\nls"), "\nls")
        self.assertEqual(lc.strip_comments("ls # aws s3 ls"), "ls ")
        self.assertEqual(lc.strip_comments('echo "#aws "'), 'echo "#aws "')
        self.assertEqual(lc.strip_comments("echo '#aws ' x"), "echo '#aws ' x")
        self.assertEqual(lc.strip_comments('echo "unclosed # aws s3'), 'echo "unclosed # aws s3')

    def test_a_hash_inside_a_word_is_not_a_comment(self):
        # shlex comment mode would hide the aws command here; the shell does not treat url#fragment as a comment.
        cmd = "echo url#fragment; aws s3 rm x"
        self.assertEqual(lc.strip_comments(cmd), cmd)
        self.assertTrue(lc.REMOTE.search(lc.strip_comments(cmd)))
        rc, out, _, d = self.run_main([cmd + "; touch ran"])
        self.assertEqual(rc, 0)
        self.assertFalse((d / "ran").exists())
        self.assertIn("needs GitHub or AWS", out)

    def test_a_comment_mentioning_aws_does_not_skip_the_terraform_test_step(self):
        marker = pathlib.Path(tempfile.mkdtemp()) / "calls"
        step = "# no aws credentials are needed: init uses -backend=false\nterraform -chdir=. init -backend=false -input=false\nterraform -chdir=. test"
        rc, out, _, _ = self.run_main([step], path=self.bin_with_terraform(marker))
        self.assertEqual(rc, 0, out)
        self.assertEqual(marker.read_text(encoding="utf-8").splitlines(), ["-chdir=. init -backend=false -input=false", "-chdir=. test"])

    def test_code_that_calls_aws_is_still_skipped(self):
        rc, out, _, d = self.run_main(["touch ran\naws s3 ls"])
        self.assertEqual(rc, 0)
        self.assertFalse((d / "ran").exists())

    def test_a_hash_inside_quotes_is_code(self):
        rc, _, _, d = self.run_main(['echo "#aws "; touch ran'])
        self.assertEqual(rc, 0)
        self.assertTrue((d / "ran").exists())

    def test_release_fails_on_a_skipped_terraform_test_and_otherwise_warns_and_lists_it_first(self):
        step = "terraform -chdir=. test\naws s3 ls"
        rc, out, err, _ = self.run_main([step, "gh api repos/x"], "--release")
        self.assertEqual(rc, 1)
        self.assertIn("terraform test skipped", out)
        rc, out, err, _ = self.run_main(["gh api repos/x", step])
        self.assertEqual(rc, 0)
        self.assertIn("WARN", err + out)
        self.assertIn("terraform test skipped", err + out)
        listed = out.split("Skipped (CI runs these):")[1].splitlines()
        self.assertIn("terraform test skipped", listed[1])


class LinuxContainerL2(unittest.TestCase):
    def test_which_steps_run_in_the_container(self):
        for code in ("python3 -m unittest discover -s scripts -p 'test_*.py'", "python -m pytest scripts/", "bash scripts/t.sh",
                     "scripts/run.sh --x", "cd x && ./check.sh"):
            self.assertTrue(lc.wants_linux(code), code)
        for code in ("npx eslint .", "terraform fmt -check", "python3 scripts/tool.py", "echo hello # bash x"):
            self.assertFalse(lc.wants_linux(lc.strip_comments(code)), code)

    def test_the_image_tag_is_the_hash_of_the_dockerfile(self):
        d = pathlib.Path(tempfile.mkdtemp())
        f = d / "Dockerfile"
        f.write_text("FROM x\n", encoding="utf-8")
        first = lc.image_tag(f)
        self.assertEqual(lc.image_tag(f), first)
        f.write_text("FROM y\n", encoding="utf-8")
        self.assertNotEqual(lc.image_tag(f), first)
        self.assertRegex(lc.image_tag(), r"^onepeek-local-ci:[0-9a-f]{16}$")

    def test_the_dockerfile_base_is_the_ci_python_pinned_by_digest(self):
        text = (ROOT / "workspace/local-ci/Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(text, rf"(?m)^FROM public\.ecr\.aws/docker/library/python:{re.escape(lc.CI_PYTHON)}-slim@sha256:[0-9a-f]{{64}}$")

    def run_darwin(self, *args, docker=True):
        d = make_repo_with(job_yaml("python3 -m unittest discover -s scripts", "npx eslint ."))
        seen = []

        def fake_step(cmd, cwd, env, shell, linux=None):
            seen.append((cmd.split()[0], linux))
            return 0, "", 0.0
        out = io.StringIO()
        with mock.patch.object(lc.platform, "system", return_value="Darwin"), mock.patch.object(lc, "docker_ready", return_value=docker), \
                mock.patch.object(lc, "ensure_image", return_value="img:1"), mock.patch.object(lc, "run_step", fake_step), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = lc.main([str(d), "--jobs", "a", *args])
        return rc, seen, out.getvalue()

    def test_only_script_tests_run_in_the_container_on_macos(self):
        rc, seen, _ = self.run_darwin()
        self.assertEqual(rc, 0)
        self.assertEqual([(c, bool(l)) for c, l in seen], [("python3", True), ("npx", False)])
        self.assertEqual(seen[0][1][1], "img:1")

    def test_native_turns_it_off(self):
        _, seen, _ = self.run_darwin("--native")
        self.assertEqual([bool(l) for _, l in seen], [False, False])

    def test_without_docker_release_fails_and_otherwise_runs_natively(self):
        self.assertEqual(self.run_darwin("--release", docker=False)[0], 1)
        rc, seen, _ = self.run_darwin(docker=False)
        self.assertEqual(rc, 0)
        self.assertEqual([bool(l) for _, l in seen], [False, False])

    def test_linux_is_not_used_off_macos(self):
        d = make_repo_with(job_yaml("python3 -m unittest discover -s scripts"))
        calls = []
        with mock.patch.object(lc.platform, "system", return_value="Linux"), \
                mock.patch.object(lc, "run_step", lambda *a: calls.append(a) or (0, "", 0.0)), contextlib.redirect_stdout(io.StringIO()):
            lc.main([str(d), "--jobs", "a"])
        self.assertEqual(len(calls), 1)

    @unittest.skipUnless(shutil.which("docker") and subprocess.run(["docker", "info"], capture_output=True).returncode == 0, "Docker is needed")
    def test_the_container_has_bash_5(self):
        repo = pathlib.Path(tempfile.mkdtemp())
        rc, out, _ = lc.container_step('echo "bash=${BASH_VERSINFO[0]}"; echo "x=$FOO"', repo, {"FOO": "bar"}, repo, {}, False, lc.ensure_image())
        self.assertEqual(rc, 0, out)
        self.assertIn("bash=5", out)
        self.assertIn("x=bar", out)


class BuildImageL3(unittest.TestCase):
    def build_step(self, **with_):
        return {"uses": "docker/build-push-action@abc # v6", "with": {"context": ".", "load": True, "push": False,
                                                                      "tags": "lci-test:boot", **with_}}

    def test_a_load_build_is_translated(self):
        step = self.build_step(file="images/Dockerfile", platforms="linux/arm64", **{"build-args": "A=1\nB=${{ github.sha }}"})
        argv, env, tags = lc.build_command(step, {}, {"github.sha": "abc123"}, pathlib.Path("."), ".")
        self.assertEqual(tags, ["lci-test:boot"])
        self.assertEqual(argv[:4], ["docker", "buildx", "build", "--load"])
        for pair in (["-t", "lci-test:boot"], ["-f", "images/Dockerfile"], ["--platform", "linux/arm64"], ["--build-arg", "A=1"], ["--build-arg", "B=abc123"]):
            self.assertTrue(any(argv[i:i + 2] == pair for i in range(len(argv))), pair)
        self.assertEqual(argv[-1], ".")

    def test_what_cannot_be_translated_is_refused(self):
        for step in (self.build_step(push=True), self.build_step(**{"made-up": "x"}), self.build_step(tags="${{ steps.m.outputs.tags }}"),
                     self.build_step(secrets="npm_token=${{ secrets.NODE_AUTH_TOKEN }}")):
            with mock.patch.object(lc, "packages_token", return_value=""), self.assertRaises(ValueError):
                lc.build_command(step, {}, {}, pathlib.Path("."), ".")

    def test_an_npm_token_secret_comes_from_the_gh_login(self):
        with mock.patch.object(lc, "packages_token", return_value="tok"):
            argv, env, _ = lc.build_command(self.build_step(secrets="npm_token=${{ secrets.NODE_AUTH_TOKEN }}"), {}, {}, pathlib.Path("."), ".")
        self.assertIn("id=npm_token,env=LOCAL_CI_SECRET_NPM_TOKEN", argv)
        self.assertEqual(env, {"LOCAL_CI_SECRET_NPM_TOKEN": "tok"})
        self.assertNotIn("tok", argv)

    def job(self, build):
        wf = ("on: [pull_request]\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: docker/build-push-action@abc\n        with:\n"
              + "".join(f"          {k}: {v}\n" for k, v in build.items())
              + "      - run: |\n          echo lci-test:boot\n          touch ran\n")
        return make_repo_with(wf)

    def run_main(self, d, *args, build_rc=0):
        calls = []
        real = subprocess.run

        def fake(argv, *a, **k):
            if isinstance(argv, list) and argv[:3] in (["docker", "image", "rm"], ["docker", "buildx", "build"]):
                calls.append(argv[:3] + [argv[-1]])
                return subprocess.CompletedProcess(argv, build_rc if argv[2] == "build" else 0, "", "")
            return real(argv, *a, **k)
        out = io.StringIO()
        with mock.patch.object(lc.subprocess, "run", fake), mock.patch.object(lc, "docker_ready", return_value=True), contextlib.redirect_stdout(out):
            rc = lc.main([str(d), "--jobs", "a", *args])
        return rc, calls, out.getvalue()

    def test_the_tag_is_removed_then_built_and_the_next_step_runs(self):
        d = self.job({"context": ".", "load": "true", "push": "false", "tags": "lci-test:boot"})
        rc, calls, out = self.run_main(d)
        self.assertEqual(rc, 0, out)
        self.assertEqual(calls[0], ["docker", "image", "rm", "lci-test:boot"])
        self.assertEqual(calls[1][:3], ["docker", "buildx", "build"])
        self.assertTrue((d / "ran").exists())

    def test_a_failed_build_fails_the_run(self):
        d = self.job({"load": "true", "push": "false", "tags": "lci-test:boot"})
        self.assertEqual(self.run_main(d, build_rc=1)[0], 1)
        self.assertFalse((d / "ran").exists())

    def test_an_untranslatable_build_removes_its_tag_and_skips_the_steps_that_use_it(self):
        for build in ({"push": "true", "tags": "lci-test:boot"}, {"load": "true", "push": "false", "tags": "lci-test:boot", "made-up": "1"}):
            d = self.job(build)
            rc, calls, out = self.run_main(d)
            self.assertEqual(rc, 0, out)
            self.assertEqual(calls, [["docker", "image", "rm", "lci-test:boot"]])
            self.assertFalse((d / "ran").exists())
            self.assertIn("needs the image a skipped build makes", out)

    def test_skip_docker_never_leaves_a_stale_image_standing_in(self):
        d = self.job({"load": "true", "push": "false", "tags": "lci-test:boot"})
        rc, calls, out = self.run_main(d, "--skip-docker")
        self.assertEqual(calls, [["docker", "image", "rm", "lci-test:boot"]])
        self.assertFalse((d / "ran").exists())


class SharedPinL4(unittest.TestCase):
    """L4: the shared workflows are read at the caller's pin, not from whatever the shared checkout is on."""

    def git(self, cwd, *args):
        r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def shared_wf(self, code: str) -> str:
        return f"on:\n  workflow_call:\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - run: {code}\n"

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        self.git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.shared = self.ws / ".github"
        self.git(self.ws, "clone", "-q", str(self.origin), str(self.shared))
        (self.shared / ".github/workflows").mkdir(parents=True)
        self.wf = self.shared / ".github/workflows/w.yml"
        self.wf.write_text(self.shared_wf("exit 0"), encoding="utf-8")
        self.git(self.shared, "add", "-A")
        self.git(self.shared, "commit", "-qm", "A")
        self.A = self.git(self.shared, "rev-parse", "HEAD")
        self.wf.write_text(self.shared_wf("exit 1"), encoding="utf-8")
        self.git(self.shared, "commit", "-qam", "B")
        self.B = self.git(self.shared, "rev-parse", "HEAD")
        self.git(self.shared, "push", "-q", "origin", "main")
        self.old = lc.WORKSPACE
        lc.WORKSPACE = self.ws
        lc._SHARED.clear()

    def tearDown(self):
        lc.cleanup_shared()
        lc.WORKSPACE = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def caller(self, pin: str) -> pathlib.Path:
        wf = f"on: [pull_request]\njobs:\n  c:\n    uses: OnePeek/.github/.github/workflows/w.yml@{pin} # v1\n"
        return make_repo_with(wf)

    def run_main(self, d, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, {"ONEPEEK_SHARED_GITHUB": ""}):
            rc = lc.main([str(d), *args])
        return rc, out.getvalue()

    def test_the_pin_decides_not_the_checkout(self):
        self.assertEqual(self.git(self.shared, "rev-parse", "HEAD"), self.B)
        self.assertEqual(self.run_main(self.caller(self.A))[0], 0)
        self.assertEqual(self.run_main(self.caller(self.B))[0], 1)

    def test_shared_reads_the_given_worktree_and_says_so(self):
        d = self.caller(self.A)
        rc, out = self.run_main(d, "--shared", str(self.shared))
        self.assertEqual(rc, 1)
        self.assertIn(f"shared workflows from {self.shared.resolve()} (main {self.B[:7]}), not the pin", out)

    def test_the_environment_variable_does_the_same(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, {"ONEPEEK_SHARED_GITHUB": str(self.shared)}):
            rc = lc.main([str(self.caller(self.A))])
        self.assertEqual(rc, 1)
        self.assertIn("not the pin", out.getvalue())

    def test_a_pin_missing_locally_is_fetched_and_an_unknown_one_fails(self):
        other = self.tmp / "other"
        self.git(self.tmp, "clone", "-q", str(self.origin), str(other))
        (other / ".github/workflows/w.yml").write_text(self.shared_wf("exit 0"), encoding="utf-8")
        self.git(other, "commit", "-qam", "C")
        C = self.git(other, "rev-parse", "HEAD")
        self.git(other, "push", "-q", "origin", "main")
        self.assertNotEqual(subprocess.run(["git", "cat-file", "-e", C + "^{commit}"], cwd=self.shared).returncode, 0)
        self.assertEqual(self.run_main(self.caller(C))[0], 0)
        rc, out = self.run_main(self.caller("f" * 40))
        self.assertEqual(rc, 1)
        self.assertIn("could not be fetched", out)

    def cli(self, d, *args, **kw):
        env = {**os.environ, "ONEPEEK_WORKSPACE": str(self.ws), "ONEPEEK_SHARED_GITHUB": ""}
        return subprocess.Popen([sys.executable, str(ROOT / "workspace/local_ci.py"), str(d), *args], env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)

    def worktrees(self) -> int:
        return len(self.git(self.shared, "worktree", "list").splitlines())

    def test_the_worktree_is_removed_after_the_run_and_the_first_line_names_the_path(self):
        d = self.caller(self.A)
        p = self.cli(d.name, cwd=d.parent)
        out, _ = p.communicate(timeout=120)
        self.assertEqual(p.returncode, 0, out)
        self.assertEqual(out.splitlines()[0], f"local CI on {d.resolve()}")
        self.assertEqual(self.worktrees(), 1)

    def test_the_worktree_is_removed_after_sigterm(self):
        self.wf.write_text(self.shared_wf("sleep 30"), encoding="utf-8")
        self.git(self.shared, "commit", "-qam", "slow")
        slow = self.git(self.shared, "rev-parse", "HEAD")
        p = self.cli(self.caller(slow))
        for _ in range(100):
            if self.worktrees() == 2:
                break
            time.sleep(0.2)
        self.assertEqual(self.worktrees(), 2)
        time.sleep(0.5)
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=30)
        self.assertEqual(self.worktrees(), 1)


if __name__ == "__main__":
    unittest.main()
