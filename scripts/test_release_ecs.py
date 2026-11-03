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
        return run_step(script, aws, {"SIGNING_KEY": "k", "RUNNER_ARCH": "X64"}, {"digest-backend.txt": f"backend={DIGEST}\n", "sbom.spdx.json": "{}"})

    def test_a_reused_image_is_not_signed_again(self):
        out, calls, _ = self.signing(aws_stub(IN_ECR))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertNotIn("cosign sign", calls)

    def test_a_new_image_is_signed_and_attested(self):
        out, calls, _ = self.signing(aws_stub(IN_ECR, digest="sha256:" + "c" * 64))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("cosign sign", calls)
        self.assertIn("cosign attest", calls)


class ArchitectureAwareTools(unittest.TestCase):
    """ci-runners.md section 5.3: tool downloads follow runner.arch, both SHA-256 values pinned; QEMU only off ARM64."""
    text = (WORKFLOWS / "release-ecs.yml").read_text(encoding="utf-8")

    def test_syft_has_both_pinned_checksums_and_no_downloaded_checksum_file(self):
        self.assertIn("syft_1.55.0_linux_${syft_arch}.tar.gz", self.text)
        for sha in ("bb65249542db5fedeeb4d3f9b018b75e7264dbfbbb10486f044581a92f90490d",
                    "929bd163db2d7cd2255d3bcb8967208079813d7b46b0c952f7b794d8e2c7d304"):
            self.assertIn(sha, self.text)
        self.assertNotIn("syft.checksums", self.text)

    def test_syft_asset_follows_the_architecture_and_unknown_stops(self):
        script = re.sub(r"^.*\./syft.*$", "", step("sbom-sign", "Syft SPDX SBOM"), flags=re.M)
        files = {"digest-backend.txt": f"backend={DIGEST}\n", "sbom.spdx.json": "{}"}
        for arch, asset, rc in (("X64", "linux_amd64", 0), ("ARM64", "linux_arm64", 0), ("RISCV", "", 1)):
            out, calls, _ = run_step(script, aws_stub(IN_ECR), {"SIGNING_KEY": "k", "RUNNER_ARCH": arch}, files)
            self.assertEqual(out.returncode, rc, (arch, out.stdout, out.stderr))
            if asset:
                self.assertIn(asset, calls)
            else:
                self.assertNotIn("curl", calls)

    def test_qemu_runs_only_off_arm64(self):
        for name in ("release-ecs.yml", "deploy-ecs.yml"):
            doc = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
            found = 0
            for job in doc["jobs"].values():
                for st in job.get("steps", []):
                    if "setup-qemu-action" in st.get("uses", ""):
                        found += 1
                        self.assertIn("runner.arch != 'ARM64'", st["if"], name)
            self.assertGreaterEqual(found, 1, name)


class NoRecordOnly(unittest.TestCase):
    def test_no_workflow_has_a_record_only_mode(self):
        # decision 148: db-bootstrap runs inside the release, so the record-only first release is gone.
        for path in WORKFLOWS.glob("*.yml"):
            text = path.read_text(encoding="utf-8").lower()
            for word in ("record-only", "record_only", "recordonly"):
                self.assertNotIn(word, text, path.name)
        self.assertNotIn("record-only", RELEASE[True]["workflow_call"]["inputs"])  # PyYAML reads the key `on` as True


class DbBootstrap(unittest.TestCase):
    job = RELEASE["jobs"]["db-bootstrap"]

    def test_it_calls_infras_reusable_workflow_on_release_with_the_signed_digest(self):
        self.assertEqual(self.job["uses"], "OnePeek/onepeek-infra/.github/workflows/db-bootstrap.yml@release")
        self.assertEqual(self.job["with"]["digest"], "${{ fromJSON(needs.record-digest.outputs.digests)[inputs.image] }}")
        self.assertIn("record-digest", self.job["needs"])
        self.assertEqual(self.job["with"]["role-to-assume"], "${{ inputs.db-bootstrap-role }}")

    def test_secrets_and_permissions_are_explicit(self):
        self.assertEqual(self.job["permissions"], {"contents": "read", "id-token": "write"})
        self.assertEqual(self.job["secrets"], {"INFRA_READ_TOKEN": "${{ secrets.INFRA_READ_TOKEN }}"})

    def test_it_runs_before_migrate_and_a_failure_stops_the_release(self):
        migrate, rollout = RELEASE["jobs"]["migrate"], RELEASE["jobs"]["rollout"]
        self.assertIn("db-bootstrap", migrate["needs"])
        self.assertIn("needs.db-bootstrap.result == 'success'", migrate["if"])
        self.assertIn("db-bootstrap", rollout["needs"])
        self.assertIn("!contains(needs.*.result, 'failure')", rollout["if"])

    def test_it_never_runs_in_a_dry_run(self):
        self.assertIn("!inputs.dry-run", self.job["if"])


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

    def test_the_login_missing_exit_is_a_plain_failure(self):
        out, _, _ = self.failed("3")
        self.assertEqual(out.returncode, 1)
        self.assertIn("db-bootstrap did not create the login", out.stdout)
        self.assertNotIn("bootstrap.yml", out.stdout)

    def test_the_text_no_longer_says_json_file(self):
        self.assertNotIn("json-file", self.script)


class DeployBucketWrites(unittest.TestCase):
    def test_writes_send_no_encryption_header_so_the_bucket_default_applies(self):
        # `--sse aws:kms` with no key ID makes S3 use the AWS-managed key, not the bucket default; a key ID is the alias form the policy denies.
        for name in ("release-ecs.yml", "operate.yml"):
            text = (WORKFLOWS / name).read_text(encoding="utf-8")
            code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
            self.assertFalse([line for line in code if "--sse" in line], name)


FUNCTION = re.search(r"# --- render-taskdef begin[^\n]*\n(.*?)# --- render-taskdef end ---", step("migrate", "Verify, render, register and run"), re.S).group(1)
REF = "r@" + DIGEST


def render(template: dict, image_ref: str = REF):
    with tempfile.TemporaryDirectory() as d:
        Path(d, "t.json").write_text(json.dumps(template))
        stub = Path(d, "aws")
        stub.write_text(f"#!/bin/bash\ncat '{d}/t.json'\n")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        return subprocess.run(["bash", "-c", FUNCTION + "\nrender api"], capture_output=True, text=True, env={
            **os.environ, "PATH": f"{d}:{os.environ['PATH']}", "NAME": "n", "IMAGE_REF": image_ref, "VERSION": "1", "GITHUB_RUN_ID": "9",
        })  # fmt: skip


SIDECAR = {"name": "pgbouncer-agent", "entryPoint": ["app-run", "pgbouncer-agent"], "environment": [{"name": "ONEPEEK_SERVICE", "value": "pgbouncer"}]}
MAIN = {"name": "pgbouncer", "image": "111.dkr.ecr/onepeek-preprod/pgbouncer@sha256:" + "e" * 64, "command": ["pgbouncer", "/etc/p.ini"]}


class RenderFunction(unittest.TestCase):
    def test_the_only_container_gets_the_image(self):
        out = render({"family": "f", "containerDefinitions": [{"name": "api"}]})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["containerDefinitions"][0]["image"], REF)

    def test_a_two_container_template_fills_only_the_sidecar(self):
        out = render({"family": "f", "containerDefinitions": [MAIN, SIDECAR]})
        self.assertEqual(out.returncode, 0, out.stderr)
        main, sidecar = json.loads(out.stdout)["containerDefinitions"]
        self.assertEqual(main["image"], MAIN["image"])
        self.assertEqual(sidecar["image"], REF)

    def test_nothing_else_changes_so_iam_sec_5_and_6_still_hold(self):
        # IAM-SEC-5 (environment block) and IAM-SEC-6 (fixed entryPoint, no command) are checked on the template;
        # rendering adds only the image and the tags.
        template = {"family": "f", "taskRoleArn": "arn:role/pgbouncer", "containerDefinitions": [MAIN, SIDECAR]}
        doc = json.loads(render(template).stdout)
        for before, after in zip(template["containerDefinitions"], doc["containerDefinitions"]):
            self.assertEqual({k: v for k, v in after.items() if k != "image"}, {k: v for k, v in before.items() if k != "image"})
        self.assertNotIn("command", doc["containerDefinitions"][1])
        self.assertEqual(doc["taskRoleArn"], "arn:role/pgbouncer")
        self.assertEqual(doc["tags"], [{"key": "version", "value": "1"}, {"key": "release-run", "value": "9"}])

    def test_a_null_empty_or_missing_image_is_filled(self):
        for container in ({"name": "api", "image": None}, {"name": "api", "image": ""}, {"name": "api"}):
            out = render({"family": "f", "containerDefinitions": [container]})
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(json.loads(out.stdout)["containerDefinitions"][0]["image"], REF)

    def test_a_blank_or_non_string_image_refuses(self):
        for image in ("   ", 5, ["x"]):
            out = render({"family": "f", "containerDefinitions": [{**MAIN, "image": image}, SIDECAR]})
            self.assertNotEqual(out.returncode, 0, image)
            self.assertIn("pgbouncer", out.stderr)
            self.assertEqual(out.stdout, "")

    def test_an_empty_release_image_refuses(self):
        self.assertNotEqual(render({"family": "f", "containerDefinitions": [{"name": "api"}]}, image_ref="").returncode, 0)

    def test_a_placeholder_left_refuses(self):
        self.assertNotEqual(render({"family": "f", "containerDefinitions": [{"name": "api", "x": "__IMAGE__"}]}).returncode, 0)


INDEX = {
    "onepeek-preprod-api": {"image": "backend", "kind": "service", "service": "api", "order": 1},
    "onepeek-preprod-db-bootstrap": {"image": "backend", "kind": "one-off", "service": "db-bootstrap", "order": 1},
    "onepeek-preprod-migrate": {"image": "backend", "kind": "one-off", "service": "migrate", "order": 1},
    "onepeek-preprod-pgbouncer": {"image": "backend", "kind": "daemon", "service": "pgbouncer", "order": 0},
    "onepeek-preprod-valkey": {"image": "backend", "kind": "daemon", "service": "valkey", "order": 0},
    "onepeek-preprod-web": {"image": "web", "kind": "service", "service": "web", "order": 1},
    "onepeek-preprod-worker": {"image": "backend", "kind": "service", "service": "worker", "order": 1},
}


def families(index: dict, image: str = "backend"):
    aws = f"""case "$*" in *"taskdef/index.json"*) echo '{json.dumps(index)}' > index.json;; esac"""
    out, _, output = run_step(step("rollout", "The families this image rolls"), aws, {"IMAGE": image})
    return out, dict(line.split("=", 1) for line in output.splitlines())


class RolloutOrder(unittest.TestCase):
    def test_daemons_first_then_services_never_one_offs_never_other_images(self):
        out, output = families(INDEX)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(output["groups"]), [["pgbouncer", "valkey"], ["api", "worker"]])
        self.assertEqual(json.loads(output["services"]), ["pgbouncer", "valkey", "api", "worker"])

    def test_the_web_release_rolls_only_web(self):
        out, output = families(INDEX, "web")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(output["groups"]), [["web"]])

    def test_a_bad_index_entry_refuses(self):
        self.assertNotEqual(families({**INDEX, "x": {"image": "backend", "kind": "sidecar", "service": "x", "order": 0}})[0].returncode, 0)

    def test_order_must_be_a_real_int_names_lowercase_and_services_unique(self):
        ok = {"image": "backend", "kind": "service", "service": "x", "order": 0}
        for bad in (
            {**ok, "order": True},
            {**ok, "service": "Bad_Name"},
            {**ok, "service": "a b"},
            {**ok, "service": 5},
        ):
            self.assertNotEqual(families({**INDEX, "x": bad})[0].returncode, 0, bad)
        self.assertNotEqual(families({**INDEX, "Bad_Family": ok})[0].returncode, 0)
        dup = families({**INDEX, "onepeek-preprod-api2": {**ok, "service": "api"}})[0]
        self.assertNotEqual(dup.returncode, 0)
        self.assertIn("twice", dup.stderr)

    def test_a_failed_wait_stops_the_next_order_and_exits_1(self):
        aws = """case "$*" in
*"s3 cp"*) echo '{"family": "f", "containerDefinitions": [{"name": "c"}]}';;
*register-task-definition*) echo arn:td/f:1;;
*services-stable*) exit 255;;
esac"""
        out, calls, _ = run_step(step("rollout", "Verify, render, register and update each order"), aws, {
            "IMAGE_REF": REF, "SIGNING_KEY": "k", "DRY_RUN": "false", "GITHUB_RUN_ID": "9", "ORDERS": '[["pgbouncer", "valkey"], ["api", "worker"]]'})  # fmt: skip
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("did not become stable", out.stdout)
        self.assertIn("update-service --cluster onepeek-preprod --service valkey", calls)
        self.assertNotIn("--service api", calls)
        self.assertNotIn("--service worker", calls)
        self.assertEqual(calls.count("services-stable"), 1)

    def test_each_order_is_stable_before_the_next_starts(self):
        aws = """case "$*" in
*"s3 cp"*) echo '{"family": "f", "containerDefinitions": [{"name": "c"}]}';;
*register-task-definition*) echo arn:td/f:1;;
esac"""
        out, calls, _ = run_step(step("rollout", "Verify, render, register and update each order"), aws, {
            "IMAGE_REF": REF, "SIGNING_KEY": "k", "DRY_RUN": "false", "GITHUB_RUN_ID": "9", "ORDERS": '[["pgbouncer", "valkey"], ["api", "worker"]]'})  # fmt: skip
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        ecs = [line for line in calls.splitlines() if "update-service" in line or "services-stable" in line]
        order = [("update", "pgbouncer"), ("update", "valkey"), ("wait", "pgbouncer valkey"), ("update", "api"), ("update", "worker"), ("wait", "api worker")]
        seen = []
        for line in ecs:
            if "services-stable" in line:
                seen.append(("wait", line.split("--services ", 1)[1].strip()))
            else:
                seen.append(("update", line.split("--service ", 1)[1].split()[0]))
        self.assertEqual(seen, order)

    def test_a_dry_run_renders_every_family_and_changes_nothing(self):
        aws = """case "$*" in *"s3 cp"*) echo '{"family": "f", "containerDefinitions": [{"name": "c"}]}';; esac"""
        out, calls, _ = run_step(step("rollout", "Verify, render, register and update each order"), aws, {
            "IMAGE_REF": REF, "SIGNING_KEY": "k", "DRY_RUN": "true", "GITHUB_RUN_ID": "9", "ORDERS": '[["pgbouncer"], ["api"]]'})  # fmt: skip
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("rendered 2 task definitions", out.stdout)
        self.assertNotIn("register-task-definition", calls)
        self.assertNotIn("update-service", calls)


class Smoke(unittest.TestCase):
    def smoke(self, services: str, images: list[str], svc_td: str = "arn:td/x:2"):
        aws = f"""case "$*" in
*describe-services*) echo {svc_td};;
*describe-task-definition*) echo '{json.dumps(images)}';;
esac"""
        return run_step(step("smoke", "The edge answers"), aws, {"DOMAIN": "d", "IMAGE": "backend", "DIGEST": DIGEST, "SERVICES": services})[0]

    def test_a_daemon_passes_when_its_sidecar_runs_the_digest_whatever_the_order(self):
        sidecar, main = f"x/backend@{DIGEST}", "x/pgbouncer@sha256:" + "e" * 64
        for images in ([sidecar, main], [main, sidecar]):
            out = self.smoke('["pgbouncer"]', images)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_a_service_on_another_digest_fails(self):
        self.assertEqual(self.smoke('["api"]', ["x/backend@sha256:" + "c" * 64]).returncode, 1)

    def test_a_digest_only_in_the_middle_of_an_image_name_fails(self):
        self.assertEqual(self.smoke('["api"]', [f"x/backend@{DIGEST}-other"]).returncode, 1)


class DbBootstrapRoleGuard(unittest.TestCase):
    def guard(self, role: str, migrate: str = "true", dry: str = "false"):
        return run_step(step("version", "Refuse at once"), "", {"MIGRATE": migrate, "DRY_RUN": dry, "ROLE": role, "ENVIRONMENT_NAME": "preprod"})[0]

    def test_an_empty_role_on_a_migrating_release_fails_naming_the_variable(self):
        out = self.guard("")
        self.assertEqual(out.returncode, 1)
        self.assertIn("AWS_DB_BOOTSTRAP_ROLE_PREPROD", out.stdout)

    def test_it_passes_with_a_role_without_migrate_or_in_a_dry_run(self):
        self.assertEqual(self.guard("arn:role").returncode, 0)
        self.assertEqual(self.guard("", migrate="false").returncode, 0)
        self.assertEqual(self.guard("", dry="true").returncode, 0)


SERVICES_FILE = """version: 1
defaults: {image: backend}
services:
  api: {triggers: [request]}
  worker: {triggers: [event, job]}
  ops: {triggers: []}
  web: {image: web, triggers: [request]}
"""


class OneServicesFile(unittest.TestCase):
    """Decision 153; delivery-pipelines §13 "Services file a release uses": the copy infra's last apply published."""

    def services(self, image: str):
        aws = f"""case "$*" in *"s3 cp s3://onepeek-preprod-deploy/services.preprod.yaml services.yaml"*) printf '%s' '{SERVICES_FILE}' > services.yaml;; esac"""
        out, calls, output = run_step(step("rollout", "The services file's services for this image"), aws, {"IMAGE": image})
        return out, calls, dict(line.split("=", 1) for line in output.splitlines())

    def test_e7_reads_the_published_copy_in_the_deploy_bucket(self):
        out, calls, output = self.services("backend")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("aws s3 cp s3://onepeek-preprod-deploy/services.preprod.yaml services.yaml", calls)
        self.assertEqual(json.loads(output["services"]), ["api", "worker"])
        self.assertEqual(json.loads(self.services("web")[2]["services"]), ["web"])

    def test_the_gateway_routes_read_the_same_file(self):
        self.assertIn("app api routes --env \"${ENVIRONMENT}\" --services-file services.yaml", step("rollout", "The gateway routes"))

    def test_no_infra_pin_and_no_infra_checkout_in_the_rollout(self):
        self.assertNotIn("infra-version.txt", TEXT)
        steps = RELEASE["jobs"]["rollout"]["steps"]
        self.assertFalse([s for s in steps if "onepeek-infra" in str(s.get("with", {}).get("repository", ""))])
        self.assertNotIn("_infra", json.dumps(steps))


if __name__ == "__main__":
    unittest.main()
