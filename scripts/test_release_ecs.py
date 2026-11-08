"""Steps of .github/workflows/release-ecs.yml, run for real with stub tools (aws-stack §16 "Settled from review round 2").
Run: python -m unittest discover -s scripts -p 'test_*.py'"""

import json
import os
import pathlib
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
OPERATE = (WORKFLOWS / "operate.yml").read_text(encoding="utf-8")
DIGEST = "sha256:" + "a" * 64
COMMIT = "sha-" + "f" * 12
ENV = {"NAME": "onepeek-preprod", "ENVIRONMENT": "preprod", "IMAGE": "backend", "MAIN": "backend", "VERSION": "1.2.3",
       "COMMIT": COMMIT, "NPM_TOKEN": "", "GITHUB_SHA": "f" * 40, "AWS_REGION": "ap-south-1"}  # fmt: skip


def step(job: str, name: str) -> str:
    return next(s["run"] for s in RELEASE["jobs"][job]["steps"] if s.get("name", "").startswith(name))


def needs_of(job: str) -> list[str]:
    n = RELEASE["jobs"][job].get("needs", [])
    return [n] if isinstance(n, str) else n


def run_step(script: str, aws: str, extra_env: dict[str, str] | None = None, files: dict[str, str] | None = None,
             extra_stubs: dict[str, str] | None = None, with_summary: bool = False):
    """Returns the result, every stub call, and what the step wrote to GITHUB_OUTPUT (and GITHUB_STEP_SUMMARY)."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        stubs = {"aws": aws, "docker": "", "cosign": "", "curl": "", "sha256sum": "", "tar": "", **(extra_stubs or {})}
        for tool, body in stubs.items():
            f = root / tool
            # docker and cosign take a password on stdin (`aws ecr get-login-password | docker login --password-stdin`),
            # as the real tools do: a stand-in that exits without reading it gives the writer SIGPIPE (exit 141 under
            # pipefail), which raced on Linux (pitfall 11; .github #64 and #65 local CI, 2026-11-06). Only these two
            # drain it: a stub called inside a `while read` loop must not swallow the loop's input.
            drain = "if [ -p /dev/stdin ]; then cat >/dev/null; fi\n" if tool in ("docker", "cosign") else ""
            f.write_text(f'#!/bin/bash\n{drain}echo "{tool} $*" >> "{root}/calls"\n{body}\n')
            f.chmod(f.stat().st_mode | stat.S_IEXEC)
        for name, content in (files or {}).items():
            (root / name).write_text(content)
        out = subprocess.run(["bash", "-eo", "pipefail", "-c", script], cwd=root, capture_output=True, text=True, stdin=subprocess.DEVNULL, env={
            **os.environ, **ENV, **(extra_env or {}), "PATH": f"{root}:{os.environ['PATH']}", "GITHUB_OUTPUT": str(root / "output"), "GITHUB_STEP_SUMMARY": str(root / "summary"),
        })  # fmt: skip
        read = lambda n: (root / n).read_text() if (root / n).exists() else ""
        if with_summary:
            return out, read("calls"), read("output"), read("summary"), {n: read(n) for n in ("deployment.json", "status.json")}
        return out, read("calls"), read("output")


def aws_stub(ecr: str, digest: str = DIGEST, commit: str = COMMIT) -> str:
    return f"""case "$*" in
*sts*) echo 111111111111;;
*"ecr get-login-password"*) echo pw;;
*"ecr describe-images"*) {ecr};;
*"release/backend/digest"*) echo {digest};;
*"release/backend/commit"*) echo {commit};;
*"release/backend/version"*) echo "the version record must not be read" >&2; exit 9;;
esac"""


IN_ECR = f"echo {DIGEST}"
NOT_IN_ECR = 'echo "An error occurred (ImageNotFoundException)" >&2; exit 254'
BUILT = '{"containerimage.digest": "sha256:' + "b" * 64 + '"}'


class BuildReuse(unittest.TestCase):
    """Playbook §5.2: the preprod image is keyed on the commit; a re-run of the same commit reuses its image."""
    script = step("build-image", "Build for ARM64")

    def test_this_commits_image_is_looked_up_by_sha_12_and_reused_without_a_build(self):
        out, calls, output = run_step(self.script, aws_stub(IN_ECR))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn(f"--image-ids imageTag={COMMIT}", calls)
        self.assertNotIn("imageTag=v", calls)
        self.assertNotIn("docker buildx", calls)
        self.assertIn(f"digest={DIGEST}", output)

    def test_a_second_merge_with_the_same_version_still_builds(self):
        # Two merges in a row without a version bump (playbook §5.2): the version plays no part in the lookup.
        out, calls, output = run_step(self.script, aws_stub(NOT_IN_ECR, digest="sha256:" + "c" * 64, commit="sha-" + "0" * 12), files={"meta.json": BUILT})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("docker buildx build", calls)
        self.assertIn("sha256:" + "b" * 64, output)
        self.assertNotIn("version bump", out.stdout + out.stderr)

    def test_an_unrecorded_image_of_this_commit_is_reused_not_refused(self):
        out, calls, output = run_step(self.script, aws_stub(IN_ECR, digest="sha256:" + "c" * 64, commit="none"))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertNotIn("docker buildx", calls)
        self.assertIn(f"digest={DIGEST}", output)

    def test_only_the_commit_tag_is_pushed_and_the_version_is_metadata(self):
        out, calls, _ = run_step(self.script, aws_stub(NOT_IN_ECR), files={"meta.json": BUILT})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        build = next(line for line in calls.splitlines() if "docker buildx build" in line)
        self.assertEqual(re.findall(r"--tag (\S+)", build), [f"111111111111.dkr.ecr.ap-south-1.amazonaws.com/onepeek-preprod/backend:{COMMIT}"])
        self.assertIn(f"org.opencontainers.image.version=1.2.3+{COMMIT}", build)
        self.assertIn(f"VERSION=1.2.3+{COMMIT}", build)
        self.assertIn("ONEPEEK_COMMIT=" + "f" * 40, build)

    def test_the_web_image_is_built_with_release_sha_12(self):
        out, calls, _ = run_step(self.script, aws_stub(NOT_IN_ECR), {"IMAGE": "web", "MAIN": "web"}, files={"meta.json": BUILT})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn(f"--build-arg RELEASE={COMMIT}", calls)

    def test_extra_images_are_refused_until_their_dockerfile_location_exists(self):
        # delivery-pipelines §16: the sandbox image waits for document parsing; extra-images stays empty until then.
        for extra in ("sandbox", "sandbox, other"):
            out, calls, _ = run_step(self.script, aws_stub(IN_ECR), {"EXTRA": extra})
            self.assertEqual(out.returncode, 1, extra)
            self.assertIn("No Dockerfile location is defined for the extra images", out.stdout)
            self.assertNotIn("aws ", calls)
        for extra in ("", " ", ","):
            self.assertEqual(run_step(self.script, aws_stub(IN_ECR), {"EXTRA": extra})[0].returncode, 0, repr(extra))

    def test_any_other_ecr_answer_stops(self):
        out, calls, _ = run_step(self.script, aws_stub('echo "AccessDeniedException" >&2; exit 254'))
        self.assertEqual(out.returncode, 1)
        self.assertNotIn("docker buildx", calls)


class Version(unittest.TestCase):
    script = step("prepare", "Record the commit")

    def test_the_commit_is_recorded_and_an_existing_version_tag_does_not_stop_preprod(self):
        out, calls, output = run_step(self.script, "", {"VERSION_FILE": "m.json", "EXTRA": ""}, files={"m.json": '{".": "1.2.3"}'}, extra_stubs={"git": "exit 0"})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn(f"commit={COMMIT}", output)
        self.assertIn("version=1.2.3", output)
        self.assertNotIn("git", calls)

    def test_no_job_checks_or_creates_a_version_tag(self):
        self.assertNotIn("refs/tags/v", TEXT)
        self.assertNotIn("gh release", TEXT)
        self.assertNotIn("tag-notify", RELEASE["jobs"])


class SignOnce(unittest.TestCase):
    script = step("build-image", "Syft SPDX SBOM")

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

    def test_an_image_recorded_for_another_commit_is_signed(self):
        out, calls, _ = self.signing(aws_stub(IN_ECR, commit="sha-" + "0" * 12))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("cosign sign", calls)

    def test_the_record_is_digest_and_commit_never_version(self):
        out, calls, _ = self.signing(aws_stub(IN_ECR, digest="sha256:" + "c" * 64))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn(f"put-parameter --name /onepeek/preprod/release/backend/digest --type String --overwrite --value {DIGEST}", calls)
        self.assertIn(f"put-parameter --name /onepeek/preprod/release/backend/commit --type String --overwrite --value {COMMIT}", calls)
        self.assertNotIn("release/backend/version", calls)


class CosignArguments(unittest.TestCase):
    """cosign v2.6.5: signatures are OCI 1.1 referrers via sign --registry-referrers-mode=oci-1-1, read by verify --experimental-oci11;
    attest has no --registry-referrers-mode and stores its referrer with --new-bundle-format (delivery-pipelines §4 row 4, §5)."""

    SIGN = "cosign sign --yes --tlog-upload=false --registry-referrers-mode=oci-1-1 --key k "
    ATTEST = "cosign attest --yes --tlog-upload=false --new-bundle-format --type spdxjson --predicate sbom.spdx.json --key k "
    VERIFY = "cosign verify --experimental-oci11 --insecure-ignore-tlog=true --key "

    def cosign_lines(self):
        """Every cosign command in the shared workflows, with backslash continuations joined."""
        lines = []
        for f in sorted(WORKFLOWS.glob("*.yml")):
            joined = re.sub(r"\\\n\s*", "", f.read_text(encoding="utf-8"))
            lines += [(f.name, m.group(0)) for m in re.finditer(r"\bcosign (sign|attest|verify|verify-attestation|copy)\b[^\n]*", joined)]
        return lines

    def test_signing_runs_the_exact_sign_and_attest_arguments(self):
        out, calls, _ = SignOnce().signing(aws_stub(IN_ECR, digest="sha256:" + "c" * 64))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        cosign = [c for c in calls.splitlines() if c.startswith("cosign ")]
        self.assertEqual(len(cosign), 2, cosign)
        self.assertTrue(cosign[0].startswith(self.SIGN), cosign[0])
        self.assertTrue(cosign[1].startswith(self.ATTEST), cosign[1])
        self.assertEqual(cosign[0].removeprefix(self.SIGN), cosign[1].removeprefix(self.ATTEST), "sign and attest the same digest")
        self.assertRegex(cosign[0].removeprefix(self.SIGN), r"^\S+@sha256:[0-9a-f]{64}$")

    def test_every_signer_writes_referrers_and_every_verifier_reads_them(self):
        lines = self.cosign_lines()
        kinds = {line.split()[1] for _, line in lines}
        self.assertEqual(kinds, {"sign", "attest", "verify"}, lines)
        for name, line in lines:
            with self.subTest(file=name, line=line):
                if line.startswith("cosign sign "):
                    self.assertIn("--registry-referrers-mode=oci-1-1", line.split())
                    self.assertIn("--tlog-upload=false", line.split())
                elif line.startswith("cosign attest "):
                    self.assertIn("--new-bundle-format", line.split())
                    self.assertIn("--tlog-upload=false", line.split())
                    self.assertFalse(any(a.startswith("--registry-referrers-mode") for a in line.split()), "attest has no such flag in cosign v2.6.5")
                else:
                    self.assertTrue(line.startswith(self.VERIFY), line)

    def test_the_referrers_mode_has_its_experimental_switch(self):
        self.assertEqual(RELEASE["env"]["COSIGN_EXPERIMENTAL"], "1")

    def test_every_job_installs_the_same_cosign(self):
        for name in ("release-ecs.yml", "operate.yml"):
            with self.subTest(file=name):
                releases = set(re.findall(r"cosign-release: (\S+)", (WORKFLOWS / name).read_text(encoding="utf-8")))
                self.assertEqual(releases, {"v2.6.5"})


class RecordDigest(unittest.TestCase):
    script = step("build-image", "Read the recorded digests")

    def read(self, commit: str, dry: str = "false"):
        return run_step(self.script, aws_stub(IN_ECR, commit=commit), {"IMAGES": '["backend"]', "MAIN_IMAGE": "backend", "SIGNING_KEY": "k", "RELEASE_COMMIT": COMMIT, "DRY_RUN": dry})

    def test_this_commits_record_is_verified_and_passed_on(self):
        out, calls, output = self.read(COMMIT)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("cosign verify", calls)
        self.assertIn(f"commit={COMMIT}", output)
        # The main image's digest and reference, for db-bootstrap, the deploy job's renders and the smoke digest check.
        self.assertIn(f"digest={DIGEST}\n", output)
        self.assertIn(f"image-ref=111111111111.dkr.ecr.ap-south-1.amazonaws.com/onepeek-preprod/backend@{DIGEST}\n", output)

    def test_a_record_of_another_commit_refuses(self):
        out, _, _ = self.read("sha-" + "0" * 12)
        self.assertEqual(out.returncode, 1)
        self.assertIn("SSM records commit", out.stdout)

    def test_a_dry_run_uses_the_recorded_commit(self):
        out, _, output = self.read("sha-" + "0" * 12, dry="true")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("commit=sha-" + "0" * 12, output)


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
        script = step("build-image", "Install syft")
        files = {}
        for arch, asset, rc in (("X64", "linux_amd64", 0), ("ARM64", "linux_arm64", 0), ("RISCV", "", 1)):
            out, calls, _ = run_step(script, aws_stub(IN_ECR), {"RUNNER_ARCH": arch, "VERSION": "1.55.0", "SHA256_A": "a", "SHA256_B": "b", "RUNNER_TEMP": os.environ.get("TMPDIR", "/tmp")}, files)
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
        self.assertEqual(self.job["with"]["digest"], "${{ needs.build-image.outputs.digest || needs.build-web.outputs.digest }}")
        self.assertEqual(RELEASE["jobs"]["build-image"]["outputs"]["digest"], "${{ steps.record.outputs.digest }}")
        self.assertEqual(self.job["needs"], ["prepare", "build-image", "build-web"])
        self.assertEqual(self.job["with"]["role-to-assume"], "${{ inputs.db-bootstrap-role }}")

    def test_secrets_and_permissions_are_explicit(self):
        self.assertEqual(self.job["permissions"], {"contents": "read", "id-token": "write"})
        self.assertEqual(self.job["secrets"], {
            "AGENTS_APP_CLIENT_ID": "${{ secrets.AGENTS_APP_CLIENT_ID }}",
            "AGENTS_APP_PRIVATE_KEY": "${{ secrets.AGENTS_APP_PRIVATE_KEY }}"})  # the App only (playbook §8a S6)

    def test_infra_read_token_is_retired(self):
        self.assertNotIn("INFRA_READ_TOKEN", RELEASE[True]["workflow_call"]["secrets"])
        self.assertEqual({n for n, j in RELEASE["jobs"].items() if "INFRA_READ_TOKEN" in str(j)}, set())

    def test_it_runs_before_migrate_and_a_failure_stops_the_release(self):
        deploy = RELEASE["jobs"]["deploy"]
        self.assertIn("db-bootstrap", deploy["needs"])
        self.assertIn("(!inputs.migrate || inputs.dry-run || needs.db-bootstrap.result == 'success')", deploy["if"])
        self.assertIn("!contains(needs.*.result, 'failure')", deploy["if"])

    def test_it_is_the_only_job_that_is_not_a_job_of_ours(self):
        # A reusable-workflow call under its own role cannot be a step, so it stays a job (foundation-reset W2.1).
        self.assertEqual({n for n, j in RELEASE["jobs"].items() if "uses" in j}, {"db-bootstrap"})

    def test_it_never_runs_in_a_dry_run(self):
        self.assertIn("!inputs.dry-run", self.job["if"])


class PackageRead(unittest.TestCase):
    """Least privilege: only the web build's token reads packages, and only the web image gets the npm_token secret."""
    jobs = RELEASE["jobs"]
    push = next(s for s in RELEASE["jobs"]["build-web"]["steps"] if s.get("name", "").startswith("Build for ARM64"))

    def test_only_build_web_has_packages_read_and_it_runs_only_for_web(self):
        reads = {n for n, j in self.jobs.items() if (j.get("permissions") or {}).get("packages") == "read"}
        self.assertEqual(reads, {"build-web"})
        self.assertEqual(self.jobs["build-web"]["permissions"], {"contents": "read", "id-token": "write", "packages": "read"})
        self.assertEqual(self.jobs["build-image"]["permissions"], {"contents": "read", "id-token": "write"})
        self.assertIn("inputs.image == 'web'", self.jobs["build-web"]["if"])
        self.assertIn("inputs.image != 'web'", self.jobs["build-image"]["if"])
        self.assertEqual(self.jobs["build-web"]["steps"], self.jobs["build-image"]["steps"])  # one set of steps

    def test_the_npm_token_is_the_run_token_for_the_web_image_only(self):
        self.assertEqual(self.push["env"]["NPM_TOKEN"], "${{ inputs.image == 'web' && github.token || '' }}")

    def test_the_backend_build_gets_no_npm_secret(self):
        out, calls, _ = run_step(self.push["run"], aws_stub(NOT_IN_ECR), {"NPM_TOKEN": ""}, files={"meta.json": BUILT})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertNotIn("npm_token", calls)
        out, calls, _ = run_step(self.push["run"], aws_stub(NOT_IN_ECR), {"IMAGE": "web", "MAIN": "web", "NPM_TOKEN": "t"}, files={"meta.json": BUILT})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("--secret id=npm_token,env=NPM_TOKEN", calls)

    def test_the_two_build_jobs_are_exclusive_and_later_jobs_need_one_to_succeed(self):
        # No join job: each later job needs both and requires one of them to have succeeded (a failed, cancelled or
        # doubly skipped build stops the release there).
        self.assertNotIn("build", self.jobs)
        self.assertEqual(self.jobs["build-web"]["outputs"], self.jobs["build-image"]["outputs"])
        for job in ("db-bootstrap", "deploy"):
            self.assertEqual(needs_of(job)[1:3], ["build-image", "build-web"], job)
            self.assertIn("(needs.build-image.result == 'success' || needs.build-web.result == 'success')", self.jobs[job]["if"], job)
        for job in ("verify", "finish"):
            self.assertTrue({"build-image", "build-web", "deploy"} <= set(needs_of(job)), job)

    def test_npm_read_token_is_ignored_and_optional(self):
        self.assertFalse(RELEASE[True]["workflow_call"]["secrets"]["NPM_READ_TOKEN"]["required"])
        self.assertNotIn("secrets.NPM_READ_TOKEN", TEXT)


import test_release_lock as L  # noqa: E402  (the fake S3 and ECS of the release-lock tests)


class Migrate(unittest.TestCase):
    """Row 5 with the phases of playbook §5.4a "Rollback on cancel": migrating (no ARN) before run-task, the ARN after
    it, locked once the task has stopped; a failed phase write means the key is lost and the run stops."""

    script = step("deploy", "Verify, render, register and run")

    def migrate(self, code: str = "0", held: str | None = None, **extra: str):
        held = held or L.mine()
        return L.run(self.script, keys={L.LEGACY: held, L.BACKEND: held}, stubs={"cosign": "if [ -p /dev/stdin ]; then cat >/dev/null; fi"}, **{
            "LOCK_TOKEN": L.TOKEN, "IMAGE_REF": "r@" + DIGEST, "SIGNING_KEY": "k", "DRY_RUN": "false", "COMMIT": COMMIT, "VERSION": "",
            "AWS_CP": '{"family": "f", "containerDefinitions": [{"name": "migrate"}]}', "AWS_REGISTER_TASK_DEFINITION": "arn:td/migrate:1",
            "AWS_RUN_TASK": L.TASK, "AWS_DESCRIBE_TASKS": code, "AWS_GET_LOG_EVENTS": "the migration line", **extra})  # fmt: skip

    def test_migrating_is_written_before_run_task_which_carries_started_by_run_id_and_attempt(self):
        out = self.migrate()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        lines = out.calls.splitlines()
        run_task = next(i for i, l in enumerate(lines) if "ecs run-task" in l)
        self.assertIn("--started-by 5000-1", lines[run_task])
        first_puts = [i for i, l in enumerate(lines) if "put-object" in l][:2]
        self.assertTrue(first_puts and all(i < run_task for i in first_puts))
        phases = [(json.loads(p.split(" ", 1)[1])["phase"], json.loads(p.split(" ", 1)[1]).get("migrate_task")) for p in out.puts.splitlines()]
        self.assertEqual(phases, [("migrating", None)] * 2 + [("migrating", L.TASK)] * 2 + [("locked", None)] * 2)
        wait = next(i for i, l in enumerate(lines) if "tasks-stopped" in l)
        self.assertLess(wait, [i for i, l in enumerate(lines) if "put-object" in l][4])

    def test_a_lost_key_starts_no_migrate_task(self):
        out = self.migrate(held=L.record(L.REPO, L.RUN, token="e" * 32, scope="backend"))
        self.assertEqual(out.returncode, 1)
        self.assertIn("lost the release lock", out.stdout)
        self.assertNotIn("run-task", out.calls)

    def test_a_failure_prints_the_tasks_awslogs_output_after_the_task_stopped(self):
        out = self.migrate("1")
        self.assertEqual(out.returncode, 1)
        self.assertIn("the migration line", out.stdout)
        self.assertIn("--log-group-name /onepeek/preprod/one-off --log-stream-name migrate/migrate/abc123", out.calls)
        self.assertEqual(json.loads(out.keys[L.BACKEND])["phase"], "locked")   # the task has stopped: nothing to wait for

    def test_unreadable_logs_still_report_the_failure(self):
        out = self.migrate("1", FAIL_GET_LOG_EVENTS="ResourceNotFoundException")
        self.assertEqual(out.returncode, 1)
        self.assertIn("No output could be read", out.stdout)

    def test_the_login_missing_exit_is_a_plain_failure(self):
        out = self.migrate("3")
        self.assertEqual(out.returncode, 1)
        self.assertIn("db-bootstrap did not create the login", out.stdout)
        self.assertNotIn("bootstrap.yml", out.stdout)

    def test_the_text_no_longer_says_json_file(self):
        self.assertNotIn("json-file", self.script)


class Rolling(unittest.TestCase):
    """deploy writes rolling with previous and previous_lambda before the first update-service; verify writes done or
    locked (playbook §5.4a "Rollback on cancel")."""

    script = step("deploy", "Record every service's current task definition")

    def previous(self, held: str | None = None, **extra: str):
        held = held or L.mine()
        return L.run(self.script, keys={L.LEGACY: held, L.BACKEND: held}, **{
            "LOCK_TOKEN": L.TOKEN, "SERVICES": '["api", "worker"]', "HAS_SANDBOX": "yes", "DRY_RUN": "false",
            "AWS_DESCRIBE_SERVICES": L.PREV["api"], "AWS_LIST_VERSIONS_BY_FUNCTION": "$LATEST 1 2 3", **extra})  # fmt: skip

    def test_rolling_is_written_with_previous_and_previous_lambda_and_then_the_output(self):
        out = self.previous()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        rec = json.loads(out.keys[L.BACKEND])
        self.assertEqual(rec["phase"], "rolling")
        self.assertEqual(rec["previous"], {"api": L.PREV["api"], "worker": L.PREV["api"]})
        self.assertEqual(rec["previous_lambda"], {"onepeek-preprod-sandbox-parse": "3", "onepeek-preprod-sandbox-fetch": "3"})
        self.assertIn('previous={"api": ', out.output)

    def test_a_lost_key_writes_no_previous_so_verify_never_rolls_back(self):
        out = self.previous(held=L.record(L.REPO, L.RUN, token="e" * 32, scope="backend"))
        self.assertEqual(out.returncode, 1)
        self.assertNotIn("previous=", out.output)

    def test_a_dry_run_writes_no_phase(self):
        out = self.previous(DRY_RUN="true")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(out.puts, "")

    def test_it_comes_before_the_first_update_service(self):
        names = [s.get("name", "") for s in RELEASE["jobs"]["deploy"]["steps"]]
        rec = names.index(next(n for n in names if n.startswith("Record every service's current task definition")))
        roll = names.index(next(n for n in names if n.startswith("Verify, render, register and update each order")))
        self.assertLess(rec, roll)

    def test_verify_writes_done_after_a_clean_watch_and_locked_after_a_stable_rollback(self):
        end = step("verify", "Record the end in the release lock")
        for watch, phase in (("success", "done"), ("failure", "locked")):
            held = L.mine("rolling", previous=L.PREV)
            out = L.run(end, keys={L.LEGACY: held, L.BACKEND: held}, LOCK_TOKEN=L.TOKEN, WATCH=watch)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            rec = json.loads(out.keys[L.BACKEND])
            self.assertEqual(rec["phase"], phase)
            self.assertNotIn("previous", rec)
        st = next(s for s in RELEASE["jobs"]["verify"]["steps"] if s.get("name", "").startswith("Record the end"))
        self.assertEqual(st["if"], "${{ !cancelled() && steps.guard.outcome == 'success' && steps.creds.outcome == 'success' && "
                                   "(steps.watch.outcome == 'success' || steps.rollback.outcome == 'success') }}")


class DeployBucketWrites(unittest.TestCase):
    def test_writes_send_no_encryption_header_so_the_bucket_default_applies(self):
        # `--sse aws:kms` with no key ID makes S3 use the AWS-managed key, not the bucket default; a key ID is the alias form the policy denies.
        for name in ("release-ecs.yml", "operate.yml"):
            text = (WORKFLOWS / name).read_text(encoding="utf-8")
            code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
            self.assertFalse([line for line in code if "--sse" in line], name)


FUNCTION = re.search(r"# --- render-taskdef begin[^\n]*\n(.*?)# --- render-taskdef end ---", step("deploy", "Verify, render, register and run"), re.S).group(1)
REF = "r@" + DIGEST


def render(template: dict, image_ref: str = REF, commit: str = COMMIT, version: str = "", mode: str = "one-off",
           current: dict | None = None, allow_drop: str = ""):
    with tempfile.TemporaryDirectory() as d:
        Path(d, "t.json").write_text(json.dumps(template))
        if current is not None:
            Path(d, "current.json").write_text(json.dumps(current))
            mode = str(Path(d, "current.json"))
        stub = Path(d, "aws")
        stub.write_text(f"#!/bin/bash\ncat '{d}/t.json'\n")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        return subprocess.run(["bash", "-c", FUNCTION + f"\nrender api '{mode}'"], capture_output=True, text=True, env={
            **os.environ, "PATH": f"{d}:{os.environ['PATH']}", "NAME": "n", "IMAGE_REF": image_ref, "COMMIT": commit, "VERSION": version, "GITHUB_RUN_ID": "9",
            "ALLOW_DROP": allow_drop,
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
        self.assertEqual(doc["tags"], [{"key": "commit", "value": COMMIT}, {"key": "release-run", "value": "9"}])

    def test_tags_are_the_commit_and_run_and_a_version_only_when_promoting(self):
        tags = json.loads(render({"family": "f", "containerDefinitions": [{"name": "api"}]}, version="v1.2.3").stdout)["tags"]
        self.assertEqual(tags, [{"key": "commit", "value": COMMIT}, {"key": "release-run", "value": "9"}, {"key": "version", "value": "v1.2.3"}])

    def test_no_commit_refuses(self):
        out = render({"family": "f", "containerDefinitions": [{"name": "api"}]}, commit="")
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(out.stdout, "")

    def test_release_steps_never_pass_a_version_tag_in_preprod(self):
        deploy = RELEASE["jobs"]["deploy"]
        renders = [st for st in deploy["steps"] if "render-taskdef begin" in st.get("run", "")]
        self.assertEqual(len(renders), 3)  # migrate, the rollout, ops
        for st in renders:
            self.assertEqual(st["env"]["VERSION"], "", st["name"])
            self.assertNotIn("COMMIT", st["env"], st["name"])  # the job's, the one the build recorded
            self.assertNotIn("IMAGE_REF", st["env"], st["name"])
        self.assertEqual(deploy["env"]["COMMIT"], "${{ needs.build-image.outputs.commit || needs.build-web.outputs.commit }}")
        self.assertEqual(deploy["env"]["IMAGE_REF"], "${{ needs.build-image.outputs.image-ref || needs.build-web.outputs.image-ref }}")

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


def env(*names):
    return [{"name": n, "value": "x"} for n in names]


def secrets(*names):
    return [{"name": n, "valueFrom": "arn:ssm:" + n} for n in names]


RUNNING = {"family": "f", "containerDefinitions": [{"name": "api", "image": "old", "environment": env("A", "B"), "secrets": secrets("S")}]}


class DropProtection(unittest.TestCase):
    """delivery-pipelines §5 step 4: per service, against the service's currently registered task definition."""

    def test_a_setting_or_secret_that_would_disappear_is_refused(self):
        for c in ({"name": "api", "environment": env("A"), "secrets": secrets("S")}, {"name": "api", "environment": env("A", "B")}):
            out = render({"family": "f", "containerDefinitions": [c]}, current=RUNNING)
            self.assertNotEqual(out.returncode, 0, c)
            self.assertIn("drop protection", out.stderr)
            self.assertEqual(out.stdout, "")

    def test_a_dropped_name_listed_in_allow_drop_passes(self):
        out = render({"family": "f", "containerDefinitions": [{"name": "api", "environment": env("A")}]}, current=RUNNING, allow_drop="B, S")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("B", out.stderr)

    def test_allow_drop_allows_only_the_names_it_lists(self):
        out = render({"family": "f", "containerDefinitions": [{"name": "api", "environment": env("A")}]}, current=RUNNING, allow_drop="B")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("api/S", out.stderr)

    def test_a_name_moved_between_settings_and_secrets_is_not_a_drop(self):
        out = render({"family": "f", "containerDefinitions": [{"name": "api", "environment": env("A", "S"), "secrets": secrets("B")}]}, current=RUNNING)
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_a_name_in_both_settings_and_secrets_is_refused_even_on_a_first_deploy(self):
        both = {"family": "f", "containerDefinitions": [{"name": "api", "environment": env("A", "S"), "secrets": secrets("S")}]}
        for kwargs in ({"current": RUNNING}, {"mode": "first-deploy"}):
            out = render(both, **kwargs)
            self.assertNotEqual(out.returncode, 0, kwargs)
            self.assertIn("both a setting and a secret", out.stderr)
        self.assertNotEqual(render(both, current=RUNNING, allow_drop="S").returncode, 0)

    def test_a_first_deploy_with_no_registered_revision_passes(self):
        out = render({"family": "f", "containerDefinitions": [{"name": "api", "environment": env("A")}]}, mode="first-deploy")
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_a_vanished_container_counts_as_dropping_its_names_that_no_container_keeps(self):
        out = render({"family": "f", "containerDefinitions": [{"name": "other", "environment": env("A", "B")}]}, current=RUNNING)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("api/S", out.stderr)
        self.assertNotIn("api/A", out.stderr)

    def test_names_moved_between_containers_of_the_service_are_not_a_drop(self):
        """First real backend release (run 42011154980): the registered pgbouncer was the placeholder, one container with the
        start-up variables; the real template carries them in the pgbouncer-config sidecar and none in pgbouncer."""
        names = ("AWS_REGION", "CLOUD", "HTTPS_PROXY", "NODE_LOCAL_ADDRESS", "NO_PROXY", "ONEPEEK_CONFIG_PATH", "ONEPEEK_ENV", "ONEPEEK_SERVICE", "ONEPEEK_SERVICES_FILE")
        placeholder = {"family": "f", "containerDefinitions": [{"name": "pgbouncer", "image": "old", "environment": env(*names)}]}
        real = {"family": "f", "containerDefinitions": [{"name": "pgbouncer-config", "environment": env(*names)}, {"name": "pgbouncer", "image": "main"}]}
        out = render(real, current=placeholder)
        self.assertEqual(out.returncode, 0, out.stderr)
        gone = {"family": "f", "containerDefinitions": [{"name": "pgbouncer-config", "environment": env(*names[1:])}, {"name": "pgbouncer", "image": "main"}]}
        out = render(gone, current=placeholder)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("pgbouncer/AWS_REGION", out.stderr)
        self.assertEqual(render(gone, current=placeholder, allow_drop="AWS_REGION").returncode, 0)

    def test_one_offs_are_not_checked_and_an_unknown_mode_refuses(self):
        both = {"family": "f", "containerDefinitions": [{"name": "migrate", "environment": env("S"), "secrets": secrets("S")}]}
        self.assertEqual(render(both, mode="one-off").returncode, 0)
        self.assertNotEqual(render({"family": "f", "containerDefinitions": [{"name": "api"}]}, mode="/no/such/file").returncode, 0)

    def test_current_td_reads_the_services_registered_revision_or_reports_a_first_deploy(self):
        for described, want in (("arn:td/api:7", "current-api.json"), ("None", "first-deploy")):
            aws = f"""case "$*" in *describe-services*) echo {described};; *describe-task-definition*) echo '{json.dumps(RUNNING)}';; esac"""
            out, calls, _ = run_step(FUNCTION + "\ncurrent_td api current-api.json", aws)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(out.stdout.strip(), want)
            self.assertEqual("describe-task-definition --task-definition arn:td/api:7" in calls, want != "first-deploy")

    def test_an_aws_error_fails_closed_in_every_copy(self):
        for failing in ("describe-services", "describe-task-definition"):
            aws = f"""case "$*" in *{failing}*) echo "AccessDeniedException" >&2; exit 254;; *describe-services*) echo arn:td/api:7;; *describe-task-definition*) echo '{{}}';; esac"""
            out, _, _ = run_step(FUNCTION + "\ncurrent_td api current-api.json", aws)
            self.assertNotEqual(out.returncode, 0, failing)
            self.assertNotIn("first-deploy", out.stdout)
        for text in (TEXT, OPERATE):
            self.assertEqual(text.count("--output text) || return 1"), text.count("current_td() {"))
            self.assertEqual(text.count('--output json > "$2" || return 1'), text.count("current_td() {"))

    def test_an_aws_error_in_the_rollout_registers_nothing(self):
        aws = """case "$*" in *describe-services*) echo "ThrottlingException" >&2; exit 254;; *"s3 cp"*) echo '{"family": "f", "containerDefinitions": [{"name": "api"}]}';; *register-task-definition*) echo arn:td/api:8;; esac"""
        out, calls, _ = run_step(step("deploy", "Verify, render, register and update each order"), aws,
                                 {"IMAGE_REF": REF, "SIGNING_KEY": "k", "DRY_RUN": "false", "GITHUB_RUN_ID": "9", "ORDERS": '[["api"]]'})
        self.assertNotEqual(out.returncode, 0)
        self.assertNotIn("register-task-definition", calls)

    def test_a_drop_in_the_rollout_stops_before_anything_is_registered(self):
        aws = f"""case "$*" in
*describe-services*) echo arn:td/api:7;;
*describe-task-definition*) echo '{json.dumps(RUNNING)}';;
*"s3 cp"*) echo '{json.dumps({"family": "f", "containerDefinitions": [{"name": "api", "environment": env("A")}]})}';;
*register-task-definition*) echo arn:td/api:8;;
esac"""
        base = {"IMAGE_REF": REF, "SIGNING_KEY": "k", "DRY_RUN": "false", "GITHUB_RUN_ID": "9", "ORDERS": '[["api"]]'}
        script = step("deploy", "Verify, render, register and update each order")
        out, calls, _ = run_step(script, aws, base)
        self.assertNotEqual(out.returncode, 0)
        self.assertNotIn("register-task-definition", calls)
        out, calls, _ = run_step(script, aws, {**base, "ALLOW_DROP": "B S"})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("register-task-definition", calls)

    def test_the_inputs_reach_the_render(self):
        self.assertEqual(RELEASE[True]["workflow_call"]["inputs"]["allow-drop"]["default"], "")
        roll = next(s for s in RELEASE["jobs"]["deploy"]["steps"] if s.get("id") == "roll")
        self.assertEqual(roll["env"]["ALLOW_DROP"], "${{ inputs.allow-drop }}")
        self.assertIn('render migrate one-off', TEXT)
        self.assertIn('render ops one-off', TEXT)
        self.assertIn('mode=$(current_td "${svc}" "current-${svc}.json")', OPERATE)


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
    out, _, output = run_step(step("deploy", "The families this image rolls"), aws, {"IMAGE": image})
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
        out, calls, _ = run_step(step("deploy", "Verify, render, register and update each order"), aws, {
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
        out, calls, _ = run_step(step("deploy", "Verify, render, register and update each order"), aws, {
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
        out, calls, _ = run_step(step("deploy", "Verify, render, register and update each order"), aws, {
            "IMAGE_REF": REF, "SIGNING_KEY": "k", "DRY_RUN": "true", "GITHUB_RUN_ID": "9", "ORDERS": '[["pgbouncer"], ["api"]]'})  # fmt: skip
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("rendered 2 task definitions", out.stdout)
        self.assertNotIn("register-task-definition", calls)
        self.assertNotIn("update-service", calls)


class BaselineReadiness(unittest.TestCase):
    """UC-DEL-001 row 7: the shared readiness check always runs, whatever the caller's smoke script does."""
    script = step("verify", "Baseline readiness")

    def test_each_image_checks_its_path_and_a_failure_fails_the_job(self):
        for image, path in (("backend", "/api/status"), ("web", "/sign-in")):
            out, calls, _ = run_step(self.script, "", {"DOMAIN": "d", "IMAGE": image})
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("https://d" + path, calls)
            out, _, _ = run_step(self.script, "", {"DOMAIN": "d", "IMAGE": image}, extra_stubs={"curl": "exit 22"})
            self.assertEqual(out.returncode, 22)

    def test_it_is_not_conditional(self):
        st = next(s for s in RELEASE["jobs"]["verify"]["steps"] if s.get("name", "").startswith("Baseline readiness"))
        self.assertNotIn("if", st)


class Smoke(unittest.TestCase):
    def smoke(self, services: str, images: list[str], svc_td: str = "arn:td/x:2"):
        aws = f"""case "$*" in
*describe-services*) echo {svc_td};;
*describe-task-definition*) echo '{json.dumps(images)}';;
esac"""
        return run_step(step("verify", "Every service's task definition"), aws, {"DOMAIN": "d", "IMAGE": "backend", "DIGEST": DIGEST, "SERVICES": services})[0]

    def test_a_daemon_passes_when_its_sidecar_runs_the_digest_whatever_the_order(self):
        sidecar, main = f"x/backend@{DIGEST}", "x/pgbouncer@sha256:" + "e" * 64
        for images in ([sidecar, main], [main, sidecar]):
            out = self.smoke('["pgbouncer"]', images)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_a_service_on_another_digest_fails(self):
        self.assertEqual(self.smoke('["api"]', ["x/backend@sha256:" + "c" * 64]).returncode, 1)

    def test_a_digest_only_in_the_middle_of_an_image_name_fails(self):
        self.assertEqual(self.smoke('["api"]', [f"x/backend@{DIGEST}-other"]).returncode, 1)


class SmokeScript(unittest.TestCase):
    """UC-DEL-001 row 7: the caller's smoke-script checks the domain without AWS credentials; absent = a warning."""
    script = step("verify", "The repo's own smoke checks")

    def run_smoke(self, path: str, files: dict[str, str] | None = None):
        out, _, _, summary, _ = run_step(self.script, "", {"SCRIPT": path, "DOMAIN": "preprod.example", "AWS_SESSION_TOKEN": "secret-token"}, files, with_summary=True)
        return out, summary

    def test_it_runs_after_the_shared_checks_in_the_same_job(self):
        names = [s.get("name", "") for s in RELEASE["jobs"]["verify"]["steps"]]
        self.assertTrue(any(n.startswith("Every service's task definition") for n in names))
        self.assertTrue(any(n.startswith("The repo's own") for n in names))
        self.assertLess(names.index(next(n for n in names if n.startswith("Baseline readiness"))), names.index(next(n for n in names if n.startswith("The repo's own"))))
        self.assertEqual(RELEASE[True]["workflow_call"]["inputs"]["smoke-script"]["default"], "smoke.sh")

    def test_an_absent_script_is_skipped_with_a_visible_warning(self):
        out, summary = self.run_smoke("smoke.sh")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("::warning::smoke.sh is not in this repository", out.stdout)
        self.assertIn("no smoke.sh", summary)

    def test_a_failing_script_fails_the_smoke_and_so_rolls_back(self):
        self.assertEqual(self.run_smoke("smoke.sh", {"smoke.sh": "exit 3\n"})[0].returncode, 3)
        steps = RELEASE["jobs"]["verify"]["steps"]
        ids = [s.get("id") or s.get("name", "") for s in steps]
        self.assertLess(ids.index(next(i for i in ids if i.startswith("The repo's own"))), ids.index("watch"))
        self.assertIn("steps.watch.outcome != 'success'", steps[ids.index("rollback")]["if"])

    def test_a_passing_script_gets_the_domain_and_no_aws_credentials(self):
        out, _ = self.run_smoke("smoke.sh", {"smoke.sh": 'echo "domain=${DOMAIN} token=${AWS_SESSION_TOKEN:-none}"\n'})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("domain=preprod.example token=none", out.stdout)

    def test_a_path_outside_the_repository_refuses(self):
        for path in ("../x.sh", "/etc/x.sh", ""):
            self.assertEqual(self.run_smoke(path)[0].returncode, 1, path)


# The watch step uses bash 4+ (`mapfile`), as every CI runner has; macOS ships bash 3.2, so local_ci.py (§8a P5) skips
# these on such a laptop. CI and a Linux container always run them.
BASH4 = subprocess.run(["bash", "-c", "mapfile -t x < /dev/null"], capture_output=True).returncode == 0


@unittest.skipUnless(BASH4 or os.environ.get("CI") == "true" and os.environ.get("GITHUB_ACTIONS") == "true",
                     "needs bash 4+ (mapfile); this bash is older")
class Watch(unittest.TestCase):
    def watch(self, known: str, firing: str, seconds: str, services: str = '["api", "worker"]'):
        script = step("verify", "10 minutes after stable").replace("SECONDS + 600", "SECONDS + " + seconds)
        # The stub answers like AWS: only the alarms among the requested --alarm-names that exist (or are firing).
        aws = f"""want=" $* "
pick() {{ for a in $1; do case "$want" in *" $a "*) printf '%s\\t' "$a";; esac; done; echo; }}
case "$*" in
*"--state-value ALARM"*) pick "{firing}";;
*describe-alarms*) pick "{known}";;
esac"""
        out, calls, _, summary, _ = run_step(script, aws, extra_env={"SERVICES": services}, extra_stubs={"sleep": "exit 0"}, with_summary=True)
        return out, calls, summary

    def test_no_deploy_alarm_at_all_is_a_visible_warning_not_a_silent_pass(self):
        out, _, summary = self.watch("", "", "0")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("::warning::None of the deploy alarms", out.stdout)
        self.assertIn("could not detect a regression", summary)

    def test_it_asks_for_exactly_the_rolled_services_two_alarms(self):
        _, calls, _ = self.watch("", "", "0")
        self.assertIn("--alarm-names onepeek-preprod-deploy-api-errors onepeek-preprod-deploy-api-latency "
                      "onepeek-preprod-deploy-worker-errors onepeek-preprod-deploy-worker-latency", calls)
        self.assertNotIn("--alarm-name-prefix", calls)

    def test_a_firing_alarm_fails_the_watch(self):
        out, _, _ = self.watch("onepeek-preprod-deploy-api-errors", "onepeek-preprod-deploy-api-errors", "5")
        self.assertEqual(out.returncode, 1)
        self.assertIn("Deploy alarms firing: onepeek-preprod-deploy-api-errors", out.stdout)

    def test_another_services_alarm_does_not_fail_this_release(self):
        # api-critical starts with api-: a prefix watch would have caught it; exact names do not.
        other = "onepeek-preprod-deploy-api-critical-errors onepeek-preprod-deploy-web-latency"
        out, _, _ = self.watch("onepeek-preprod-deploy-api-errors " + other, other, "5", services='["api"]')
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_existing_quiet_alarms_pass_without_a_warning(self):
        out, _, summary = self.watch("onepeek-preprod-deploy-api-errors", "", "0")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertNotIn("warning", out.stdout)
        self.assertEqual(summary, "")

    def test_no_rolled_service_fails_instead_of_watching_nothing(self):
        out, _, _ = self.watch("", "", "0", services="[]")
        self.assertEqual(out.returncode, 1)
        self.assertIn("nothing to watch", out.stdout)

    def test_a_bad_service_name_is_refused(self):
        out, _, _ = self.watch("", "", "0", services='["api; rm -rf /"]')
        self.assertNotEqual(out.returncode, 0)


class Assets(unittest.TestCase):
    def test_the_folder_is_sha_12(self):
        aws = """case "$*" in *list-objects-v2*--prefix*) echo None;; esac"""
        out, calls, _ = run_step(step("deploy", "Copy /app/dist/assets"), aws, {"IMAGE_REF": REF, "REGISTRY": "r", "DRY_RUN": "false"})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn(f"--prefix {COMMIT}/", calls)
        self.assertIn(f"s3 sync ./assets-out s3://onepeek-preprod-web-assets/{COMMIT}/", calls)


class RollbackIssue(unittest.TestCase):
    def test_the_issue_title_names_the_commit(self):
        self.assertIn('--title "Release ${COMMIT} to ${ENVIRONMENT} rolled back"', step("verify", "Open an issue"))


class Notify(unittest.TestCase):
    script = step("finish", "Deployment record")

    def notify(self, watch: str, rollback: str, stable_at: str = "1700000300"):
        gh = 'case "$*" in *"/statuses"*) ;; *deployments*) echo 42;; esac'
        env = {"COMMIT": COMMIT, "NEXT_VERSION": "1.2.3", "DIGESTS": json.dumps({"backend": DIGEST}), "SERVICES": '["api", "worker"]',
               "WATCH": watch, "ROLLBACK": rollback, "STARTED": "1700000000", "STABLE_AT": stable_at, "MERGED_AT": "2023-11-14T22:13:20Z",
               "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "OnePeek/backend-service", "GITHUB_RUN_ID": "9", "GITHUB_SHA": "f" * 40}  # fmt: skip
        out, calls, _, summary, files = run_step(self.script, "", env, extra_stubs={"gh": gh}, with_summary=True)
        return out, calls, summary, {k: json.loads(v) for k, v in files.items() if v}

    def test_a_watched_release_records_a_successful_deployment_and_no_release(self):
        out, calls, summary, files = self.notify("success", "skipped")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertNotIn("release create", calls)
        self.assertIn("repos/OnePeek/backend-service/deployments --input deployment.json", calls)
        self.assertEqual(files["status.json"]["state"], "success")
        payload = files["deployment.json"]["payload"]
        self.assertEqual((payload["commit"], payload["digests"], payload["services"]), (COMMIT, {"backend": DIGEST}, ["api", "worker"]))
        self.assertEqual(files["deployment.json"]["environment"], "preprod")
        self.assertIn(f"Release {COMMIT} to preprod: success.", summary)

    def test_the_summary_has_the_run_duration_and_merge_to_running_time(self):
        _, _, summary, files = self.notify("success", "skipped")
        self.assertIn("- merge_to_running: 5m 00s", summary)  # merged at 1700000000 (22:13:20Z), stable at 1700000300
        self.assertIn("- duration: ", summary)
        self.assertEqual(files["deployment.json"]["payload"]["merge_to_running"], "5m 00s")
        self.assertIn('- services: ["api", "worker"]', summary)

    def test_a_failed_release_is_recorded_and_announced_as_a_failure(self):
        out, _, summary, files = self.notify("failure", "success", stable_at="")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(files["status.json"]["state"], "failure")
        self.assertIn("::error::Release " + COMMIT + " to preprod: failure, rolled back.", out.stdout)
        self.assertIn("failure, rolled back", summary)
        self.assertIn("not running", summary)

    def test_it_runs_after_any_result_but_never_in_a_dry_run(self):
        job = RELEASE["jobs"]["finish"]
        st = next(s for s in job["steps"] if s.get("name", "").startswith("Deployment record"))
        self.assertIn("always()", job["if"])
        self.assertIn("always()", st["if"])
        self.assertIn("!inputs.dry-run", st["if"])
        self.assertIn("needs.prepare.outputs.commit != ''", st["if"])  # after every run that got past the version
        self.assertIn("verify", job["needs"])
        self.assertEqual(st["env"]["WATCH"], "${{ needs.verify.outputs.watch }}")
        self.assertEqual(st["env"]["ROLLBACK"], "${{ needs.verify.outputs.rollback }}")
        self.assertEqual(RELEASE["jobs"]["verify"]["outputs"], {"watch": "${{ steps.watch.outcome }}", "rollback": "${{ steps.rollback.outcome }}"})
        # issues: write for the put-back's issue (playbook §5.4a "Rollback on cancel").
        self.assertEqual(job["permissions"], {"contents": "read", "id-token": "write", "deployments": "write", "issues": "write"})


class ApiClient(unittest.TestCase):
    def test_preprod_publishes_the_dev_prerelease_under_next(self):
        script = step("api-client", "Publish the prerelease under next")
        npm = 'case "$*" in *"pkg get name"*) echo \'"@onepeek/api-client"\';; *view*) exit 1;; esac'
        out, calls, _ = run_step(script, "", {"VERSION": "1.2.3", "GITHUB_SHA": "abcdef1234567890"}, extra_stubs={"npm": npm, "pnpm": ""})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("npm pkg set version=1.2.3-dev.abcdef1", calls)
        self.assertIn("pnpm publish --tag next", calls)
        self.assertNotIn("latest", calls)

    def test_a_rerun_of_a_published_commit_is_a_no_op(self):
        script = step("api-client", "Publish the prerelease under next")
        npm = 'case "$*" in *"pkg get name"*) echo \'"@onepeek/api-client"\';; *view*) echo 1.2.3-dev.abcdef1;; esac'
        out, calls, _ = run_step(script, "", {"VERSION": "1.2.3", "GITHUB_SHA": "abcdef1234567890"}, extra_stubs={"npm": npm, "pnpm": ""})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("npm view @onepeek/api-client@1.2.3-dev.abcdef1 version", calls)
        self.assertNotIn("pnpm publish", calls)

    def test_it_is_its_own_job_after_finish_for_a_clean_backend_watch_only(self):
        job = RELEASE["jobs"]["api-client"]
        self.assertEqual(needs_of("api-client"), ["prepare", "verify", "finish"])
        for part in ("!cancelled()", "!inputs.dry-run", "inputs.image == 'backend'", "needs.verify.outputs.watch == 'success'"):
            self.assertIn(part, job["if"])
        self.assertEqual(job["permissions"], {"contents": "read", "packages": "write"})
        self.assertEqual(job["defaults"]["run"]["working-directory"], "clients/typescript")
        # Nothing of the publish is left in finish, which has no packages scope.
        self.assertFalse([s for s in RELEASE["jobs"]["finish"]["steps"] if "pnpm" in str(s)])


PREVIOUS = re.search(r"(previous_deployment\(\) \{.*?\n          \})", OPERATE, re.S).group(1)


class OperateRollbackTarget(unittest.TestCase):
    """Playbook §5.2: a preprod rollback goes to the previous recorded deployment's digest, keyed on the commit."""

    def previous(self, rows: list[tuple[str, str, str]], states: dict[str, str], current: str = COMMIT):
        listing = "\\n".join("\\t".join(r) for r in rows)
        cases = "".join(f'*"deployments/{i}/statuses"*) echo {s};;\n' for i, s in states.items())
        gh = f"""case "$*" in
{cases}*"deployments?environment=preprod"*) printf '{listing}\\n';;
esac"""
        out, calls, _ = run_step(PREVIOUS + f'\nprevious_deployment backend {current}', "", {"GITHUB_REPOSITORY": "OnePeek/backend-service"}, extra_stubs={"gh": gh})
        return out, calls

    def test_the_first_successful_record_older_than_the_running_one_wins(self):
        rows = [("3", COMMIT, DIGEST), ("2", "sha-" + "2" * 12, "sha256:" + "2" * 64), ("1", "sha-" + "1" * 12, "sha256:" + "1" * 64)]
        out, _ = self.previous(rows, {"2": "failure", "1": "success"})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(out.stdout.strip(), "sha-" + "1" * 12 + " sha256:" + "1" * 64)

    def test_an_older_record_marked_inactive_by_github_is_still_a_target(self):
        rows = [("3", COMMIT, DIGEST), ("2", "sha-" + "2" * 12, "sha256:" + "2" * 64)]
        out, _ = self.previous(rows, {"3": "success", "2": "inactive"})
        self.assertEqual(out.stdout.strip(), "sha-" + "2" * 12 + " sha256:" + "2" * 64)

    def test_a_newer_release_than_the_running_one_is_never_a_target(self):
        # A newer release (4) failed and was rolled back, so the services run 3: the target is 2, never 4.
        rows = [("4", "sha-" + "4" * 12, "sha256:" + "4" * 64), ("3", COMMIT, DIGEST), ("2", "sha-" + "2" * 12, "sha256:" + "2" * 64)]
        out, _ = self.previous(rows, {"4": "success", "2": "success"})
        self.assertEqual(out.stdout.strip(), "sha-" + "2" * 12 + " sha256:" + "2" * 64)

    def test_no_record_of_the_running_commit_means_no_target(self):
        rows = [("2", "sha-" + "2" * 12, "sha256:" + "2" * 64), ("1", "sha-" + "1" * 12, "sha256:" + "1" * 64)]
        out, _ = self.previous(rows, {"2": "success", "1": "success"})
        self.assertEqual(out.stdout.strip(), "")

    def test_nothing_is_printed_when_there_is_no_earlier_record(self):
        out, _ = self.previous([("3", COMMIT, DIGEST)], {"3": "success"})
        self.assertEqual(out.stdout.strip(), "")

    def test_preprod_current_is_the_running_commit_tag_and_prod_the_version(self):
        self.assertIn("--include TAGS --query \"tags[?key=='commit'].value | [0]\"", OPERATE)
        self.assertIn('/onepeek/${ENVIRONMENT}/release/${image}/version', OPERATE)
        self.assertIn("'^sha-[0-9a-f]{12}$'", OPERATE)
        self.assertIn('--image-ids "imageTag=${target}"', OPERATE)


class DbBootstrapRoleGuard(unittest.TestCase):
    def guard(self, role: str, migrate: str = "true", dry: str = "false"):
        return run_step(step("prepare", "Refuse at once"), "", {"MIGRATE": migrate, "DRY_RUN": dry, "ROLE": role, "ENVIRONMENT_NAME": "preprod"})[0]

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
        out, calls, output = run_step(step("deploy", "The services file's services for this image"), aws, {"IMAGE": image})
        return out, calls, dict(line.split("=", 1) for line in output.splitlines())

    def test_e7_reads_the_published_copy_in_the_deploy_bucket(self):
        out, calls, output = self.services("backend")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("aws s3 cp s3://onepeek-preprod-deploy/services.preprod.yaml services.yaml", calls)
        self.assertEqual(json.loads(output["services"]), ["api", "worker"])
        self.assertEqual(json.loads(self.services("web")[2]["services"]), ["web"])

    def test_the_gateway_routes_read_the_same_file(self):
        self.assertIn("app api routes --env \"${ENVIRONMENT}\" --services-file services.yaml", step("deploy", "The gateway routes"))

    def test_no_infra_pin_and_no_infra_checkout_in_the_rollout(self):
        self.assertNotIn("infra-version.txt", TEXT)
        steps = RELEASE["jobs"]["deploy"]["steps"]
        self.assertFalse([s for s in steps if "onepeek-infra" in str(s.get("with", {}).get("repository", ""))])
        self.assertNotIn("_infra", json.dumps(steps))



class CosignRegistryLogin(unittest.TestCase):
    """Every cosign verify runs after an ECR login that is unconditional within its job (first live release: 401)."""

    @staticmethod
    def _code_lines(run: str) -> list[str]:
        return [ln.strip() for ln in run.splitlines() if ln.strip() and not ln.strip().startswith(("#", ": "))]

    def test_every_cosign_verify_comes_after_an_unconditional_ecr_login_in_its_job(self):
        import yaml  # noqa: PLC0415
        login_re = re.compile(r"^aws ecr get-login-password \| (cosign|docker) login\b")
        root = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows"
        for name in ("release-ecs.yml", "operate.yml"):
            wf = yaml.safe_load((root / name).read_text(encoding="utf-8"))
            for job_id, job in wf["jobs"].items():
                job_login = False  # an earlier, unconditional step logged in
                for st in job.get("steps", []):
                    step_login = False  # a login earlier in this same step
                    for line in self._code_lines(str(st.get("run", ""))):
                        if login_re.match(line):
                            step_login = True
                        if line.startswith("cosign verify"):
                            self.assertTrue(job_login or step_login,
                                            f"{name}:{job_id}: cosign verify without an ECR login before it")
                    if step_login and st.get("if") is None:
                        job_login = True


class SkippedBuildBranch(unittest.TestCase):
    """One of build-image and build-web is always skipped; every job below them must still run (v1.12.5 web release:
    scan and sbom-sign were skipped, so no digest was recorded)."""

    STATUS = re.compile(r"always\(\)|cancelled\(\)|failure\(\)|success\(\)")

    def test_every_job_below_the_split_build_calls_a_status_function(self):
        root = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows"
        jobs = yaml.safe_load((root / "release-ecs.yml").read_text(encoding="utf-8"))["jobs"]

        def ancestors(job_id: str) -> set[str]:
            found: set[str] = set()
            todo = [job_id]
            while todo:
                needs = jobs[todo.pop()].get("needs", [])
                for need in [needs] if isinstance(needs, str) else needs:
                    if need not in found:
                        found.add(need)
                        todo.append(need)
            return found

        below = [j for j in jobs if ancestors(j) & {"build-image", "build-web"}]
        self.assertEqual(below, ["db-bootstrap", "deploy", "verify", "finish", "api-client"])
        for job_id in below:
            self.assertRegex(str(jobs[job_id].get("if", "")), self.STATUS,
                             f"release-ecs.yml:{job_id}: below a skipped build job, so it needs !cancelled() or always()")


class JobGraph(unittest.TestCase):
    """Foundation-reset W2.1: five stages, because each job waits about a minute for a runner (2026-11-05: 11.9 of 19.1
    minutes were waiting). db-bootstrap stays a job (a reusable-workflow call under its own role), the build is two
    exclusive jobs (only the web build's token reads packages) and the API client publish is its own job (only it holds
    packages: write), so 8 jobs are defined; the backend runs at most 7 and the web image at most 5."""

    JOBS = ["prepare", "build-image", "build-web", "db-bootstrap", "deploy", "verify", "finish", "api-client"]

    def test_the_job_count_is_pinned(self):
        self.assertEqual(list(RELEASE["jobs"]), self.JOBS)

    @staticmethod
    def _simulate(image: str, migrate: bool, dry_run: bool) -> list[str]:
        """Evaluates every job's needs and `if` the way Actions does, with every job that runs succeeding."""
        attempt = 1
        inputs = {"image": image, "migrate": migrate, "dry-run": dry_run, "environment": "preprod"}
        result: dict[str, str] = {}
        outputs = {"prepare": {"skip": "false", "attempt": str(attempt)}, "deploy": {"previous": ""},
                   "verify": {"watch": "success"}}

        def evaluate(expr: str) -> bool:
            e = expr.strip()
            e = e[3:-2] if e.startswith("${{") else e
            e = re.sub(r"contains\(needs\.\*\.result, '(\w+)'\)", lambda m: repr(m.group(1) in result.values()), e)
            e = re.sub(r"format\('\{0\}', github\.run_attempt\)", repr(str(attempt)), e)
            e = re.sub(r"needs\.([\w-]+)\.outputs\.(\w+)", lambda m: repr(outputs.get(m.group(1), {}).get(m.group(2), "")), e)
            e = re.sub(r"needs\.([\w-]+)\.result", lambda m: repr(result[m.group(1)]), e)
            e = re.sub(r"inputs\.([\w-]+)", lambda m: repr(inputs[m.group(1)]), e)
            e = e.replace("!cancelled()", "True").replace("always()", "True")
            e = e.replace("&&", " and ").replace("||", " or ")
            e = re.sub(r"!(?!=)", " not ", e)
            return bool(eval(e, {"__builtins__": {}}))  # noqa: S307  (the workflow's own expressions, in a test)

        ran = []
        for name, job in RELEASE["jobs"].items():
            if "if" in job:
                go = evaluate(job["if"])
            else:  # the implicit success(): every needed job succeeded
                go = all(result[n] == "success" for n in needs_of(name))
            result[name] = "success" if go else "skipped"
            if go:
                ran.append(name)
        return ran

    def test_the_jobs_that_run_per_image_come_from_evaluating_the_conditions(self):
        every = ["prepare", "build-image", "db-bootstrap", "deploy", "verify", "finish", "api-client"]
        self.assertEqual(self._simulate("backend", True, False), every)
        self.assertEqual(self._simulate("backend", False, False), [j for j in every if j != "db-bootstrap"])
        web = ["prepare", "build-web", "db-bootstrap", "deploy", "verify", "finish"]
        self.assertEqual(self._simulate("web", True, False), web)
        self.assertEqual(self._simulate("web", False, False), [j for j in web if j != "db-bootstrap"])
        # A dry run reads the digests back and deploys nothing live: no migrate, watch or client publish.
        self.assertEqual(self._simulate("backend", True, True), ["prepare", "build-image", "deploy", "finish"])
        self.assertEqual(self._simulate("web", True, True), ["prepare", "build-web", "deploy", "finish"])
        self.assertEqual(len(self._simulate("backend", True, False)), 7)
        self.assertEqual(len(self._simulate("web", False, False)), 5)

    def test_the_stages_run_in_order(self):
        self.assertEqual(needs_of("deploy"), ["prepare", "build-image", "build-web", "db-bootstrap"])
        self.assertEqual(needs_of("verify"), ["prepare", "build-image", "build-web", "deploy"])
        self.assertEqual(needs_of("finish"), ["prepare", "build-image", "build-web", "db-bootstrap", "deploy", "verify"])
        self.assertEqual(needs_of("api-client"), ["prepare", "verify", "finish"])

    def test_only_api_client_has_packages_write_and_build_web_keeps_packages_read(self):
        for name, job in RELEASE["jobs"].items():
            perms = job.get("permissions") or {}
            self.assertEqual(perms.get("packages") == "write", name == "api-client", name)
        self.assertEqual(RELEASE["jobs"]["build-web"]["permissions"].get("packages"), "read")
        self.assertNotIn("packages: write", str(RELEASE.get("permissions", "")))

    def test_no_registry_password_is_handled_for_the_scan(self):
        # Trivy authenticates to ECR from the AWS credentials the credentials step exported.
        for word in ("TRIVY_PASSWORD", "TRIVY_USERNAME", "scanner-secret", "add-mask"):
            # The release-lock functions mask the App token and role session they mint again inside a wait (playbook §5.4a).
            text = re.sub(r"# --- release-lock begin.*?# --- release-lock end ---", "", TEXT, flags=re.S)
            self.assertNotIn(word, text.replace("# No registry password: Trivy reads", ""), word)
        self.assertNotRegex(TEXT, r"(?m)^[^#\n]*>>\s*\"?\$GITHUB_ENV")

    def test_every_verify_step_before_the_rollback_has_a_limit_and_the_job_limit_exceeds_them_all(self):
        # A job timeout cancels the job, so the rollback would never run: a hang must end as a failed step.
        job = RELEASE["jobs"]["verify"]
        steps = job["steps"]
        rollback = next(i for i, s in enumerate(steps) if s.get("id") == "rollback")
        trivial = ("This run attempt holds", "The deploy job finished its rollout")
        for s in steps[:rollback]:
            if "run" in s and not s.get("name", "").startswith(trivial):
                self.assertIn("timeout-minutes", s, s.get("name"))
        smoke = next(s for s in steps if s.get("name", "").startswith("The repo's own smoke checks"))
        self.assertEqual(smoke["timeout-minutes"], 10)
        self.assertGreater(job["timeout-minutes"], sum(s.get("timeout-minutes", 0) for s in steps))
        self.assertGreaterEqual(job["timeout-minutes"], 55)

    def test_every_deploy_step_has_a_limit_and_the_job_limit_exceeds_them_all(self):
        # A job timeout ends deploy as cancelled, and verify (the rollback) runs only after a failed deploy.
        job = RELEASE["jobs"]["deploy"]
        for s in job["steps"]:
            if "run" in s and not s.get("name", "").startswith("This run attempt holds"):
                self.assertIn("timeout-minutes", s, s.get("name"))
        self.assertGreater(job["timeout-minutes"], sum(s.get("timeout-minutes", 0) for s in job["steps"]))

    def test_no_step_gating_a_change_runs_after_a_failure(self):
        # No always() or cancelled() in the build, deploy or verify steps before the rollback, so the lock-attempt
        # check (and every earlier step) gates every change.
        for name in ("build-image", "build-web", "deploy", "verify"):
            steps = RELEASE["jobs"][name]["steps"]
            if name == "verify":
                steps = steps[:next(i for i, s in enumerate(steps) if s.get("id") == "rollback")]
            for s in steps:
                self.assertNotRegex(str(s.get("if", "")), r"always\(\)|cancelled\(\)", f"{name}: {s.get('name') or s.get('uses')}")

    def test_e7_runs_for_the_backend_only(self):
        e7 = next(s for s in RELEASE["jobs"]["deploy"]["steps"] if s.get("name", "").startswith("E7."))
        self.assertEqual(e7["if"], "${{ inputs.image == 'backend' }}")

    def test_no_job_is_a_matrix(self):
        # Matrix outputs are last-writer-wins; the build records every image's digest in one step instead.
        for name, job in RELEASE["jobs"].items():
            self.assertNotIn("strategy", job, name)
        self.assertNotIn("matrix.", TEXT)

    def test_each_stage_keeps_the_steps_time_limits(self):
        # delivery-pipelines §5: build 20, migrate 15, rollout 20, watch 12, now on the steps of the merged jobs.
        def limit(job, match):
            return next(s for s in RELEASE["jobs"][job]["steps"] if match(s))["timeout-minutes"]
        self.assertEqual(limit("build-image", lambda s: s.get("id") == "push"), 20)
        self.assertEqual(limit("deploy", lambda s: s.get("name", "").startswith("Verify, render, register and run the migrate")), 15)
        self.assertEqual(limit("deploy", lambda s: s.get("id") == "roll"), 20)
        self.assertEqual(limit("verify", lambda s: s.get("id") == "watch"), 12)
        for name, job in RELEASE["jobs"].items():
            if "steps" in job:
                total = sum(s.get("timeout-minutes", 0) for s in job["steps"])
                self.assertGreaterEqual(job["timeout-minutes"], total, name)

    def test_the_build_steps_are_in_stage_order_and_all_but_the_record_skip_a_dry_run(self):
        steps = RELEASE["jobs"]["build-image"]["steps"]
        names = [s.get("id") or s.get("name") or s.get("uses", "") for s in steps]
        order = ["push", "login", "waiver", "Trivy", "Syft", "record"]
        found = [next(i for i, n in enumerate(names) if n.startswith(o)) for o in order]
        self.assertEqual(found, sorted(found))
        record = steps[found[-1]]
        self.assertNotIn("if", record)
        for i in found[:-1]:
            self.assertIn("!inputs.dry-run", steps[i]["if"], names[i])

    def test_the_deploy_steps_are_in_stage_order(self):
        names = [s.get("name", "") for s in RELEASE["jobs"]["deploy"]["steps"]]
        order = ["This run attempt holds", "Verify, render, register and run the migrate", "Copy /app/dist/assets",
                 "E7.", "The gateway routes", "Record every service's current task definition",
                 "Verify, render, register and update each order", "Lambda sandbox", "Verify, render, check the launch settings, register ops"]
        found = [next(i for i, n in enumerate(names) if n.startswith(o)) for o in order]
        self.assertEqual(found, sorted(found))


class VerifyRollsBack(unittest.TestCase):
    """UC-DEL-001 row 9, E2, E3: a failed rollout, ops registration, smoke or watch puts every service back."""
    job = RELEASE["jobs"]["verify"]

    def test_it_runs_after_a_deploy_that_failed_once_the_rollout_had_started(self):
        self.assertEqual(self.job["if"], "${{ !cancelled() && !inputs.dry-run && (needs.deploy.result == 'success' || "
                                         "(needs.deploy.result == 'failure' && needs.deploy.outputs.previous != '')) }}")
        # previous is recorded right before the rollout: a failed migrate, assets or E7 never rolls back.
        names = [s.get("id") or s.get("name", "") for s in RELEASE["jobs"]["deploy"]["steps"]]
        self.assertEqual(names.index("previous") + 1, names.index("roll"))
        for before in ("Verify, render, register and run the migrate", "Copy /app/dist/assets", "E7."):
            self.assertLess(names.index(next(n for n in names if n.startswith(before))), names.index("previous"))
        self.assertEqual(RELEASE["jobs"]["deploy"]["outputs"]["previous"], "${{ steps.previous.outputs.previous }}")

    def test_a_failed_deploy_skips_smoke_and_watch(self):
        st = next(s for s in self.job["steps"] if s.get("name", "").startswith("The deploy job finished its rollout"))
        for result, code in (("success", 0), ("failure", 1)):
            out, _, _ = run_step(st["run"], "", {"DEPLOY_RESULT": result})
            self.assertEqual(out.returncode, code, result)
        names = [s.get("id") or s.get("name", "") for s in self.job["steps"]]
        check = names.index(st["name"])
        for later in ("Baseline readiness", "Every service's task definition", "The repo's own", "watch"):
            i = names.index(next(n for n in names if n.startswith(later)))
            self.assertGreater(i, check, later)
            self.assertNotIn("always()", str(self.job["steps"][i].get("if", "")), later)
            self.assertNotIn("cancelled()", str(self.job["steps"][i].get("if", "")), later)

    def test_the_rollback_runs_unless_the_watch_passed_and_never_after_a_failed_lock_check(self):
        steps = {s.get("id"): s for s in self.job["steps"] if s.get("id")}
        self.assertEqual(steps["rollback"]["if"], "${{ !cancelled() && steps.guard.outcome == 'success' && "
                                                  "steps.creds.outcome == 'success' && steps.watch.outcome != 'success' }}")
        self.assertEqual(steps["rollback"]["env"]["PREVIOUS"], "${{ needs.deploy.outputs.previous }}")
        names = [s.get("id") or s.get("name", "") for s in self.job["steps"]]
        self.assertEqual(names[-3:], ["rollback", "Open an issue naming the failure",
                                      "Record the end in the release lock (done after a clean watch, locked after a stable rollback)"])
        self.assertEqual(names[:2], ["guard", "creds"])
        issue = self.job["steps"][-2]
        self.assertEqual(issue["if"], "${{ !cancelled() && steps.rollback.outcome == 'success' }}")


if __name__ == "__main__":
    unittest.main()
