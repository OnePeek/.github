"""Tests of change scope (scripts/change_scope.py, .github/workflows/change-scope.yml; PROC-CI-SCOPE)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
import change_scope as cs  # noqa: E402
import path_detection as pd  # noqa: E402

ROOT = Path(__file__).parent.parent
WORKFLOW = ROOT / ".github/workflows/change-scope.yml"
BASE, HEAD = "a" * 40, "b" * 40
IMAGE = "app/**\nDockerfile\n.dockerignore\npyproject.toml\nuv.lock\nREADME.md\n"
LIGHT = yaml.safe_load(WORKFLOW.read_text())[True]["workflow_call"]["inputs"]["light"]["default"]


def answer(*names):
    return json.dumps({"status": "ahead", "files": [{"filename": n, "status": "modified"} for n in names]}).encode()


def run(*names, event="pull_request", full="false", light=LIGHT, image=IMAGE, fetch=None):
    code, img, reason, warning = cs.classify(event, full, light, image, BASE, HEAD, fetch or (lambda b, h: answer(*names)))
    return code, img


def broken(b, h):
    raise pd.DetectionError("boom")


class Classify(unittest.TestCase):
    def test_env_example_only_runs_the_tests_not_the_image(self):
        # backend-service's tests/platform/config/test_config_env_example.py reads .env.example (it caught #214)
        self.assertEqual(run(".env.example"), (True, False))

    def test_an_omitted_image_input_always_builds_the_image(self):
        self.assertEqual(run("docs/a.md", image=""), (False, True))
        self.assertEqual(run("tests/x.py", image=""), (True, True))
        self.assertTrue(yaml.safe_load(WORKFLOW.read_text())[True]["workflow_call"]["inputs"]["image"]["default"] == "")

    def test_docs_only_runs_the_minimal_set(self):
        self.assertEqual(run("docs/lld/x.md", "CHANGELOG.md"), (False, False))
        self.assertEqual(run("app/modules/x/NOTES.md"), (False, True))  # under app/**: the image may ship it, fail safe

    def test_readme_is_light_for_code_but_in_the_image(self):
        self.assertEqual(run("README.md"), (False, True))

    def test_app_code_runs_code_and_image(self):
        self.assertEqual(run("app/modules/x/service.py"), (True, True))

    def test_tests_only_run_code_not_image(self):
        self.assertEqual(run("tests/unit/test_x.py"), (True, False))

    def test_dependency_file_runs_both(self):
        self.assertEqual(run("uv.lock"), (True, True))
        self.assertEqual(run("Dockerfile"), (True, True))

    def test_mixed_docs_and_code_runs_code(self):
        self.assertEqual(run("docs/a.md", "scripts/x.sh"), (True, False))

    def test_unknown_path_counts_as_code(self):
        self.assertEqual(run("weird/new.thing"), (True, False))

    def test_workflow_change_is_code(self):
        self.assertEqual(run(".github/workflows/checks.yml"), (True, False))

    def test_rename_counts_by_old_and_new_name(self):
        body = json.dumps({"files": [{"filename": "docs/b.md", "status": "renamed", "previous_filename": "app/a.py"}]}).encode()
        self.assertEqual(run(fetch=lambda b, h: body), (True, True))


class FailSafe(unittest.TestCase):
    ALL = (True, True)

    def test_push_runs_everything(self):
        self.assertEqual(run(".env.example", event="push"), self.ALL)

    def test_full_runs_everything(self):
        self.assertEqual(run(".env.example", full="true"), self.ALL)

    def test_api_error_runs_everything(self):
        self.assertEqual(run(fetch=broken), self.ALL)

    def test_bad_answer_runs_everything(self):
        self.assertEqual(run(fetch=lambda b, h: b"not json"), self.ALL)

    def test_empty_change_runs_everything(self):
        self.assertEqual(run(), self.ALL)

    def test_300_files_runs_everything(self):
        self.assertEqual(run(*[f"docs/{i}.md" for i in range(300)]), self.ALL)

    def test_empty_light_runs_everything(self):
        self.assertEqual(run("docs/a.md", light=""), self.ALL)

    def test_malformed_glob_runs_everything(self):
        self.assertEqual(run("docs/a.md", light="[x]"), self.ALL)
        self.assertEqual(run("docs/a.md", image="{a,b}"), self.ALL)

    def test_missing_sha_runs_everything(self):
        code, img, _, warning = cs.classify("pull_request", "false", LIGHT, IMAGE, "", HEAD, lambda b, h: answer("docs/a.md"))
        self.assertEqual((code, img), self.ALL)
        self.assertTrue(warning)


class Main(unittest.TestCase):
    def test_main_writes_outputs_and_never_raises(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out"
            self.assertEqual(cs.main({"EVENT_NAME": "push", "GITHUB_OUTPUT": str(out)}), 0)
            self.assertEqual(out.read_text(), "code=true\nimage=true\n")

    def test_script_exits_zero_with_nothing_set(self):
        r = subprocess.run([sys.executable, "-I", str(ROOT / "scripts/change_scope.py")], env={}, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn("code=true", r.stdout + "code=true")


class Workflow(unittest.TestCase):
    def setUp(self):
        self.doc = yaml.safe_load(WORKFLOW.read_text())

    def test_outputs_come_from_the_scope_step(self):
        job = self.doc["jobs"]["scope"]
        self.assertEqual(job["outputs"]["code"], "${{ steps.scope.outputs.code }}")
        self.assertEqual(job["outputs"]["image"], "${{ steps.scope.outputs.image }}")
        self.assertEqual(set(self.doc[True]["workflow_call"]["outputs"]), {"code", "image"})

    def test_defaults_are_light(self):
        self.assertNotIn(".env.example", LIGHT.split())
        self.assertIn("**/*.md", LIGHT.split())
        self.assertNotIn("app/**", LIGHT)

    def test_the_script_runs_isolated_and_the_job_has_no_continue_on_error(self):
        text = WORKFLOW.read_text()
        self.assertIn("python3 -I _shared/scripts/change_scope.py", text)
        self.assertNotIn("continue-on-error", text)


if __name__ == "__main__":
    unittest.main()
