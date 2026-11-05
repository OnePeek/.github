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

# A fake S3 (one file per key) with the conditional-write semantics the lock relies on: If-None-Match: * refuses an
# existing key, If-Match refuses another ETag, a missing key is NoSuchKey. FAIL_<OP> injects any other error, and
# RACE_ON_DELETE has another run take the lock just before a delete.
FAKE_AWS = r"""#!/usr/bin/env python3
import hashlib, json, os, shutil, sys
state = os.environ["FAKE_S3"]
args = sys.argv[1:]
with open(os.path.join(state, "calls"), "a") as f:
    f.write("aws " + " ".join(args) + "\n")
def opt(name):
    return args[args.index(name) + 1] if name in args else None
def paths():
    k = opt("--key").replace("/", "_")
    return os.path.join(state, "s3_" + k), os.path.join(state, "etag_" + k)
def fail(op, message):
    sys.stderr.write("An error occurred (%s) when calling the %s operation: x\n" % (message, op))
    sys.exit(254)
op = args[1] if len(args) > 1 else ""
injected = os.environ.get("FAIL_" + op.replace("-", "_").upper())
if injected:
    fail(op, injected)
if op == "put-object":
    obj, tag = paths()
    if opt("--if-none-match") == "*" and os.path.exists(obj):
        fail("PutObject", "PreconditionFailed")
    shutil.copy(opt("--body"), obj)
    etag = '"%s"' % hashlib.md5(open(obj, "rb").read()).hexdigest()
    open(tag, "w").write(etag)
    print(json.dumps({"ETag": etag}))
elif op == "get-object":
    obj, tag = paths()
    if not os.path.exists(obj):
        fail("GetObject", "NoSuchKey")
    shutil.copy(obj, args[args.index("--key") + 2])
    print(open(tag).read())
elif op == "delete-object":
    obj, tag = paths()
    race = os.environ.get("RACE_ON_DELETE")
    if race and opt("--key") == "locks/release.json":
        open(obj, "w").write(race)
        open(tag, "w").write('"raced"')
    if not os.path.exists(obj):
        fail("DeleteObject", "NoSuchKey")
    if opt("--if-match") != open(tag).read():
        fail("DeleteObject", "PreconditionFailed")
    os.remove(obj)
    os.remove(tag)
else:
    print(os.environ.get("AWS_" + op.replace("-", "_").upper(), ""))
"""
TOKEN = "c" * 32  # a fixed fake lock token (32 hex characters)


def record(repo: str = "OnePeek/onepeek-infra", run: str = "77", attempt: str = "1", token: str = "f" * 32) -> str:
    return json.dumps({"repo": repo, "run_id": run, "run_attempt": attempt, "commit": "sha-" + "1" * 12,
                       "started": "2026-11-03T10:00:00+00:00", "token": token})  # fmt: skip


def frozen(repo: str = "OnePeek/backend-service", ref: str = "refs/heads/hotfix/x", run: str = "90") -> str:
    return json.dumps({"repo": repo, "ref": ref, "run_id": run, "run_attempt": "1", "who": "founder", "why": "hotfix " + ref[11:],
                       "run": "https://github.com/%s/actions/runs/%s" % (repo, run)})  # fmt: skip


def job_step(doc: dict, job: str, name: str) -> str:
    return next(s["run"] for s in doc["jobs"][job]["steps"] if s.get("name", "").startswith(name))


# release-ecs.yml takes the lock in its first job, prepare, and releases it in its last, finish (foundation-reset W2.1);
# operate.yml and release-lock.yml keep their lock and unlock jobs.
LOCK_JOB = {"release-ecs": "prepare", "operate": "lock", "release-lock": "lock"}
UNLOCK_JOB = {"release-ecs": "finish", "operate": "unlock"}
TAKE = job_step(RELEASE, "prepare", "Take the release lock")
RELEASE_STEP = job_step(RELEASE, "finish", "Release the release lock")
LOCKWF = yaml.safe_load((WORKFLOWS / "release-lock.yml").read_text(encoding="utf-8"))
ACTION = job_step(LOCKWF, "lock", "Take, release, freeze or thaw")


def run(script: str, held: str | None = None, gh: str = "echo in_progress", frozen_by: str | None = None, files: dict | None = None,
        git: str | None = None, **extra: str):
    """Returns (result, calls, the lock left in S3 or None, GITHUB_OUTPUT); `.frozen` and `.summary` on the result."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        stubs = {"aws": FAKE_AWS, "gh": f'#!/bin/bash\necho "gh $*" >> "{root}/calls"\n{gh}\n'}
        if git is not None:
            stubs["git"] = f'#!/bin/bash\necho "git $*" >> "{root}/calls"\n{git}\n'
        for name, body in stubs.items():
            f = root / name
            f.write_text(body)
            f.chmod(f.stat().st_mode | stat.S_IEXEC)
        if held is not None:
            (root / "s3_locks_release.json").write_text(held)
            (root / "etag_locks_release.json").write_text('"held-etag"')
        if frozen_by is not None:
            (root / "s3_locks_frozen.json").write_text(frozen_by)
            (root / "etag_locks_frozen.json").write_text('"frozen-etag"')
        for name, content in (files or {}).items():
            (root / name).write_text(content)
        out = subprocess.run(["bash", "-eo", "pipefail", "-c", script], cwd=root, capture_output=True, text=True, timeout=60, env={
            **os.environ, **ENV, **extra, "FAKE_S3": str(root), "PATH": f"{root}:{os.environ['PATH']}", "GITHUB_OUTPUT": str(root / "output"),
            "GITHUB_STEP_SUMMARY": str(root / "summary"),
        })  # fmt: skip
        read = lambda n: (root / n).read_text() if (root / n).exists() else None
        out.frozen = read("s3_locks_frozen.json")  # type: ignore[attr-defined]
        out.summary = read("summary") or ""  # type: ignore[attr-defined]
        return out, read("calls") or "", read("s3_locks_release.json"), read("output") or ""


class Take(unittest.TestCase):
    def test_a_free_lock_is_taken_with_a_conditional_write_naming_this_run(self):
        out, calls, obj, output = run(TAKE)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("aws s3api put-object --bucket onepeek-preprod-deploy --key locks/release.json --body lock-mine.json --if-none-match *", calls)
        mine = json.loads(obj)
        self.assertEqual({k: mine[k] for k in ("repo", "run_id", "run_attempt", "commit")},
                         {"repo": REPO, "run_id": RUN, "run_attempt": "1", "commit": "sha-" + "f" * 12})
        self.assertTrue(mine["started"].endswith("+00:00"))
        self.assertRegex(mine["token"], r"^[0-9a-f]{32}$")
        self.assertIn("token=" + mine["token"], output)
        self.assertIn("attempt=1", output)

    def test_every_take_draws_a_new_random_token(self):
        tokens = {json.loads(run(TAKE)[2])["token"] for _ in range(3)}
        self.assertEqual(len(tokens), 3)

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

    def test_the_same_attempt_of_this_run_is_not_taken_over(self):
        # Two takes in one caller run (release-lock.yml called twice) must not take each other's lock.
        out, calls, obj, _ = run(TAKE, held=record(REPO, RUN, "1"))
        self.assertEqual(out.returncode, 1)
        self.assertEqual(obj, record(REPO, RUN, "1"))

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
    def test_the_lock_is_removed_on_its_etag_only_with_its_token(self):
        out, calls, obj, _ = run(RELEASE_STEP, held=record(REPO, RUN, token=TOKEN), LOCK_TOKEN=TOKEN)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn('delete-object --bucket onepeek-preprod-deploy --key locks/release.json --if-match "held-etag"', calls)
        self.assertIsNone(obj)

    def test_another_token_is_never_released_even_from_this_run(self):
        for held, given in ((record(REPO, RUN), TOKEN), (record(token=TOKEN), "a" * 32), (record(REPO, RUN, token=TOKEN), "")):
            out, calls, obj, _ = run(RELEASE_STEP, held=held, LOCK_TOKEN=given)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            self.assertIn("it is left in place", out.stdout)
            self.assertNotIn("delete-object", calls)
            self.assertEqual(obj, held)

    def test_no_lock_is_fine(self):
        out, calls, _, _ = run(RELEASE_STEP, LOCK_TOKEN=TOKEN)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertNotIn("delete-object", calls)

    def test_an_unreadable_lock_fails_visibly(self):
        for error in ("AccessDenied", "InternalError"):
            out, calls, obj, _ = run(RELEASE_STEP, held=record(REPO, RUN, token=TOKEN), LOCK_TOKEN=TOKEN, FAIL_GET_OBJECT=error)
            self.assertEqual(out.returncode, 1)
            self.assertNotIn("delete-object", calls)
            self.assertEqual(obj, record(REPO, RUN, token=TOKEN))

    def test_a_lock_taken_over_meanwhile_is_left_in_place(self):
        other = record("OnePeek/onepeek-web", "88")
        out, _, obj, _ = run(RELEASE_STEP, held=record(REPO, RUN, token=TOKEN), LOCK_TOKEN=TOKEN, RACE_ON_DELETE=other)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(obj, other)


class Frozen(unittest.TestCase):
    """preprod-frozen (playbook §5.4, §5.6): locks/frozen.json names a hotfix branch (repo and ref); any run from that
    branch takes the lock, every other take is refused."""

    def test_a_frozen_environment_refuses_runs_from_other_branches_at_once_with_the_reason(self):
        for repo, ref in ((REPO, "refs/heads/release"), (REPO, "refs/heads/hotfix/y"), ("OnePeek/onepeek-web", "refs/heads/hotfix/x")):
            out, calls, obj, _ = run(TAKE, frozen_by=frozen(), GITHUB_REPOSITORY=repo, GITHUB_REF=ref)
            self.assertEqual(out.returncode, 1, (repo, ref))
            self.assertIn("preprod is frozen by founder (hotfix hotfix/x) for the hotfix branch OnePeek/backend-service refs/heads/hotfix/x", out.stdout)
            self.assertNotIn("put-object", calls)
            self.assertIsNone(obj)

    def test_any_run_from_the_frozen_hotfix_branch_takes_the_lock(self):
        # Each merge into the hotfix branch is a new run (another run ID than the one that froze it).
        for run_id in ("90", RUN):
            out, _, obj, _ = run(TAKE, frozen_by=frozen(), GITHUB_RUN_ID=run_id, GITHUB_REF="refs/heads/hotfix/x")
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            self.assertEqual(json.loads(obj)["run_id"], run_id)

    def test_an_unreadable_freeze_means_no_deploy(self):
        out, calls, _, _ = run(TAKE, FAIL_GET_OBJECT="AccessDenied")
        self.assertEqual(out.returncode, 1)
        self.assertIn("frozen.json is unreadable", out.stdout)
        self.assertNotIn("put-object", calls)
        for junk in ("not json", json.dumps({"repo": REPO}), frozen(ref="refs/heads/release")):
            out, calls, _, _ = run(TAKE, frozen_by=junk, GITHUB_REF="refs/heads/release")
            self.assertEqual(out.returncode, 1, junk)
            self.assertNotIn("put-object", calls)

    def test_freeze_is_written_from_a_hotfix_branch_naming_it(self):
        out, calls, _, _ = run(ACTION, ACTION="freeze", GITHUB_REF="refs/heads/hotfix/x", GITHUB_REF_NAME="hotfix/x", GITHUB_ACTOR="founder")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("put-object --bucket onepeek-preprod-deploy --key locks/frozen.json --body lock-frozen-mine.json --if-none-match *", calls)
        written = json.loads(out.frozen)  # type: ignore[attr-defined]
        self.assertEqual((written["repo"], written["ref"], written["who"], written["why"]), (REPO, "refs/heads/hotfix/x", "founder", "hotfix hotfix/x"))
        self.assertNotIn("token", written)

    def test_freeze_from_any_other_branch_is_refused(self):
        out, calls, _, _ = run(ACTION, ACTION="freeze", GITHUB_REF="refs/heads/dev")
        self.assertEqual(out.returncode, 1)
        self.assertNotIn("put-object", calls)

    def test_a_second_freeze_from_another_branch_is_refused_and_from_the_same_branch_is_fine(self):
        out, _, _, _ = run(ACTION, frozen_by=frozen(REPO), ACTION="freeze", GITHUB_REF="refs/heads/hotfix/y")
        self.assertEqual(out.returncode, 1)
        self.assertEqual(out.frozen, frozen(REPO))  # type: ignore[attr-defined]
        out, _, _, _ = run(ACTION, frozen_by=frozen(REPO), ACTION="freeze", GITHUB_REF="refs/heads/hotfix/x")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_thaw_removes_the_freeze_on_its_etag(self):
        out, calls, _, _ = run(ACTION, frozen_by=frozen(), ACTION="thaw")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn('delete-object --bucket onepeek-preprod-deploy --key locks/frozen.json --if-match "frozen-etag"', calls)
        self.assertIsNone(out.frozen)  # type: ignore[attr-defined]
        out, _, _, _ = run(ACTION, ACTION="thaw")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_an_unreadable_freeze_is_not_thawed(self):
        out, calls, _, _ = run(ACTION, frozen_by=frozen(), ACTION="thaw", FAIL_GET_OBJECT="AccessDenied")
        self.assertEqual(out.returncode, 1)
        self.assertNotIn("delete-object", calls)


class SharedLockWorkflow(unittest.TestCase):
    """release-lock.yml (playbook §5.4 "Shared lock workflow"): take, release, freeze, thaw from the same code."""

    def test_take_outputs_the_token_and_release_needs_it(self):
        out, _, obj, output = run(ACTION, ACTION="take")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        token = json.loads(obj)["token"]
        self.assertIn("token=" + token, output)
        out, _, obj, _ = run(ACTION, held=record(REPO, RUN, token=token), ACTION="release", GIVEN_TOKEN=token)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIsNone(obj)

    def test_the_interface(self):
        call = LOCKWF[True]["workflow_call"]
        self.assertEqual(set(call["inputs"]), {"runner", "environment", "action", "role-to-assume", "token"})
        self.assertEqual(call["outputs"]["token"]["value"], "${{ jobs.lock.outputs.token }}")
        self.assertTrue(all(s["required"] for s in call["secrets"].values()))
        check = job_step(LOCKWF, "lock", "Check the action")
        for action, token, code in (("take", "", 0), ("thaw", "", 0), ("release", "", 1), ("release", TOKEN, 0), ("unlock", "", 1)):
            r = subprocess.run(["bash", "-eo", "pipefail", "-c", check], capture_output=True, text=True,
                               env={**os.environ, "ACTION": action, "GIVEN_TOKEN": token})  # fmt: skip
            self.assertEqual(r.returncode, code, action)
        self.assertNotIn("concurrency", LOCKWF)
        self.assertEqual(LOCKWF["jobs"]["lock"]["permissions"]["actions"], "read")


def needs(doc: dict, job: str) -> list[str]:
    n = doc["jobs"][job].get("needs", [])
    return [n] if isinstance(n, str) else n


# The jobs that change something: build (signs and records the digest), deploy (migrate, assets, rollout, ops),
# verify (rollback) and finish (the deployment record). build-web shares build-image's steps.
GUARDED = ["build-image", "build-web", "deploy", "verify", "finish"]


class Wiring(unittest.TestCase):
    """The lock is taken by the first job, before any build, and released by an always() job (playbook §5.4)."""

    def test_release_ecs_takes_the_lock_before_anything_else(self):
        self.assertEqual(needs(RELEASE, "prepare"), [])
        names = [s.get("name", "") for s in RELEASE["jobs"]["prepare"]["steps"]]
        take = names.index("Take the release lock")
        # Before the lock only the refusals and the credentials to take it; the version and everything else after it.
        for later in ("Order.", "Refuse at once", "Record the commit"):
            self.assertGreater(names.index(next(n for n in names if n.startswith(later))), take, later)
        version = next(s for s in RELEASE["jobs"]["prepare"]["steps"] if s.get("id") == "v")
        self.assertEqual(version["if"], "${{ steps.order.outputs.skip != 'true' }}")
        for job in ("build-image", "build-web"):
            self.assertEqual(needs(RELEASE, job), ["prepare"], job)
            self.assertIn("needs.prepare.outputs.skip != 'true'", RELEASE["jobs"][job]["if"], job)
        for job in RELEASE["jobs"]:
            if job != "prepare":
                self.assertIn("prepare", needs(RELEASE, job), job)

    def test_unlock_always_runs_after_every_deploy_job(self):
        finish = RELEASE["jobs"]["finish"]
        # Only when the prepare job ran, and never for prod (which the prepare job refuses).
        self.assertEqual(finish["if"], "${{ always() && needs.prepare.result != 'skipped' && inputs.environment != 'prod' }}")
        self.assertEqual(set(needs(RELEASE, "finish")), set(RELEASE["jobs"]) - {"finish"})
        self.assertIn("lock_release", job_step(RELEASE, "finish", "Release the release lock"))
        # Within finish the unlock and its credentials run whatever the steps before them did.
        steps = finish["steps"]
        unlock = next(i for i, s in enumerate(steps) if s.get("name") == "Release the release lock")
        self.assertEqual(steps[unlock]["if"], "${{ always() }}")
        self.assertTrue(steps[unlock - 1]["uses"].startswith("aws-actions/configure-aws-credentials@"))
        self.assertEqual(steps[unlock - 1]["if"], "${{ always() }}")
        for doc, name in ((RELEASE, "release-ecs"), (OPERATE, "operate")):
            rel = next(s for s in doc["jobs"][UNLOCK_JOB[name]]["steps"] if s.get("name") == "Release the release lock")
            self.assertEqual(rel["env"]["LOCK_TOKEN"], "${{ needs.%s.outputs.token }}" % LOCK_JOB[name])
            self.assertEqual(doc["jobs"][LOCK_JOB[name]]["outputs"]["token"], "${{ steps.take.outputs.token }}")

    def test_the_lock_token_reaches_only_the_unlock_step(self):
        self.assertEqual(RELEASE_TEXT.count("needs.prepare.outputs.token"), 1)
        self.assertEqual(RELEASE_TEXT.count("LOCK_TOKEN: "), 1)

    def test_operate_takes_and_releases_the_lock_around_its_one_job(self):
        self.assertEqual(needs(OPERATE, "operate"), ["lock"])
        self.assertEqual(set(needs(OPERATE, "unlock")), {"lock", "operate"})
        self.assertEqual(OPERATE["jobs"]["unlock"]["if"], "${{ always() && needs.lock.result != 'skipped' }}")
        self.assertIn("lock_take", job_step(OPERATE, "lock", "Take the release lock"))
        self.assertIn("lock_release", job_step(OPERATE, "unlock", "Release the release lock"))

    def test_no_concurrency_group_queues_a_release(self):
        # GitHub keeps one pending run per group and cancels the older one (playbook §5.1 step 7, checklist M).
        for doc in (RELEASE, OPERATE, LOCKWF):
            self.assertNotIn("concurrency", doc)
            for name, job in doc["jobs"].items():
                self.assertNotIn("concurrency", job, name)

    def test_a_release_waits_30_minutes_polling_every_30_seconds(self):
        for doc, name in ((RELEASE, "release-ecs"), (OPERATE, "operate"), (LOCKWF, "release-lock")):
            self.assertEqual((doc["env"]["LOCK_WAIT_SECONDS"], doc["env"]["LOCK_POLL_SECONDS"]), ("1800", "30"))
            self.assertGreater(doc["jobs"][LOCK_JOB[name]]["timeout-minutes"] * 60, 1800)

    def test_the_lock_is_per_environment_in_its_deploy_bucket(self):
        self.assertIn('LOCK_BUCKET="${NAME}-deploy"', TAKE)
        self.assertIn('LOCK_KEY="locks/release.json"', TAKE)

    def test_the_app_secrets_are_required_and_their_token_reads_the_holders_state(self):
        # No quiet fallback to GITHUB_TOKEN: without the App a lock left by another repo could never be taken over.
        for doc, wf in ((RELEASE, "release-ecs"), (OPERATE, "operate"), (LOCKWF, "release-lock")):
            for name in ("AGENTS_APP_CLIENT_ID", "AGENTS_APP_PRIVATE_KEY"):
                self.assertTrue(doc[True]["workflow_call"]["secrets"][name]["required"], name)
            lock = doc["jobs"][LOCK_JOB[wf]]
            app = next(s for s in lock["steps"] if s.get("id") == "app")
            self.assertNotIn("if", app)
            self.assertTrue(app["uses"].startswith("actions/create-github-app-token@64ab729a6294000f5aa4a4bc0b73d1f32bc45510"))
            self.assertEqual(app["with"]["permission-actions"], "read")
            take = next(s for s in lock["steps"] if s.get("id") in ("take", "lock"))
            self.assertEqual(take["env"]["LOCK_GH_TOKEN"], "${{ steps.app.outputs.token }}")
            self.assertEqual(lock["permissions"]["actions"], "read")
            self.assertNotIn("github.token", json.dumps(lock))
        check = job_step(RELEASE, "prepare", "The agents' GitHub App secrets are set")
        for has, code in (("false", 1), ("", 1), ("true", 0)):
            r = subprocess.run(["bash", "-eo", "pipefail", "-c", check], capture_output=True, text=True, env={**os.environ, "HAS_APP": has})
            self.assertEqual(r.returncode, code, has)
            if code:
                self.assertIn("AGENTS_APP_CLIENT_ID and AGENTS_APP_PRIVATE_KEY must be passed", r.stdout)
        names = [s.get("name", "") for s in RELEASE["jobs"]["prepare"]["steps"]]
        self.assertLess(names.index("The agents' GitHub App secrets are set (required, playbook §5.4)"), names.index("Take the release lock"))

    def test_deploy_from_dev_is_off_unless_the_input_turns_it_on(self):
        script = job_step(RELEASE, "prepare", "Deploy from dev is off")
        self.assertIs(RELEASE[True]["workflow_call"]["inputs"]["deploy-from-dev"]["default"], False)
        for ref, flag, code in (("refs/heads/dev", "false", 1), ("refs/heads/dev", "true", 0), ("refs/heads/release", "false", 0)):
            r = subprocess.run(["bash", "-eo", "pipefail", "-c", script], capture_output=True, text=True,
                               env={**os.environ, "GITHUB_REF": ref, "DEPLOY_FROM_DEV": flag})  # fmt: skip
            self.assertEqual(r.returncode, code, (ref, flag))
            if code:
                self.assertIn("deploy from dev is disabled until the launch switch-over (foundation-playbook §5.4)", r.stdout)
        names = [s.get("name", "") for s in RELEASE["jobs"]["prepare"]["steps"]]
        self.assertLess(names.index(next(n for n in names if n.startswith("Deploy from dev"))), names.index("Take the release lock"))

    def test_prod_is_promote_only(self):
        script = job_step(RELEASE, "prepare", "Prod is promote-only")
        refused = subprocess.run(["bash", "-eo", "pipefail", "-c", script], capture_output=True, text=True, env={**os.environ, "ENVIRONMENT_NAME": "prod"})
        self.assertEqual(refused.returncode, 1)
        self.assertIn("promote.yml", refused.stdout)
        allowed = subprocess.run(["bash", "-eo", "pipefail", "-c", script], capture_output=True, text=True, env={**os.environ, "ENVIRONMENT_NAME": "preprod"})
        self.assertEqual(allowed.returncode, 0)

    def test_operate_runs_from_release_only_until_launch(self):
        # Founder 2026-11-03 (playbook §5.4): until launch `release` is preprod; no default depends on dev.
        want = "${{ github.ref == 'refs/heads/release' }}"
        self.assertEqual(OPERATE["jobs"]["lock"]["if"], want)
        self.assertEqual(OPERATE["jobs"]["operate"]["if"], want)
        self.assertNotIn("refs/heads/dev", OPERATE_TEXT)


class PartialReRun(unittest.TestCase):
    """A re-run of only the failed jobs does not re-run the lock job, so every job that changes anything checks that
    this attempt took the lock."""

    def guard(self, doc: dict, job: str) -> dict:
        return doc["jobs"][job]["steps"][0]

    def test_every_changing_job_checks_the_lock_first(self):
        for doc, jobs, lock in ((RELEASE, GUARDED, "prepare"), (OPERATE, ["operate"], "lock")):
            for job in jobs:
                self.assertIn(lock, needs(doc, job), job)
                g = self.guard(doc, job)
                self.assertTrue(g["name"].startswith("This run attempt holds the release lock"), job)
                self.assertEqual(g["env"]["LOCKED_ATTEMPT"], "${{ needs.%s.outputs.attempt }}" % lock)

    def test_the_deployment_record_and_the_rollback_run_only_after_the_lock_check(self):
        notify = next(s for s in RELEASE["jobs"]["finish"]["steps"] if s.get("name", "").startswith("Deployment record"))
        self.assertIn("steps.guard.outcome == 'success'", notify["if"])
        rollback = next(s for s in RELEASE["jobs"]["verify"]["steps"] if s.get("id") == "rollback")
        self.assertIn("steps.guard.outcome == 'success'", rollback["if"])
        for job in ("verify", "finish"):
            self.assertEqual(self.guard(RELEASE, job).get("id"), "guard", job)

    def test_the_reusable_db_bootstrap_call_is_skipped_outside_the_locking_attempt(self):
        self.assertIn("needs.prepare.outputs.attempt == format('{0}', github.run_attempt)", RELEASE["jobs"]["db-bootstrap"]["if"])
        self.assertIn("prepare", needs(RELEASE, "db-bootstrap"))

    def test_the_check_fails_on_another_attempt_and_passes_on_the_locking_one(self):
        script = self.guard(RELEASE, "deploy")["run"]
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
        self.assertIn("lock_take", ACTION)


INDEX = json.dumps({"onepeek-preprod-api": {"kind": "service", "order": 1, "service": "api", "image": "backend"},
                    "onepeek-preprod-migrate": {"kind": "one-off", "order": 0, "service": "migrate", "image": "backend"},
                    "onepeek-preprod-web": {"kind": "service", "order": 1, "service": "web", "image": "web"}})  # fmt: skip
ORDER = job_step(RELEASE, "prepare", "Order.")


def order(running: str, git: str, **extra: str):
    aws = {"AWS_DESCRIBE_SERVICES": "arn:td/api:7", "AWS_DESCRIBE_TASK_DEFINITION": running, **extra}
    return run(ORDER, files={"index.json": INDEX}, git=git, IMAGE="backend", **aws)


class Order(unittest.TestCase):
    """Playbook §5.4 "Order, not only one at a time": an older run never undoes a newer one."""

    def test_a_newer_commit_running_makes_this_release_a_skip_with_a_notice(self):
        out, calls, _, output = order("sha-" + "2" * 12, 'case "$1" in rev-parse) echo ' + "2" * 40 + ';; merge-base) exit 0;; esac')
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("skip=true", output)
        self.assertIn("::notice::api already runs sha-222222222222", out.stdout)
        self.assertIn("Skipped", out.summary)  # type: ignore[attr-defined]
        self.assertIn("git merge-base --is-ancestor " + "f" * 40 + " " + "2" * 40, calls)
        self.assertIn("ecs describe-services --cluster onepeek-preprod --services api", calls)
        self.assertNotIn("--services migrate", calls)
        self.assertNotIn("--services web", calls)

    def test_an_older_commit_running_deploys(self):
        out, _, _, output = order("sha-" + "2" * 12, 'case "$1" in rev-parse) echo ' + "2" * 40 + ';; merge-base) exit 1;; esac')
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("skip=false", output)

    def test_the_same_commit_or_no_deployment_or_no_tag_deploys_without_asking_git(self):
        for running, services in (("sha-" + "f" * 12, "arn:td/api:7"), ("None", "arn:td/api:7"), ("x", "None")):
            out, calls, _, output = order(running, "exit 9", AWS_DESCRIBE_SERVICES=services)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            self.assertIn("skip=false", output)
            self.assertNotIn("git ", calls)

    def test_a_commit_that_cannot_be_compared_means_no_deploy(self):
        for running, git in (("sha-" + "2" * 12, "exit 1"), ("latest", "exit 9"),
                             ("sha-" + "2" * 12, 'case "$1" in rev-parse) echo ' + "2" * 40 + ';; merge-base) exit 128;; esac')):
            out, _, _, output = order(running, git)
            self.assertEqual(out.returncode, 1, running)
            self.assertNotIn("skip=", output)

    def test_the_checkout_has_the_history(self):
        checkout = next(s for s in RELEASE["jobs"]["prepare"]["steps"] if str(s.get("uses", "")).startswith("actions/checkout@"))
        self.assertEqual(checkout["with"]["fetch-depth"], 0)


if __name__ == "__main__":
    unittest.main()
