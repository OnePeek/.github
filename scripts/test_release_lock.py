"""The cross-repo release lock in release-ecs.yml and operate.yml (onepeek-docs architecture/lld/infra/foundation-playbook.md
§5.4; delivery-pipelines.md UC-DEL-001, UC-DEL-002, E5), run for real against a fake S3 and a stub `gh`.
Run: python -m unittest discover -s scripts -p 'test_*.py'"""

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"
RELEASE = yaml.safe_load((WORKFLOWS / "release-ecs.yml").read_text(encoding="utf-8"))
OPERATE = yaml.safe_load((WORKFLOWS / "operate.yml").read_text(encoding="utf-8"))
RELEASE_TEXT = (WORKFLOWS / "release-ecs.yml").read_text(encoding="utf-8")
OPERATE_TEXT = (WORKFLOWS / "operate.yml").read_text(encoding="utf-8")
REPO, RUN = "OnePeek/backend-service", "5000"
ENV = {"NAME": "onepeek-preprod", "ENVIRONMENT": "preprod", "GITHUB_REPOSITORY": REPO, "GITHUB_RUN_ID": RUN,
       "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "f" * 40, "GITHUB_SERVER_URL": "https://github.com",
       "LOCK_WAIT_SECONDS": "2", "LOCK_POLL_SECONDS": "1", "LOCK_GH_TOKEN": "t"}  # fmt: skip

# A fake S3 holding one object, with the conditional-write semantics the lock relies on: If-None-Match: * refuses an
# existing key, If-Match refuses another ETag, a missing key is NoSuchKey. FAIL_<OP> injects any other error, and
# RACE_ON_DELETE has another run take the lock just before a delete.
FAKE_AWS = r'''#!/usr/bin/env python3
import hashlib, json, os, shutil, sys
state = os.environ["FAKE_S3"]
args = sys.argv[1:]
with open(os.path.join(state, "calls"), "a") as f:
    f.write("aws " + " ".join(args) + "\n")
obj, tag = os.path.join(state, "object"), os.path.join(state, "etag")
def fail(op, message):
    sys.stderr.write("An error occurred (%s) when calling the %s operation: x\n" % (message, op))
    sys.exit(254)
def opt(name):
    return args[args.index(name) + 1] if name in args else None
op = args[1] if len(args) > 1 else ""
injected = os.environ.get("FAIL_" + op.replace("-", "_").upper())
if injected:
    fail(op, injected)
if op == "put-object":
    if opt("--if-none-match") == "*" and os.path.exists(obj):
        fail("PutObject", "PreconditionFailed")
    shutil.copy(opt("--body"), obj)
    etag = '"%s"' % hashlib.md5(open(obj, "rb").read()).hexdigest()
    open(tag, "w").write(etag)
    print(json.dumps({"ETag": etag}))
elif op == "get-object":
    if not os.path.exists(obj):
        fail("GetObject", "NoSuchKey")
    shutil.copy(obj, args[args.index("--key") + 2])
    print(open(tag).read())
elif op == "delete-object":
    race = os.environ.get("RACE_ON_DELETE")
    if race:
        open(obj, "w").write(race)
        open(tag, "w").write('"raced"')
    if not os.path.exists(obj):
        fail("DeleteObject", "NoSuchKey")
    if opt("--if-match") != open(tag).read():
        fail("DeleteObject", "PreconditionFailed")
    os.remove(obj)
    os.remove(tag)
else:
    fail(op, "UnexpectedCall")
'''


def record(repo: str = "OnePeek/onepeek-infra", run: str = "77", attempt: str = "1") -> str:
    return json.dumps({"repo": repo, "run_id": run, "run_attempt": attempt, "commit": "sha-" + "1" * 12, "started": "2026-11-03T10:00:00+00:00"})


def job_step(doc: dict, job: str, name: str) -> str:
    return next(s["run"] for s in doc["jobs"][job]["steps"] if s.get("name", "").startswith(name))


TAKE = job_step(RELEASE, "lock", "Take the release lock")
RELEASE_STEP = job_step(RELEASE, "unlock", "Release the release lock")


def run(script: str, held: str | None = None, gh: str = 'echo \'{"status":"in_progress"}\' >/dev/null; echo in_progress', **extra: str):
    """Returns (result, calls, the object left in S3 or None, GITHUB_OUTPUT)."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        aws = root / "aws"
        aws.write_text(FAKE_AWS)
        ghs = root / "gh"
        ghs.write_text(f'#!/bin/bash\necho "gh $*" >> "{root}/calls"\n{gh}\n')
        for f in (aws, ghs):
            f.chmod(f.stat().st_mode | stat.S_IEXEC)
        if held is not None:
            (root / "object").write_text(held)
            (root / "etag").write_text('"held-etag"')
        out = subprocess.run(["bash", "-eo", "pipefail", "-c", script], cwd=root, capture_output=True, text=True, timeout=60, env={
            **os.environ, **ENV, **extra, "FAKE_S3": str(root), "PATH": f"{root}:{os.environ['PATH']}", "GITHUB_OUTPUT": str(root / "output"),
        })  # fmt: skip
        read = lambda n: (root / n).read_text() if (root / n).exists() else None
        return out, read("calls") or "", read("object"), read("output") or ""


class Take(unittest.TestCase):
    def test_a_free_lock_is_taken_with_a_conditional_write_naming_this_run(self):
        out, calls, obj, output = run(TAKE)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("aws s3api put-object --bucket onepeek-preprod-deploy --key locks/release.json --body lock-mine.json --if-none-match *", calls)
        mine = json.loads(obj)
        self.assertEqual({k: mine[k] for k in ("repo", "run_id", "run_attempt", "commit")},
                         {"repo": REPO, "run_id": RUN, "run_attempt": "1", "commit": "sha-" + "f" * 12})
        self.assertTrue(mine["started"].endswith("+00:00"))
        self.assertIn("attempt=1", output)

    def test_a_lock_held_by_a_running_release_waits_then_fails_naming_its_run_and_is_left_alone(self):
        out, calls, obj, output = run(TAKE, held=record(), LOCK_WAIT_SECONDS="4")
        self.assertEqual(out.returncode, 1)
        self.assertIn("still held after 4 s by https://github.com/OnePeek/onepeek-infra/actions/runs/77", out.stdout)
        self.assertGreaterEqual(out.stdout.count("Waiting for the preprod release lock"), 2)
        self.assertEqual(obj, record())
        self.assertNotIn("delete-object", calls)
        self.assertNotIn("attempt=", output)

    def test_a_lock_whose_run_has_finished_is_removed_on_its_etag_and_taken(self):
        out, calls, obj, _ = run(TAKE, held=record(), gh="echo completed")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("gh api repos/OnePeek/onepeek-infra/actions/runs/77 --jq .status", calls)
        self.assertIn('delete-object --bucket onepeek-preprod-deploy --key locks/release.json --if-match "held-etag"', calls)
        self.assertEqual(json.loads(obj)["run_id"], RUN)

    def test_an_earlier_attempt_of_this_run_is_taken_over_without_asking_github(self):
        out, calls, obj, _ = run(TAKE, held=record(REPO, RUN, "1"), GITHUB_RUN_ATTEMPT="2")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertNotIn("gh api", calls)
        self.assertEqual(json.loads(obj)["run_attempt"], "2")

    def test_the_same_run_id_in_another_repo_is_not_ours(self):
        out, _, obj, _ = run(TAKE, held=record("OnePeek/onepeek-web", RUN))
        self.assertEqual(out.returncode, 1)
        self.assertEqual(obj, record("OnePeek/onepeek-web", RUN))

    def test_a_holder_whose_state_cannot_be_read_counts_as_running(self):
        # Fail closed: without the GitHub App token another repo's run is unreadable, and its lock is never removed.
        out, calls, obj, _ = run(TAKE, held=record(), gh='echo "HTTP 404" >&2; exit 1')
        self.assertEqual(out.returncode, 1)
        self.assertIn("counts as running", out.stdout)
        self.assertNotIn("delete-object", calls)
        self.assertEqual(obj, record())

    def test_an_unreadable_lock_means_no_deploy(self):
        out, calls, obj, _ = run(TAKE, held=record(), FAIL_GET_OBJECT="AccessDenied")
        self.assertEqual(out.returncode, 1)
        self.assertIn("unreadable, so nothing is deployed", out.stdout)
        self.assertNotIn("delete-object", calls)
        self.assertEqual(obj, record())

    def test_a_record_that_is_not_a_lock_means_no_deploy_and_is_left_in_place(self):
        for junk in ("not json", json.dumps({"repo": "x"}), record(repo="../../evil")):
            out, calls, obj, _ = run(TAKE, held=junk, gh="echo completed")
            self.assertEqual(out.returncode, 1, junk)
            self.assertNotIn("gh api", calls)
            self.assertNotIn("delete-object", calls)
            self.assertEqual(obj, junk)

    def test_any_other_write_error_means_no_deploy(self):
        out, _, obj, _ = run(TAKE, FAIL_PUT_OBJECT="AccessDenied")
        self.assertEqual(out.returncode, 1)
        self.assertIn("could not be written, so nothing is deployed", out.stdout)
        self.assertIsNone(obj)

    def test_a_concurrent_write_conflict_is_read_again_not_fatal(self):
        out, calls, obj, _ = run(TAKE, held=record(), FAIL_PUT_OBJECT="ConditionalRequestConflict")
        self.assertEqual(out.returncode, 1)
        self.assertIn("still held", out.stdout)
        self.assertIn("get-object", calls)

    def test_another_run_taking_the_lock_during_a_takeover_is_respected(self):
        other = record("OnePeek/onepeek-web", "88")
        out, calls, obj, _ = run(TAKE, held=record(), gh='case "$*" in *runs/77*) echo completed;; *) echo in_progress;; esac', RACE_ON_DELETE=other)
        self.assertEqual(out.returncode, 1)
        self.assertEqual(obj, other)
        self.assertIn("still held after 2 s by https://github.com/OnePeek/onepeek-web/actions/runs/88", out.stdout)

    def test_a_failed_delete_means_no_deploy(self):
        out, _, obj, _ = run(TAKE, held=record(), gh="echo completed", FAIL_DELETE_OBJECT="AccessDenied")
        self.assertEqual(out.returncode, 1)
        self.assertIn("could not be removed", out.stdout)
        self.assertEqual(obj, record())


class Release(unittest.TestCase):
    def test_this_runs_lock_is_removed_on_its_etag(self):
        out, calls, obj, _ = run(RELEASE_STEP, held=record(REPO, RUN))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn('delete-object --bucket onepeek-preprod-deploy --key locks/release.json --if-match "held-etag"', calls)
        self.assertIsNone(obj)

    def test_any_attempt_of_this_run_releases_it(self):
        out, _, obj, _ = run(RELEASE_STEP, held=record(REPO, RUN, "1"), GITHUB_RUN_ATTEMPT="3")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIsNone(obj)

    def test_another_runs_lock_is_never_removed(self):
        for held in (record(), record(REPO, "5001"), record("OnePeek/onepeek-web", RUN)):
            out, calls, obj, _ = run(RELEASE_STEP, held=held)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            self.assertIn("not this run; it is left in place", out.stdout)
            self.assertNotIn("delete-object", calls)
            self.assertEqual(obj, held)

    def test_no_lock_is_fine(self):
        out, calls, _, _ = run(RELEASE_STEP)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertNotIn("delete-object", calls)

    def test_an_unreadable_lock_fails_visibly(self):
        out, calls, obj, _ = run(RELEASE_STEP, held=record(REPO, RUN), FAIL_GET_OBJECT="AccessDenied")
        self.assertEqual(out.returncode, 1)
        self.assertNotIn("delete-object", calls)
        self.assertEqual(obj, record(REPO, RUN))

    def test_a_lock_taken_over_meanwhile_is_left_in_place(self):
        other = record("OnePeek/onepeek-web", "88")
        out, _, obj, _ = run(RELEASE_STEP, held=record(REPO, RUN), RACE_ON_DELETE=other)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(obj, other)


def needs(doc: dict, job: str) -> list[str]:
    n = doc["jobs"][job].get("needs", [])
    return [n] if isinstance(n, str) else n


GUARDED = ["sbom-sign", "migrate", "assets", "rollout", "register-one-offs"]


class Wiring(unittest.TestCase):
    """The lock is taken by the first job, before any build, and released by an always() job (playbook §5.4)."""

    def test_release_ecs_takes_the_lock_before_anything_else(self):
        self.assertEqual(needs(RELEASE, "lock"), [])
        self.assertEqual(needs(RELEASE, "version"), ["lock"])
        for job in RELEASE["jobs"]:
            if job not in ("lock", "version"):
                self.assertTrue(needs(RELEASE, job), job)

    def test_unlock_always_runs_after_every_deploy_job(self):
        unlock = RELEASE["jobs"]["unlock"]
        self.assertEqual(unlock["if"], "${{ always() }}")
        self.assertEqual(set(needs(RELEASE, "unlock")), set(RELEASE["jobs"]) - {"unlock", "api-client"})
        self.assertIn("lock_release", job_step(RELEASE, "unlock", "Release the release lock"))

    def test_operate_takes_and_releases_the_lock_around_its_one_job(self):
        self.assertEqual(needs(OPERATE, "operate"), ["lock"])
        self.assertEqual(set(needs(OPERATE, "unlock")), {"lock", "operate"})
        self.assertTrue(OPERATE["jobs"]["unlock"]["if"].startswith("${{ always()"))
        self.assertIn("lock_take", job_step(OPERATE, "lock", "Take the release lock"))
        self.assertIn("lock_release", job_step(OPERATE, "unlock", "Release the release lock"))

    def test_no_concurrency_group_queues_a_release(self):
        # GitHub keeps one pending run per group and cancels the older one (playbook §5.1 step 7, checklist M).
        for doc in (RELEASE, OPERATE):
            self.assertNotIn("concurrency", doc)
            for name, job in doc["jobs"].items():
                self.assertNotIn("concurrency", job, name)

    def test_a_release_waits_30_minutes_polling_every_30_seconds(self):
        for doc in (RELEASE, OPERATE):
            self.assertEqual((doc["env"]["LOCK_WAIT_SECONDS"], doc["env"]["LOCK_POLL_SECONDS"]), ("1800", "30"))
            self.assertGreater(doc["jobs"]["lock"]["timeout-minutes"] * 60, 1800)

    def test_the_lock_is_per_environment_in_its_deploy_bucket(self):
        self.assertIn('LOCK_BUCKET="${NAME}-deploy"', TAKE)
        self.assertIn('LOCK_KEY="locks/release.json"', TAKE)

    def test_the_holders_state_is_read_with_the_app_token_when_there_is_one(self):
        for doc in (RELEASE, OPERATE):
            lock = doc["jobs"]["lock"]
            app = next(s for s in lock["steps"] if s.get("id") == "app")
            self.assertTrue(app["uses"].startswith("actions/create-github-app-token@64ab729a6294000f5aa4a4bc0b73d1f32bc45510"))
            self.assertEqual(app["with"]["permission-actions"], "read")
            take = next(s for s in lock["steps"] if s.get("id") == "take")
            self.assertEqual(take["env"]["LOCK_GH_TOKEN"], "${{ steps.app.outputs.token || github.token }}")
            self.assertEqual(lock["permissions"]["actions"], "read")

    def test_prod_is_promote_only(self):
        script = job_step(RELEASE, "lock", "Prod is promote-only")
        refused = subprocess.run(["bash", "-eo", "pipefail", "-c", script], capture_output=True, text=True, env={**os.environ, "ENVIRONMENT_NAME": "prod"})
        self.assertEqual(refused.returncode, 1)
        self.assertIn("promote.yml", refused.stdout)
        allowed = subprocess.run(["bash", "-eo", "pipefail", "-c", script], capture_output=True, text=True, env={**os.environ, "ENVIRONMENT_NAME": "preprod"})
        self.assertEqual(allowed.returncode, 0)

    def test_operate_runs_preprod_from_dev_and_prod_from_release(self):
        want = "${{ (inputs.environment == 'preprod' && github.ref == 'refs/heads/dev') || (inputs.environment == 'prod' && github.ref == 'refs/heads/release') }}"
        self.assertEqual(OPERATE["jobs"]["lock"]["if"], want)
        self.assertEqual(OPERATE["jobs"]["operate"]["if"], want)


class PartialReRun(unittest.TestCase):
    """A re-run of only the failed jobs does not re-run the lock job, so every job that changes anything checks that
    this attempt took the lock."""

    def guard(self, doc: dict, job: str) -> dict:
        return doc["jobs"][job]["steps"][0]

    def test_every_changing_job_checks_the_lock_first(self):
        for doc, jobs in ((RELEASE, GUARDED), (OPERATE, ["operate"])):
            for job in jobs:
                self.assertIn("lock", needs(doc, job), job)
                g = self.guard(doc, job)
                self.assertTrue(g["name"].startswith("This run attempt holds the release lock"), job)
                self.assertEqual(g["env"]["LOCKED_ATTEMPT"], "${{ needs.lock.outputs.attempt }}")

    def test_the_reusable_db_bootstrap_call_is_skipped_outside_the_locking_attempt(self):
        self.assertIn("needs.lock.outputs.attempt == format('{0}', github.run_attempt)", RELEASE["jobs"]["db-bootstrap"]["if"])
        self.assertIn("lock", needs(RELEASE, "db-bootstrap"))

    def test_the_check_fails_on_another_attempt_and_passes_on_the_locking_one(self):
        script = self.guard(RELEASE, "rollout")["run"]
        for locked, attempt, code in (("1", "1", 0), ("1", "2", 1), ("", "1", 1)):
            out = subprocess.run(["bash", "-eo", "pipefail", "-c", script], capture_output=True, text=True,
                                 env={**os.environ, "LOCKED_ATTEMPT": locked, "GITHUB_RUN_ATTEMPT": attempt})  # fmt: skip
            self.assertEqual(out.returncode, code, (locked, attempt))
        self.assertIn("Re-run all jobs", out.stdout)

    def test_the_guard_is_the_same_everywhere(self):
        runs = {self.guard(RELEASE, j)["run"] for j in GUARDED} | {self.guard(OPERATE, "operate")["run"]}
        self.assertEqual(len(runs), 1)


class SameLockEverywhere(unittest.TestCase):
    def test_operate_uses_the_same_lock_functions(self):
        self.assertEqual(TAKE, job_step(OPERATE, "lock", "Take the release lock"))
        self.assertEqual(RELEASE_STEP, job_step(OPERATE, "unlock", "Release the release lock"))
        self.assertIn("# --- release-lock begin", RELEASE_TEXT)
        self.assertIn("# --- release-lock begin", OPERATE_TEXT)


if __name__ == "__main__":
    unittest.main()
