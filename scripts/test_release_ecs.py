"""Steps of .github/workflows/release-ecs.yml, run for real with stub tools (aws-stack §16 "Settled from review round 2").
Run: python -m unittest discover -s scripts -p 'test_*.py'"""

import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"
RELEASE = yaml.safe_load((WORKFLOWS / "release-ecs.yml").read_text(encoding="utf-8"))
TEXT = (WORKFLOWS / "release-ecs.yml").read_text(encoding="utf-8")
DIGEST = "sha256:" + "a" * 64
ENV = {"NAME": "onepeek-preprod", "ENVIRONMENT": "preprod", "IMAGE": "backend", "MAIN": "backend", "VERSION": "1.2.3",
       "NPM_TOKEN": "", "GITHUB_SHA": "f" * 40, "AWS_REGION": "ap-south-1"}  # fmt: skip


def step(job: str, name: str) -> str:
    return next(s["run"] for s in RELEASE["jobs"][job]["steps"] if s.get("name", "").startswith(name))


def run_step(script: str, aws: str, extra_env: dict[str, str] | None = None, files: dict[str, str] | None = None):
    """Returns the result, every stub call, and what the step wrote to GITHUB_OUTPUT."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        stubs = {"aws": aws, "docker": "", "cosign": "", "curl": "", "sha256sum": "", "tar": ""}
        for tool, body in stubs.items():
            f = root / tool
            f.write_text(f'#!/bin/bash\necho "{tool} $*" >> "{root}/calls"\n{body}\n')
            f.chmod(f.stat().st_mode | stat.S_IEXEC)
        for name, content in (files or {}).items():
            (root / name).write_text(content)
        out = subprocess.run(["bash", "-eo", "pipefail", "-c", script], cwd=root, capture_output=True, text=True, env={
            **os.environ, **ENV, **(extra_env or {}), "PATH": f"{root}:{os.environ['PATH']}", "GITHUB_OUTPUT": str(root / "output"), "GITHUB_STEP_SUMMARY": str(root / "summary"),
        })  # fmt: skip
        read = lambda n: (root / n).read_text() if (root / n).exists() else ""
        return out, read("calls"), read("output")


def aws_stub(ecr: str, digest: str = DIGEST, version: str = "1.2.3") -> str:
    return f"""case "$*" in
*sts*) echo 111111111111;;
*"ecr get-login-password"*) echo pw;;
*"ecr describe-images"*) {ecr};;
*"release/backend/digest"*) echo {digest};;
*"release/backend/version"*) echo {version};;
esac"""


IN_ECR = f"echo {DIGEST}"
NOT_IN_ECR = 'echo "An error occurred (ImageNotFoundException)" >&2; exit 254'
BUILT = '{"containerimage.digest": "sha256:' + "b" * 64 + '"}'


class BuildReuse(unittest.TestCase):
    script = step("build", "Build for ARM64")

    def test_the_recorded_image_is_reused_and_nothing_is_built(self):
        out, calls, output = run_step(self.script, aws_stub(IN_ECR))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertNotIn("docker buildx", calls)
        self.assertIn(f"digest={DIGEST}", output)

    def test_a_version_not_in_ecr_is_built(self):
        out, calls, output = run_step(self.script, aws_stub(NOT_IN_ECR), files={"meta.json": BUILT})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("docker buildx build", calls)
        self.assertIn("sha256:" + "b" * 64, output)

    def test_an_ecr_digest_that_is_not_the_recorded_one_refuses(self):
        out, calls, _ = run_step(self.script, aws_stub(IN_ECR, digest="sha256:" + "c" * 64))
        self.assertEqual(out.returncode, 1)
        self.assertNotIn("docker buildx", calls)

    def test_an_image_recorded_for_another_version_refuses(self):
        self.assertEqual(run_step(self.script, aws_stub(IN_ECR, version="1.2.2"))[0].returncode, 1)

    def test_any_other_ecr_answer_stops(self):
        out, calls, _ = run_step(self.script, aws_stub('echo "AccessDeniedException" >&2; exit 254'))
        self.assertEqual(out.returncode, 1)
        self.assertNotIn("docker buildx", calls)


class SignOnce(unittest.TestCase):
    script = step("sbom-sign", "Syft SPDX SBOM")

    def signing(self, aws):
        script = re.sub(r"^.*(curl|grep |tar -xzf|\./syft).*$", "", self.script, flags=re.M)
        return run_step(script, aws, {"SIGNING_KEY": "k"}, {"digest-backend.txt": f"backend={DIGEST}\n", "sbom.spdx.json": "{}"})

    def test_a_reused_image_is_not_signed_again(self):
        out, calls, _ = self.signing(aws_stub(IN_ECR))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertNotIn("cosign sign", calls)

    def test_a_new_image_is_signed_and_attested(self):
        out, calls, _ = self.signing(aws_stub(IN_ECR, digest="sha256:" + "c" * 64))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("cosign sign", calls)
        self.assertIn("cosign attest", calls)


class RecordOnly(unittest.TestCase):
    def test_a_record_only_run_tags_nothing_and_rolls_nothing_out(self):
        for job in ("migrate", "assets", "rollout", "register-one-offs", "tag-notify"):
            self.assertIn("!inputs.record-only", RELEASE["jobs"][job]["if"], job)
        self.assertIn("needs.tag-notify.result == 'success'", RELEASE["jobs"]["api-client"]["if"])


class Migrate(unittest.TestCase):
    script = step("migrate", "Verify, render, register and run")

    def failed(self, code: str, logs: str = 'echo "the migration line"'):
        aws = f"""case "$*" in
*"s3 cp"*) echo '{{"family": "f", "containerDefinitions": [{{"name": "migrate"}}]}}';;
*register-task-definition*) echo arn:td/migrate:1;;
*run-task*) echo arn:task/abc123;;
*"containers[0].exitCode"*) echo {code};;
*get-log-events*) {logs};;
esac"""
        return run_step(self.script, aws, {"IMAGE_REF": "r@" + DIGEST, "SIGNING_KEY": "k", "DRY_RUN": "false", "GITHUB_RUN_ID": "9"})

    def test_a_failure_prints_the_tasks_awslogs_output(self):
        out, calls, _ = self.failed("1")
        self.assertEqual(out.returncode, 1)
        self.assertIn("the migration line", out.stdout)
        self.assertIn("--log-group-name /onepeek/preprod/one-off --log-stream-name migrate/migrate/abc123", calls)

    def test_unreadable_logs_still_report_the_failure(self):
        out, _, _ = self.failed("1", logs='echo "ResourceNotFoundException" >&2; exit 254')
        self.assertEqual(out.returncode, 1)
        self.assertIn("No output could be read", out.stdout)

    def test_the_login_missing_exit_names_bootstrap(self):
        self.assertIn("Run bootstrap.yml", self.failed("3")[0].stdout)

    def test_the_text_no_longer_says_json_file(self):
        self.assertNotIn("json-file", self.script)


class DeployBucketWrites(unittest.TestCase):
    def test_writes_send_no_encryption_header_so_the_bucket_default_applies(self):
        # `--sse aws:kms` with no key ID makes S3 use the AWS-managed key, not the bucket default; a key ID is the alias form the policy denies.
        for name in ("release-ecs.yml", "operate.yml"):
            text = (WORKFLOWS / name).read_text(encoding="utf-8")
            code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
            self.assertFalse([line for line in code if "--sse" in line], name)


class RenderFunction(unittest.TestCase):
    def test_the_function_sets_the_image_as_decided(self):
        function = re.search(r"# --- render-taskdef begin[^\n]*\n(.*?)# --- render-taskdef end ---", step("migrate", "Verify, render, register and run"), re.S).group(1)
        with tempfile.TemporaryDirectory() as d:
            stub = Path(d, "aws")
            stub.write_text("#!/bin/bash\necho '{\"family\": \"f\", \"containerDefinitions\": [{\"name\": \"api\"}]}'\n")
            stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
            out = subprocess.run(["bash", "-c", function + "\nrender api"], capture_output=True, text=True, env={
                **os.environ, "PATH": f"{d}:{os.environ['PATH']}", "NAME": "n", "IMAGE_REF": "r@" + DIGEST, "VERSION": "1", "GITHUB_RUN_ID": "9",
            })  # fmt: skip
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["containerDefinitions"][0]["image"], "r@" + DIGEST)


if __name__ == "__main__":
    unittest.main()
