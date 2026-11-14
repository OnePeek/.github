"""The release locks per scope in release-ecs.yml, operate.yml and release-lock.yml (onepeek-docs
architecture/lld/infra/foundation-playbook.md §5.4, §5.4a; delivery-pipelines.md UC-DEL-001, UC-DEL-002, UC-DEL-007,
E2, E3, E5, E10, E11), run for real against a fake S3 and ECS and a stub `gh`.
Run: python -m unittest discover -s scripts -p 'test_*.py'"""

import json
import os
import re
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"
RELEASE = yaml.safe_load((WORKFLOWS / "release-ecs.yml").read_text(encoding="utf-8"))
OPERATE = yaml.safe_load((WORKFLOWS / "operate.yml").read_text(encoding="utf-8"))
LOCKWF = yaml.safe_load((WORKFLOWS / "release-lock.yml").read_text(encoding="utf-8"))
RELEASE_TEXT = (WORKFLOWS / "release-ecs.yml").read_text(encoding="utf-8")
OPERATE_TEXT = (WORKFLOWS / "operate.yml").read_text(encoding="utf-8")
REPO, RUN = "OnePeek/backend-service", "5000"
INFRA = "OnePeek/onepeek-infra"
ENV = {"NAME": "onepeek-preprod", "ENVIRONMENT": "preprod", "GITHUB_REPOSITORY": REPO, "GITHUB_RUN_ID": RUN,
       "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "f" * 40, "GITHUB_SERVER_URL": "https://github.com", "LOCK_SCOPE": "backend",
       "LOCK_WAIT_SECONDS": "2", "LOCK_WAIT_CAP_SECONDS": "4", "LOCK_QUEUED_WAIT_SECONDS": "2", "LOCK_MIGRATE_WAIT_SECONDS": "2",
       "LOCK_REMINT_SECONDS": "3000", "LOCK_POLL_SECONDS": "1", "LOCK_LOOKUP_GAP_SECONDS": "0", "LOCK_GH_TOKEN": "t"}  # fmt: skip
LEGACY, BACKEND, WEB, INFRA_KEY, MCP = "locks/release.json", "locks/backend.json", "locks/web.json", "locks/infra.json", "locks/mcp.json"
TASK = "arn:aws:ecs:ap-south-1:111111111111:task/onepeek-preprod/abc123"
MIGRATE_TD = "arn:aws:ecs:ap-south-1:111111111111:task-definition/onepeek-preprod-migrate:4"

# A fake S3 (one file per key) with the conditional-write semantics the lock relies on: If-None-Match: * refuses an
# existing key, If-Match refuses another ETag (and a missing key), a missing key is NoSuchKey. FAIL_<OP> injects any other
# error; CONFLICT_PUTS answers that many conditional puts with 409 (CHANGE_ON_CONFLICT: and gives the key a new ETag, its
# body unchanged, as another writer's write would); RACE_ON_DELETE has another run take a key just
# before its delete; CHANGE_BEFORE_PUT changes a key just before a put with If-Match; FROZEN_AFTER_PUTS writes
# FROZEN_BODY as locks/frozen.json right after that many key writes (a freeze racing a take). Every put's body is logged in
# `puts`. Other calls print SEQ_<OP> (a JSON list, one per call) or AWS_<OP>.
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
def counter(name):
    path = os.path.join(state, "count_" + name)
    n = int(open(path).read()) if os.path.exists(path) else 0
    open(path, "w").write(str(n + 1))
    return n
op = args[1] if len(args) > 1 else ""
injected = os.environ.get("FAIL_" + op.replace("-", "_").upper())
if injected:
    fail(op, injected)
if op == "put-object":
    obj, tag = paths()
    if opt("--if-none-match") == "*" and os.path.exists(obj):
        fail("PutObject", "PreconditionFailed")
    if opt("--if-match"):
        if counter("conflict") < int(os.environ.get("CONFLICT_PUTS", "0")):
            if os.environ.get("CHANGE_ON_CONFLICT") and os.path.exists(tag):
                open(tag, "w").write('"conflicted"')
            fail("PutObject", "ConditionalRequestConflict")
        change = os.environ.get("CHANGE_BEFORE_PUT")
        if change and os.path.exists(obj):
            open(obj, "w").write(change)
            open(tag, "w").write('"changed"')
        if not os.path.exists(obj):
            fail("PutObject", "NoSuchKey")
        if opt("--if-match") != open(tag).read():
            fail("PutObject", "PreconditionFailed")
    shutil.copy(opt("--body"), obj)
    etag = '"%s"' % hashlib.md5(open(obj, "rb").read()).hexdigest()
    open(tag, "w").write(etag)
    with open(os.path.join(state, "puts"), "a") as f:
        f.write(opt("--key") + " " + open(obj).read() + "\n")
    after = os.environ.get("FROZEN_AFTER_PUTS")   # a freeze written by another run right after this run's N-th key write
    if after and opt("--key") != "locks/frozen.json" and counter("frozen_after") + 1 == int(after):
        open(os.path.join(state, "s3_locks_frozen.json"), "w").write(os.environ["FROZEN_BODY"])
        open(os.path.join(state, "etag_locks_frozen.json"), "w").write('"raced-frozen"')
    print(etag)
elif op == "get-object":
    obj, tag = paths()
    if not os.path.exists(obj):
        fail("GetObject", "NoSuchKey")
    shutil.copy(obj, args[args.index("--key") + 2])
    print(open(tag).read())
elif op == "delete-object":
    obj, tag = paths()
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
elif op == "describe-task-definition" and "--query" not in args and not os.environ.get("AWS_DESCRIBE_TASK_DEFINITION") \
        and not os.environ.get("SEQ_DESCRIBE_TASK_DEFINITION"):
    # A whole revision as ECS describes it (any revision, ACTIVE or not): INACTIVE when TD_INACTIVE names its ARN, TD_STATUS
    # instead for every revision; the read-only fields ECS adds come back too, as they do from the real call.
    arn = opt("--task-definition")
    family, rev = arn.split("/")[-1].rsplit(":", 1)
    status = os.environ.get("TD_STATUS") or ("INACTIVE" if arn in os.environ.get("TD_INACTIVE", "").split() else "ACTIVE")
    print(json.dumps({"taskDefinition": {
        "taskDefinitionArn": arn, "family": family, "revision": int(rev), "status": status,
        "containerDefinitions": [{"name": family.rsplit("-", 1)[-1], "image": "r/x@sha256:" + "a" * 64}],
        "executionRoleArn": "arn:aws:iam::111111111111:role/exec", "networkMode": "awsvpc", "cpu": "256", "memory": "512",
        "requiresCompatibilities": ["FARGATE"], "volumes": [], "compatibilities": ["EC2", "FARGATE"],
        "requiresAttributes": [{"name": "ecs.capability.execution-role-awslogs"}], "registeredAt": "2026-11-01T00:00:00Z",
        "registeredBy": "arn:aws:sts::111111111111:assumed-role/infra/x", "deregisteredAt": "2026-11-14T00:00:00Z"},
        "tags": [{"key": "commit", "value": "sha-" + "1" * 12}, {"key": "release-run", "value": "4000"}]}))
elif op == "register-task-definition" and not os.environ.get("AWS_REGISTER_TASK_DEFINITION"):
    # Every registered body is logged in `registered`; the new revision is <family>:100, :101, ...
    body = open(opt("--cli-input-json")[len("file://"):]).read()
    with open(os.path.join(state, "registered"), "a") as f:
        f.write(body.strip() + "\n")
    print("arn:aws:ecs:ap-south-1:111111111111:task-definition/%s:%d" % (json.loads(body)["family"], 100 + counter("register")))
else:
    name = op.replace("-", "_").upper()
    seq = os.environ.get("SEQ_" + name)
    if seq:
        items = json.loads(seq)
        print(items[min(counter("seq_" + name), len(items) - 1)])
    else:
        print(os.environ.get("AWS_" + name, ""))
"""
TOKEN = "c" * 32  # a fixed fake lock token (32 hex characters)


def record(repo: str = INFRA, run: str = "77", attempt: str = "1", token: str = "f" * 32, **fields) -> str:
    r = {"repo": repo, "run_id": run, "run_attempt": attempt, "commit": "sha-" + "1" * 12, "started": "2026-11-03T10:00:00+00:00", "token": token}
    r.update(fields)
    return json.dumps(r)


def mine(phase: str = "locked", **fields) -> str:
    return record(REPO, RUN, token=TOKEN, scope="backend", phase=phase, **fields)


INFRA_HEAD = "e" * 40


def frozen(repo: str = "OnePeek/backend-service", ref: str = "refs/heads/hotfix/x", run: str = "90", **fields) -> str:
    """A preprod-frozen record; pass infra_head and infra_settled for one whose freeze has finished."""
    r = {"repo": repo, "ref": ref, "run_id": run, "run_attempt": "1", "who": "founder", "why": "hotfix " + ref[11:],
         "run": "https://github.com/%s/actions/runs/%s" % (repo, run)}  # fmt: skip
    r.update(fields)
    return json.dumps(r)


def settled_frozen(**kw) -> str:
    return frozen(infra_head=INFRA_HEAD, infra_settled=True, **kw)


def job_step(doc: dict, job: str, name: str) -> str:
    return next(s["run"] for s in doc["jobs"][job]["steps"] if s.get("name", "").startswith(name))


def step_of(doc: dict, job: str, name: str) -> dict:
    return next(s for s in doc["jobs"][job]["steps"] if s.get("name", "").startswith(name))


LOCK_JOB = {"release-ecs": "prepare", "operate": "lock", "release-lock": "lock"}
UNLOCK_JOB = {"release-ecs": "finish", "operate": "unlock"}
TAKE = job_step(RELEASE, "prepare", "Take the release lock")
FINISH = job_step(RELEASE, "finish", "Release the release lock")
ACTION = job_step(LOCKWF, "lock", "Take, release, freeze or thaw")
LIB = re.search(r"# --- release-lock begin.*?# --- release-lock end ---\n", TAKE, re.S).group(0)


def run(script: str, keys: dict | None = None, gh: str = "echo in_progress", frozen_by: str | None = None, files: dict | None = None,
        git: str | None = None, stubs: dict | None = None, **extra: str):
    """Returns the result with .keys (every key left in S3), .calls, .puts, .output, .summary, .frozen and .elapsed (wall-clock seconds)."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        tools = {"aws": FAKE_AWS, "gh": f'#!/bin/bash\necho "gh token=$GH_TOKEN $*" >> "{root}/calls"\n{gh}\n', **(stubs or {})}
        if git is not None:
            tools["git"] = f'#!/bin/bash\necho "git $*" >> "{root}/calls"\n{git}\n'
        for name, body in tools.items():
            f = root / name
            f.write_text(body if body.startswith("#!") else f'#!/bin/bash\necho "{name} $*" >> "{root}/calls"\n{body}\n')
            f.chmod(f.stat().st_mode | stat.S_IEXEC)
        for key, content in (keys or {}).items():
            (root / ("s3_" + key.replace("/", "_"))).write_text(content)
            (root / ("etag_" + key.replace("/", "_"))).write_text('"etag-%s"' % key.split("/")[-1])
        if frozen_by is not None:
            (root / "s3_locks_frozen.json").write_text(frozen_by)
            (root / "etag_locks_frozen.json").write_text('"frozen-etag"')
        for name, content in (files or {}).items():
            (root / name).write_text(content)
        began = time.monotonic()
        out = subprocess.run(["bash", "-eo", "pipefail", "-c", script], cwd=root, capture_output=True, text=True, timeout=60, env={
            **os.environ, **ENV, **extra, "FAKE_S3": str(root), "PATH": f"{root}:{os.environ['PATH']}", "GITHUB_OUTPUT": str(root / "output"),
            "GITHUB_STEP_SUMMARY": str(root / "summary"),
        })  # fmt: skip
        out.elapsed = time.monotonic() - began  # type: ignore[attr-defined]
        read = lambda n: (root / n).read_text() if (root / n).exists() else None
        out.keys = {p.name[3:].replace("locks_", "locks/"): p.read_text() for p in root.glob("s3_locks_*") if p.name != "s3_locks_frozen.json"}  # type: ignore[attr-defined]
        out.frozen = read("s3_locks_frozen.json")  # type: ignore[attr-defined]
        out.calls = read("calls") or ""  # type: ignore[attr-defined]
        out.puts = read("puts") or ""  # type: ignore[attr-defined]
        out.output = read("output") or ""  # type: ignore[attr-defined]
        out.summary = read("summary") or ""  # type: ignore[attr-defined]
        out.token_request = read("token-request") or ""  # type: ignore[attr-defined]
        out.headers = read("headers") or ""  # type: ignore[attr-defined]
        out.registered = read("registered") or ""  # type: ignore[attr-defined]
        return out


def call(*lines: str) -> str:
    """The lock functions followed by the given lines (a phase write, a finish), as a step would run them."""
    return LIB + "\n".join(lines) + "\n"


def tasks(*items: tuple[str, str, str]) -> str:
    return json.dumps({"tasks": [{"taskArn": a, "taskDefinitionArn": td, "lastStatus": s} for a, td, s in items], "failures": []})


class Take(unittest.TestCase):
    def test_a_free_scope_takes_release_json_then_its_own_key_with_conditional_writes_naming_this_run(self):
        out = run(TAKE)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        puts = [line for line in out.calls.splitlines() if "put-object" in line]
        self.assertEqual([p.split("--key ")[1].split()[0] for p in puts], [LEGACY, BACKEND])
        self.assertTrue(all("--if-none-match *" in p for p in puts))
        self.assertEqual(set(out.keys), {LEGACY, BACKEND})
        rec = json.loads(out.keys[BACKEND])
        self.assertEqual({k: rec[k] for k in ("repo", "run_id", "run_attempt", "commit", "scope", "phase")},
                         {"repo": REPO, "run_id": RUN, "run_attempt": "1", "commit": "sha-" + "f" * 12, "scope": "backend", "phase": "locked"})
        self.assertRegex(rec["token"], r"^[0-9a-f]{32}$")
        self.assertEqual(json.loads(out.keys[LEGACY]), rec)
        self.assertIn("token=" + rec["token"], out.output)
        self.assertIn("attempt=1", out.output)

    def test_every_take_draws_a_new_random_token(self):
        tokens = {json.loads(run(TAKE).keys[BACKEND])["token"] for _ in range(3)}
        self.assertEqual(len(tokens), 3)

    def test_the_scope_is_never_a_free_input(self):
        self.assertEqual(RELEASE["env"]["LOCK_SCOPE"], "${{ inputs.image }}")
        for bad in ("", "sandbox", "all"):
            out = run(TAKE, LOCK_SCOPE=bad)
            self.assertEqual(out.returncode, 1, bad)
            self.assertNotIn("put-object", out.calls)

    def test_its_own_key_held_by_a_live_run_of_its_scope_waits_30_minutes_then_fails_with_the_re_run_message_holding_nothing(self):
        held = record(REPO, "77", scope="backend", phase="locked")
        out = run(TAKE, keys={BACKEND: held}, LOCK_WAIT_SECONDS="2", LOCK_WAIT_CAP_SECONDS="30")
        self.assertEqual(out.returncode, 1)
        self.assertIn("::error::waiting for OnePeek/backend-service (backend, run 77); re-run", out.stdout)
        self.assertLess(out.stdout.count("Waiting for the preprod release lock"), 6)  # the 30-minute limit, not the cap
        self.assertEqual(out.keys, {BACKEND: held})  # release.json was given back every time: no wait while holding a key

    def test_its_own_key_held_by_infra_waits_up_to_the_cap_not_the_30_minutes(self):
        held = record(INFRA, "77", scope="infra")
        out = run(TAKE, keys={BACKEND: held}, LOCK_WAIT_SECONDS="1", LOCK_WAIT_CAP_SECONDS="6")
        self.assertEqual(out.returncode, 1)
        # The cap is wall-clock time (bash SECONDS), so it is asserted as time: the step ends only once SECONDS has
        # moved 6 past its start, more than 5 real seconds. The 1-second own-scope limit would end it after one wait.
        # A count of waits inside the cap shrinks on a loaded machine, to none when one try takes the whole cap (pitfall 33).
        self.assertGreaterEqual(out.elapsed, 5)
        self.assertIn("::error::waiting for OnePeek/onepeek-infra (infra, run 77); re-run", out.stdout)

    def test_try_or_release_gives_back_every_key_taken_on_the_etag_of_its_own_write_before_it_waits(self):
        # Held by a live run of its own scope, so the step stops after exactly two tries by the counted own-scope limit
        # (LOCK_WAIT_SECONDS 1, one poll per wait), never by the wall-clock cap, which a loaded machine could reach
        # after the first try (pitfall 33).
        out = run(TAKE, keys={BACKEND: record(REPO, "77", scope="backend")}, LOCK_WAIT_SECONDS="1", LOCK_WAIT_CAP_SECONDS="60")
        self.assertEqual(out.returncode, 1)
        written = re.search(r'locks/release.json \{.*', out.puts).group(0)
        self.assertIn(REPO, written)
        lines = out.calls.splitlines()
        put = next(i for i, l in enumerate(lines) if "put-object" in l and LEGACY in l)
        delete = next(i for i, l in enumerate(lines) if "delete-object" in l and LEGACY in l)
        self.assertGreater(delete, put)
        self.assertRegex(lines[delete], r'--if-match "[0-9a-f]{32}"')
        self.assertNotIn(LEGACY, out.keys)
        sleeps = [i for i, l in enumerate(lines) if "get-object" in l and "frozen" in l]
        self.assertGreater(sleeps[1], delete)  # the next attempt starts only after the give-back

    def test_infra_takes_all_five_keys_in_the_fixed_order_one_token_one_run(self):
        out = run(ACTION, ACTION="take", LOCK_SCOPE="infra", GITHUB_REPOSITORY=INFRA)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        order = [l.split("--key ")[1].split()[0] for l in out.calls.splitlines() if "put-object" in l]
        self.assertEqual(order, [LEGACY, INFRA_KEY, BACKEND, WEB, MCP])
        records = [json.loads(v) for v in out.keys.values()]
        self.assertEqual(len({(r["token"], r["run_id"], r["scope"]) for r in records}), 1)
        self.assertEqual(records[0]["scope"], "infra")

    def test_infras_partial_take_releases_every_key_it_took_in_reverse_then_waits_holding_nothing(self):
        web = record("OnePeek/onepeek-web", "88", scope="web", phase="rolling")
        out = run(ACTION, keys={WEB: web}, ACTION="take", LOCK_SCOPE="infra", GITHUB_REPOSITORY=INFRA, LOCK_WAIT_CAP_SECONDS="1")
        self.assertEqual(out.returncode, 0)  # at the cap the step passes; "Wait cap reached (re-run)" fails the job
        self.assertIn("capped=true", out.output)
        deletes = [l.split("--key ")[1].split()[0] for l in out.calls.splitlines() if "delete-object" in l]
        self.assertEqual(deletes[:3], [BACKEND, INFRA_KEY, LEGACY])
        self.assertEqual(out.keys, {WEB: web})
        self.assertIn("::error::waiting for OnePeek/onepeek-web (web, run 88); re-run", out.stdout)

    def test_a_key_whose_run_has_finished_is_taken_over_by_its_phase_locked_or_done(self):
        for phase in ("locked", "done"):
            out = run(TAKE, keys={BACKEND: record(REPO, "77", scope="backend", phase=phase)}, gh="echo completed")
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            self.assertIn('delete-object --bucket onepeek-preprod-deploy --key locks/backend.json --if-match "etag-backend.json"', out.calls)
            self.assertEqual(json.loads(out.keys[BACKEND])["run_id"], RUN)

    def test_a_rolling_key_is_never_taken_over_and_every_take_names_its_run_and_cancel_release(self):
        held = record(REPO, "77", scope="backend", phase="rolling", previous={"api": "arn:aws:ecs:ap-south-1:111111111111:task-definition/x:1"})
        for extra in ({"gh": "echo completed"}, {"GITHUB_RUN_ID": "77", "GITHUB_RUN_ATTEMPT": "2"}):
            out = run(TAKE, keys={BACKEND: held}, **extra)
            self.assertEqual(out.returncode, 1)
            self.assertIn("held rolling by https://github.com/OnePeek/backend-service/actions/runs/77", out.stdout)
            self.assertIn("cancel-release.yml", out.stdout)
            self.assertNotIn("delete-object --bucket onepeek-preprod-deploy --key locks/backend.json", out.calls)
            self.assertEqual(out.keys[BACKEND], held)
            self.assertNotIn(LEGACY, out.keys)  # given back

    def test_a_migrating_key_with_a_running_task_is_never_taken_over(self):
        held = record(REPO, "77", scope="backend", phase="migrating", migrate_task=TASK)
        out = run(TAKE, keys={BACKEND: held}, gh="echo completed", AWS_DESCRIBE_TASKS=tasks((TASK, MIGRATE_TD, "RUNNING")))
        self.assertEqual(out.returncode, 1)
        self.assertIn(f"::error::waiting for {TASK} (backend, run 77); re-run", out.stdout)
        self.assertEqual(out.keys, {BACKEND: held})
        self.assertNotIn("ecs stop-task", out.calls)

    def test_a_migrating_key_whose_task_stopped_or_is_missing_is_taken_over(self):
        held = record(REPO, "77", scope="backend", phase="migrating", migrate_task=TASK)
        stopped = tasks((TASK, MIGRATE_TD, "STOPPED"))
        missing = json.dumps({"tasks": [], "failures": [{"arn": TASK, "reason": "MISSING"}]})
        for answer in (stopped, missing):
            out = run(TAKE, keys={BACKEND: held}, gh="echo completed", AWS_DESCRIBE_TASKS=answer)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            self.assertEqual(json.loads(out.keys[BACKEND])["run_id"], RUN)

    def test_the_migrate_lookup_uses_cluster_and_started_by_only_and_filters_the_family_from_describe_tasks(self):
        held = record(REPO, "77", "2", scope="backend", phase="migrating")
        other = "arn:aws:ecs:ap-south-1:111111111111:task-definition/onepeek-preprod-ops:3"
        out = run(TAKE, keys={BACKEND: held}, gh="echo completed", AWS_LIST_TASKS=TASK, AWS_DESCRIBE_TASKS=tasks((TASK, other, "RUNNING")))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)   # another family's task is not the migration
        self.assertIn("aws ecs list-tasks --cluster onepeek-preprod --started-by 77-2 --query taskArns[] --output text", out.calls)
        listing = next(l for l in out.calls.splitlines() if "list-tasks" in l)
        self.assertNotIn("--family", listing)
        out = run(TAKE, keys={BACKEND: held}, gh="echo completed", AWS_LIST_TASKS=TASK, AWS_DESCRIBE_TASKS=tasks((TASK, MIGRATE_TD, "RUNNING")))
        self.assertEqual(out.returncode, 1)
        self.assertEqual(out.keys, {BACKEND: held})

    def test_no_migrate_task_in_two_lookups_means_stopped(self):
        held = record(REPO, "77", scope="backend", phase="migrating")
        out = run(TAKE, keys={BACKEND: held}, gh="echo completed", AWS_LIST_TASKS="None")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(out.calls.count("ecs list-tasks"), 2)

    def test_an_earlier_attempt_of_this_run_is_taken_over_without_asking_github(self):
        out = run(TAKE, keys={BACKEND: record(REPO, RUN, "1", scope="backend")}, GITHUB_RUN_ATTEMPT="2")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertNotIn("gh token", out.calls)
        self.assertEqual(json.loads(out.keys[BACKEND])["run_attempt"], "2")

    def test_the_same_attempt_of_this_run_is_not_taken_over(self):
        held = record(REPO, RUN, "1", scope="backend")
        out = run(TAKE, keys={BACKEND: held})
        self.assertEqual(out.returncode, 1)
        self.assertEqual(out.keys, {BACKEND: held})

    def test_a_legacy_release_json_record_with_no_scope_is_read_too(self):
        # Written by a caller still on the older tag: no scope and no phase; it counts as locked, scope from its repo.
        held = record("OnePeek/onepeek-web", "88")
        out = run(TAKE, keys={LEGACY: held}, LOCK_WAIT_CAP_SECONDS="1")
        self.assertEqual(out.returncode, 1)
        self.assertIn("::error::waiting for OnePeek/onepeek-web (web, run 88); re-run", out.stdout)
        out = run(TAKE, keys={LEGACY: held}, gh="echo completed")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_a_holder_whose_state_cannot_be_read_counts_as_running(self):
        held = record(scope="infra")
        out = run(TAKE, keys={BACKEND: held}, gh='echo "HTTP 404" >&2; exit 1', LOCK_WAIT_CAP_SECONDS="1")
        self.assertEqual(out.returncode, 1)
        self.assertIn("counts as running", out.stdout + out.stderr)
        self.assertEqual(out.keys, {BACKEND: held})

    def test_an_unreadable_lock_means_no_deploy(self):
        out = run(TAKE, keys={BACKEND: record()}, FAIL_GET_OBJECT="AccessDenied")
        self.assertEqual(out.returncode, 1)
        self.assertNotIn("delete-object", out.calls)

    def test_a_record_that_is_not_a_lock_means_no_deploy_and_is_left_in_place(self):
        for junk in ("not json", json.dumps({"repo": "x"}), record(repo="../../evil"), record(phase="half"), record(migrate_task="; rm -rf /")):
            out = run(TAKE, keys={BACKEND: junk}, gh="echo completed")
            self.assertEqual(out.returncode, 1, junk)
            self.assertNotIn("delete-object --bucket onepeek-preprod-deploy --key locks/backend.json", out.calls)
            self.assertEqual(out.keys[BACKEND], junk)

    def test_any_other_write_error_means_no_deploy(self):
        out = run(TAKE, FAIL_PUT_OBJECT="AccessDenied")
        self.assertEqual(out.returncode, 1)
        self.assertIn("could not be written, so nothing is deployed", out.stdout)
        self.assertEqual(out.keys, {})

    def test_a_concurrent_write_conflict_is_read_again_not_fatal(self):
        out = run(TAKE, keys={LEGACY: record()}, FAIL_PUT_OBJECT="ConditionalRequestConflict", LOCK_WAIT_CAP_SECONDS="1")
        self.assertEqual(out.returncode, 1)
        self.assertIn("waiting for", out.stdout)
        self.assertIn("get-object", out.calls)

    def test_another_run_taking_the_lock_during_a_takeover_is_respected(self):
        other = record("OnePeek/onepeek-web", "88")
        out = run(TAKE, keys={LEGACY: record()}, gh='case "$*" in *runs/77*) echo completed;; *) echo in_progress;; esac', RACE_ON_DELETE=other,
                  LOCK_WAIT_CAP_SECONDS="1")
        self.assertEqual(out.returncode, 1)
        self.assertEqual(out.keys[LEGACY], other)
        self.assertIn("waiting for OnePeek/onepeek-web (web, run 88); re-run", out.stdout)

    def test_a_failed_delete_means_no_deploy(self):
        out = run(TAKE, keys={LEGACY: record()}, gh="echo completed", FAIL_DELETE_OBJECT="AccessDenied")
        self.assertEqual(out.returncode, 1)
        self.assertIn("could not be removed", out.stdout)
        self.assertEqual(out.keys[LEGACY], record())


OPENSSL = '#!/bin/bash\necho "openssl $*" >> "$FAKE_S3/calls"\ncat >/dev/null\nprintf sig\n'
CURL = """#!/bin/bash
echo "curl $*" >> "$FAKE_S3/calls"
prev=""
for a in "$@"; do if [ "$prev" = "-H" ] && [ "${a#@}" != "$a" ]; then cat "${a#@}" >> "$FAKE_S3/headers"; fi; prev="$a"; done
case "$*" in
*access_tokens*) cat >> "$FAKE_S3/token-request"; echo >> "$FAKE_S3/token-request"; echo '{"token": "new-app-token"}';;
*installation*) echo '{"id": 42}';;
*audience=sts*) echo '{"value": "oidc-jwt"}';;
esac
"""


class Remint(unittest.TestCase):
    """Playbook §5.4a rule 4: every 50 minutes inside a wait, a new App token and a new role session."""

    def held_run(self, **extra):
        return run(TAKE, keys={BACKEND: record(scope="infra")}, stubs={"openssl": OPENSSL, "curl": CURL},
                   **{"LOCK_REMINT_SECONDS": "1", **extra}, LOCK_WAIT_CAP_SECONDS="3", LOCK_APP_CLIENT_ID="Iv1.client", LOCK_APP_PRIVATE_KEY="-----PRIVATE KEY SECRET-----",
                   LOCK_APP_REPOS="backend-service,onepeek-web,onepeek-infra,mcp-athena", LOCK_APP_PERMISSIONS='{"actions": "read", "contents": "read"}',
                   LOCK_ROLE_ARN="arn:aws:iam::111111111111:role/onepeek-preprod-deploy-backend-service", LOCK_ROLE_SESSION="backend-service-5000",
                   ACTIONS_ID_TOKEN_REQUEST_URL="https://token?x=1", ACTIONS_ID_TOKEN_REQUEST_TOKEN="req",
                   AWS_ASSUME_ROLE_WITH_WEB_IDENTITY="AKIA sk st")  # fmt: skip

    def test_a_wait_longer_than_the_interval_uses_a_fresh_app_token_and_role_session(self):
        out = self.held_run()
        self.assertIn("Authorization: Bearer ", out.headers)
        self.assertIn("/repos/OnePeek/backend-service/installation", out.calls)
        self.assertIn("/app/installations/42/access_tokens", out.calls)
        self.assertIn("aws sts assume-role-with-web-identity --role-arn arn:aws:iam::111111111111:role/onepeek-preprod-deploy-backend-service "
                      "--role-session-name backend-service-5000 --web-identity-token oidc-jwt", out.calls)
        gh = [l for l in out.calls.splitlines() if l.startswith("gh token=")]
        # The last reads use the new token (the first may already come after a mint on a slow machine).
        self.assertTrue(gh[-1].startswith("gh token=new-app-token "), gh)

    def test_the_new_token_has_the_same_repositories_and_permissions_and_the_key_is_never_printed(self):
        out = self.held_run()
        self.assertNotIn("PRIVATE KEY", out.stdout + out.stderr + out.calls)
        # Every bearer token reaches curl as a header file, never on its command line.
        self.assertIn("Authorization: Bearer eyJ", out.headers)
        self.assertIn("Authorization: bearer req", out.headers)
        self.assertNotIn("Authorization", out.calls)
        self.assertNotIn("eyJ", out.calls)
        self.assertIn("::add-mask::new-app-token", out.stdout)
        self.assertIn("::add-mask::st", out.stdout)
        self.assertEqual(json.loads(out.token_request.splitlines()[0]), {"repositories": ["backend-service", "onepeek-web", "onepeek-infra", "mcp-athena"],
                                                         "permissions": {"actions": "read", "contents": "read"}})

    def test_no_new_mint_before_the_interval(self):
        out = self.held_run(LOCK_REMINT_SECONDS="3000")
        self.assertNotIn("assume-role-with-web-identity", out.calls)
        self.assertNotIn("curl", out.calls)

    def test_the_interval_is_below_the_shortest_credential_lifetime(self):
        for doc in (RELEASE, OPERATE, LOCKWF):
            self.assertEqual(doc["env"]["LOCK_REMINT_SECONDS"], "3000")
        for doc, wf in ((RELEASE, "release-ecs"), (OPERATE, "operate"), (LOCKWF, "release-lock")):
            take = next(s for s in doc["jobs"][LOCK_JOB[wf]]["steps"] if s.get("id") in ("take", "lock"))
            self.assertEqual(take["env"]["LOCK_APP_PRIVATE_KEY"], "${{ secrets.AGENTS_APP_PRIVATE_KEY }}")
            self.assertEqual(take["env"]["LOCK_APP_REPOS"], "backend-service,onepeek-web,onepeek-infra,mcp-athena")
            app = next(s for s in doc["jobs"][LOCK_JOB[wf]]["steps"] if s.get("id") == "app")
            self.assertEqual(take["env"]["LOCK_APP_REPOS"], app["with"]["repositories"].split(" #")[0])
            # Exactly actions and contents read, on exactly the four lock repos, at the mint and the re-mint (playbook §5.4a
            # "The run's GitHub reads and their grants").
            perms = {k.removeprefix("permission-"): v for k, v in app["with"].items() if k.startswith("permission-")}
            self.assertEqual(perms, {"actions": "read", "contents": "read"}, wf)
            self.assertEqual(json.loads(take["env"]["LOCK_APP_PERMISSIONS"]), perms, wf)
            self.assertEqual(sorted(app["with"]["repositories"].split(" #")[0].split(",")),
                             ["backend-service", "mcp-athena", "onepeek-infra", "onepeek-web"])


class PhaseWrites(unittest.TestCase):
    """Only the holder writes its record, with If-Match on the ETag it read (playbook §5.4a "Keys")."""

    def test_a_phase_is_written_into_every_key_it_holds_with_if_match(self):
        out = run(call('lock_write_phase rolling \'{"previous": {"api": "x"}}\''), keys={LEGACY: mine(), BACKEND: mine()}, LOCK_TOKEN=TOKEN)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn('put-object --bucket onepeek-preprod-deploy --key locks/release.json --body lock-phase-mine.json --if-match "etag-release.json"', out.calls)
        self.assertIn('--key locks/backend.json --body lock-phase-mine.json --if-match "etag-backend.json"', out.calls)
        for key in (LEGACY, BACKEND):
            rec = json.loads(out.keys[key])
            self.assertEqual((rec["phase"], rec["previous"], rec["token"]), ("rolling", {"api": "x"}, TOKEN))

    def test_locked_and_done_drop_what_there_was_to_undo(self):
        out = run(call("lock_write_phase done"), keys={LEGACY: mine("rolling", previous={"a": "b"}), BACKEND: mine("migrating", migrate_task=TASK)}, LOCK_TOKEN=TOKEN)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual({k: json.loads(v).get("previous") or json.loads(v).get("migrate_task") for k, v in out.keys.items()}, {LEGACY: None, BACKEND: None})

    def test_a_record_that_is_not_its_own_means_the_key_is_lost_and_the_run_stops(self):
        for held in (record(), mine(repair_run="9-1", repair_token="d" * 32)):
            out = run(call("lock_write_phase rolling", "echo changed-something"), keys={LEGACY: held, BACKEND: held}, LOCK_TOKEN=TOKEN)
            self.assertEqual(out.returncode, 1)
            self.assertIn("lost the release lock", out.stdout)
            self.assertNotIn("changed-something", out.stdout)
            self.assertEqual(out.puts, "")

    def test_a_412_stops_the_run(self):
        out = run(call("lock_write_phase rolling", "echo changed-something"), keys={LEGACY: mine(), BACKEND: mine()}, LOCK_TOKEN=TOKEN,
                  CHANGE_BEFORE_PUT=mine(repair_run="9-1"))
        self.assertEqual(out.returncode, 1)
        self.assertIn("lost the release lock", out.stdout)
        self.assertNotIn("changed-something", out.stdout)

    def test_a_409_is_retried_after_a_re_read_up_to_3_times(self):
        out = run(call("lock_write_phase rolling"), keys={LEGACY: mine(), BACKEND: mine()}, LOCK_TOKEN=TOKEN, CONFLICT_PUTS="2")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(json.loads(out.keys[LEGACY])["phase"], "rolling")
        self.assertGreaterEqual(out.calls.count("get-object --bucket onepeek-preprod-deploy --key locks/release.json"), 3)
        out = run(call("lock_write_phase rolling"), keys={LEGACY: mine(), BACKEND: mine()}, LOCK_TOKEN=TOKEN, CONFLICT_PUTS="9")
        self.assertEqual(out.returncode, 1)
        self.assertEqual(json.loads(out.keys[LEGACY])["phase"], "locked")


PREV = {"api": "arn:aws:ecs:ap-south-1:111111111111:task-definition/onepeek-preprod-api:6",
        "worker": "arn:aws:ecs:ap-south-1:111111111111:task-definition/onepeek-preprod-worker:3"}


class Finish(unittest.TestCase):
    """finish acts on the record's phase, never on cancelled() (playbook §5.4a "Rollback on cancel"; E2, E3, E10, E11)."""

    def finish(self, held: str, **extra):
        return run(FINISH, keys={LEGACY: held, BACKEND: held}, **{"LOCK_TOKEN": TOKEN, **extra})

    def test_locked_or_done_unlocks_every_key(self):
        for phase in ("locked", "done"):
            out = self.finish(mine(phase))
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            self.assertEqual(out.keys, {})
            self.assertNotIn("update-service", out.calls)

    def test_migrating_waits_for_the_task_never_stops_it_and_keeps_the_key_while_it_runs(self):
        held = mine("migrating", migrate_task=TASK)
        out = self.finish(held, AWS_DESCRIBE_TASKS=tasks((TASK, MIGRATE_TD, "RUNNING")))
        self.assertEqual(out.returncode, 1)
        self.assertIn(f"The migrate task {TASK} has not stopped", out.stdout)
        self.assertEqual(out.keys, {LEGACY: held, BACKEND: held})
        self.assertNotIn("stop-task", out.calls)

    def test_migrating_with_the_task_stopped_reports_its_exit_code_and_unlocks(self):
        out = self.finish(mine("migrating", migrate_task=TASK),
                          SEQ_DESCRIBE_TASKS=json.dumps([tasks((TASK, MIGRATE_TD, "RUNNING")), tasks((TASK, MIGRATE_TD, "STOPPED")), "0"]))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("exit code 0", out.summary)
        self.assertEqual(out.keys, {})

    def test_rolling_puts_every_service_back_then_unlocks(self):
        out = self.finish(mine("rolling", previous=PREV, previous_lambda={"onepeek-preprod-sandbox-parse": "7"}), GH_TOKEN="gh")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        for svc, td in PREV.items():
            self.assertIn(f"aws ecs update-service --cluster onepeek-preprod --service {svc} --task-definition {td}", out.calls)
        self.assertIn("aws ecs wait services-stable --cluster onepeek-preprod --services api worker", out.calls)
        self.assertIn("aws lambda update-alias --function-name onepeek-preprod-sandbox-parse --name live --function-version 7", out.calls)
        self.assertIn("gh issue create --repo OnePeek/backend-service", out.calls)
        self.assertEqual(out.keys, {})

    def test_a_failed_put_back_keeps_the_key_rolling_and_names_cancel_release(self):
        held = mine("rolling", previous=PREV)
        for fail in ({"FAIL_UPDATE_SERVICE": "ServiceNotFound"}, {"FAIL_WAIT": "Waiter failed"}):
            out = self.finish(held, **fail)
            self.assertEqual(out.returncode, 1)
            self.assertIn("cancel-release.yml", out.stdout)
            self.assertEqual(out.keys, {LEGACY: held, BACKEND: held})

    def test_it_matches_run_id_and_run_attempt_when_the_token_output_is_lost(self):
        out = self.finish(mine("rolling", previous=PREV), LOCK_TOKEN="")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("update-service", out.calls)
        self.assertEqual(out.keys, {})
        held = mine("rolling", previous=PREV)
        out = self.finish(held, LOCK_TOKEN="", GITHUB_RUN_ATTEMPT="2")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertNotIn("update-service", out.calls)
        self.assertEqual(out.keys, {LEGACY: held, BACKEND: held})

    def test_a_record_being_repaired_or_of_another_token_is_left_alone(self):
        for held in (mine("rolling", previous=PREV, repair_run="9-1", repair_token="d" * 32), record(REPO, RUN, scope="backend", phase="rolling", previous=PREV)):
            out = self.finish(held)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            self.assertNotIn("update-service", out.calls)
            self.assertNotIn("delete-object", out.calls)
            self.assertEqual(out.keys, {LEGACY: held, BACKEND: held})

    def test_no_lock_is_fine_and_an_unreadable_one_fails_visibly(self):
        out = run(FINISH, LOCK_TOKEN=TOKEN)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        out = self.finish(mine(), FAIL_GET_OBJECT="AccessDenied")
        self.assertEqual(out.returncode, 1)
        self.assertNotIn("delete-object", out.calls)

    def test_a_key_taken_over_meanwhile_is_left_in_place(self):
        other = record("OnePeek/onepeek-web", "88")
        out = self.finish(mine(), RACE_ON_DELETE=other)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(out.keys[BACKEND], other)

    def test_finish_never_reads_cancelled_and_verify_never_starts_in_a_cancelled_run(self):
        finish = RELEASE["jobs"]["finish"]
        self.assertEqual(finish["if"], "${{ always() && needs.prepare.result != 'skipped' && inputs.environment != 'prod' }}")
        self.assertNotIn("cancelled()", json.dumps(finish))
        self.assertIn("!cancelled()", RELEASE["jobs"]["verify"]["if"])
        self.assertNotIn("cancelled()", json.dumps(OPERATE["jobs"]["unlock"]))
        end = step_of(RELEASE, "verify", "Record the end in the release lock")
        self.assertIn("!cancelled()", end["if"])


class SharedLockWorkflow(unittest.TestCase):
    """release-lock.yml (playbook §5.4 "Shared lock workflow", §5.4a): only infra calls it."""

    def test_take_outputs_the_token_and_release_removes_every_key(self):
        out = run(ACTION, ACTION="take", LOCK_SCOPE="infra", GITHUB_REPOSITORY=INFRA)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        token = json.loads(out.keys[INFRA_KEY])["token"]
        self.assertIn("token=" + token, out.output)
        held = record(INFRA, RUN, token=token, scope="infra", phase="locked")
        out = run(ACTION, keys={k: held for k in (LEGACY, INFRA_KEY, BACKEND, WEB, MCP)}, ACTION="release", GIVEN_TOKEN=token,
                  LOCK_SCOPE="infra", GITHUB_REPOSITORY=INFRA)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(out.keys, {})
        deletes = [l.split("--key ")[1].split()[0] for l in out.calls.splitlines() if "delete-object" in l]
        self.assertEqual(deletes, [MCP, WEB, BACKEND, INFRA_KEY, LEGACY])

    def test_the_interface_and_the_scope_accepts_only_infra(self):
        call_ = LOCKWF[True]["workflow_call"]
        self.assertEqual(set(call_["inputs"]), {"runner", "environment", "action", "role-to-assume", "token", "scope"})
        self.assertTrue(call_["inputs"]["scope"]["required"])
        self.assertEqual(LOCKWF["env"]["LOCK_SCOPE"], "${{ inputs.scope }}")
        self.assertEqual(call_["outputs"]["token"]["value"], "${{ jobs.lock.outputs.token }}")
        self.assertTrue(all(s["required"] for s in call_["secrets"].values()))
        check = job_step(LOCKWF, "lock", "Check the action")
        for scope, action, token, code in (("infra", "take", "", 0), ("infra", "thaw", "", 0), ("infra", "release", "", 1), ("infra", "release", TOKEN, 0),
                                           ("infra", "unlock", "", 1), ("backend", "take", "", 1), ("", "take", "", 1)):
            r = subprocess.run(["bash", "-eo", "pipefail", "-c", check], capture_output=True, text=True,
                               env={**os.environ, "ACTION": action, "GIVEN_TOKEN": token, "SCOPE": scope})  # fmt: skip
            self.assertEqual(r.returncode, code, (scope, action))
        self.assertNotIn("concurrency", LOCKWF)
        self.assertEqual(LOCKWF["jobs"]["lock"]["permissions"]["actions"], "read")

    def test_at_the_wait_cap_the_step_records_capped_and_passes_holding_nothing(self):
        web = record("OnePeek/onepeek-web", "88", scope="web", phase="locked")
        out = run(ACTION, keys={WEB: web}, ACTION="take", LOCK_SCOPE="infra", GITHUB_REPOSITORY=INFRA, LOCK_WAIT_CAP_SECONDS="1")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("::error::waiting for OnePeek/onepeek-web (web, run 88); re-run", out.stdout)
        self.assertIn("capped=true", out.output.splitlines())
        self.assertEqual(out.keys, {WEB: web})

    def test_a_failed_give_back_records_no_cap_and_fails_the_step(self):
        # Ruling R-S2l0-2 (g97 m4): a key that could not be given back may still name this run, so it is no clean cap.
        web = record("OnePeek/onepeek-web", "88", scope="web", phase="locked")
        out = run(ACTION, keys={WEB: web}, ACTION="take", LOCK_SCOPE="infra", GITHUB_REPOSITORY=INFRA, LOCK_WAIT_CAP_SECONDS="1",
                  FAIL_DELETE_OBJECT="AccessDenied")
        self.assertNotEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertNotIn("capped", out.output)
        self.assertIn("could not be given back", out.stdout)

    def test_any_other_failure_still_fails_the_step_and_records_no_cap(self):
        out = run(ACTION, keys={INFRA_KEY: record()}, ACTION="take", LOCK_SCOPE="infra", GITHUB_REPOSITORY=INFRA, FAIL_GET_OBJECT="AccessDenied")
        self.assertNotEqual(out.returncode, 0)
        self.assertNotIn("capped", out.output)
        out = run(ACTION, ACTION="take", LOCK_SCOPE="infra", GITHUB_REPOSITORY=INFRA)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertNotIn("capped", out.output)

    def test_the_cap_is_a_step_of_its_own_named_wait_cap_reached_re_run(self):
        steps = LOCKWF["jobs"]["lock"]["steps"]
        names = [s.get("name", s.get("uses", "")) for s in steps]
        at = names.index("Wait cap reached (re-run)")
        self.assertEqual(steps[at - 1].get("id"), "lock")
        cap = steps[at]
        self.assertEqual(cap["if"], "${{ steps.lock.outputs.capped == 'true' }}")
        r = subprocess.run(["bash", "-eo", "pipefail", "-c", cap["run"]], capture_output=True, text=True, env={**os.environ, "ENVIRONMENT": "preprod"})
        self.assertEqual(r.returncode, 1)
        # Any later step that runs under always() or failure() must not read the waiting step's success as a take.
        for later in steps[at + 1:]:
            cond = str(later.get("if", ""))
            if "always()" in cond or "failure()" in cond:
                self.assertIn("steps.lock.outputs.capped != 'true'", cond, later.get("name"))
        self.assertEqual(sum(n == "Wait cap reached (re-run)" for n in names), 1)

    def test_its_limit_waits_only_for_take_and_freeze(self):
        # freeze waits for a live infra.json holder under the same cap as take (playbook §5.4a "Freeze and thaw"):
        # the cap plus 15; release and thaw never wait.
        expr = LOCKWF["jobs"]["lock"]["timeout-minutes"]
        self.assertEqual({a: evaluate(expr, action=a) for a in ("take", "release", "freeze", "thaw")},
                         {"take": 135, "release": 10, "freeze": 135, "thaw": 10})


def evaluate(expr: str, **inputs: str):
    """A timeout-minutes expression of inputs.* comparisons, &&, || and integers, evaluated for the given inputs."""
    body = re.fullmatch(r"\$\{\{(.*)\}\}", expr.strip(), re.S).group(1)
    body = re.sub(r"inputs\.([\w-]+)", lambda m: repr(inputs[m.group(1)]), body).replace("&&", " and ").replace("||", " or ")
    return eval(body, {"__builtins__": {}})  # noqa: S307 - only the shipped expression, already held to that form by the lint


def needs(doc: dict, job: str) -> list[str]:
    n = doc["jobs"][job].get("needs", [])
    return [n] if isinstance(n, str) else n


# The jobs that change something: build (signs and records the digest), deploy (migrate, assets, rollout, ops),
# verify (rollback) and finish (the deployment record). build-web shares build-image's steps.
GUARDED = ["build-image", "build-web", "deploy", "verify", "finish"]


class Wiring(unittest.TestCase):
    """The lock is taken by the first job, before any build, and ended by an always() job (playbook §5.4)."""

    def test_release_ecs_takes_the_lock_before_anything_else(self):
        self.assertEqual(needs(RELEASE, "prepare"), [])
        names = [s.get("name", "") for s in RELEASE["jobs"]["prepare"]["steps"]]
        take = names.index("Take the release lock")
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
        self.assertEqual(set(needs(RELEASE, "finish")), set(RELEASE["jobs"]) - {"finish", "api-client"})
        self.assertIn("lock_finish", FINISH)
        steps = finish["steps"]
        unlock = next(i for i, s in enumerate(steps) if s.get("name") == "Release the release lock")
        self.assertEqual(steps[unlock]["if"], "${{ always() }}")
        self.assertTrue(steps[unlock - 1]["uses"].startswith("aws-actions/configure-aws-credentials@"))
        self.assertEqual(steps[unlock - 1]["if"], "${{ always() }}")
        self.assertEqual(finish["permissions"]["issues"], "write")   # the put-back opens an issue
        for doc, name in ((RELEASE, "release-ecs"), (OPERATE, "operate")):
            rel = next(s for s in doc["jobs"][UNLOCK_JOB[name]]["steps"] if s.get("name") == "Release the release lock")
            self.assertEqual(rel["env"]["LOCK_TOKEN"], "${{ needs.%s.outputs.token }}" % LOCK_JOB[name])
            self.assertEqual(rel["env"]["GH_TOKEN"], "${{ github.token }}")
            self.assertEqual(doc["jobs"][LOCK_JOB[name]]["outputs"]["token"], "${{ steps.take.outputs.token }}")

    def test_the_finish_guard_is_skipped_when_prepare_took_no_lock(self):
        # Then the only failed job is prepare, with the re-run message (playbook §5.4a rule 3).
        guard = RELEASE["jobs"]["finish"]["steps"][0]
        self.assertEqual(guard["if"], "${{ always() && needs.prepare.outputs.attempt != '' }}")

    def test_the_lock_token_reaches_only_the_steps_that_write_or_end_the_record(self):
        uses = sorted((job, s["name"]) for job, j in RELEASE["jobs"].items() for s in j.get("steps", [])
                      if "needs.prepare.outputs.token" in json.dumps(s.get("env", {})))
        self.assertEqual([j for j, _ in uses], ["deploy", "deploy", "finish", "verify"])
        self.assertEqual(RELEASE_TEXT.count("needs.prepare.outputs.token"), 4)

    def test_operate_takes_and_ends_the_lock_around_its_one_job(self):
        self.assertEqual(needs(OPERATE, "operate"), ["lock"])
        self.assertEqual(set(needs(OPERATE, "unlock")), {"lock", "operate"})
        self.assertEqual(OPERATE["jobs"]["unlock"]["if"], "${{ always() && needs.lock.result != 'skipped' }}")
        self.assertIn("lock_take", job_step(OPERATE, "lock", "Take the release lock"))
        self.assertIn("lock_finish", job_step(OPERATE, "unlock", "Release the release lock"))
        self.assertEqual(OPERATE["jobs"]["unlock"]["permissions"]["issues"], "write")

    def test_no_concurrency_group_queues_a_release(self):
        for doc in (RELEASE, OPERATE, LOCKWF):
            self.assertNotIn("concurrency", doc)
            for name, job in doc["jobs"].items():
                self.assertNotIn("concurrency", job, name)

    def test_the_waits_and_their_limits(self):
        for doc in (RELEASE, OPERATE, LOCKWF):
            env = doc["env"]
            self.assertEqual((env["LOCK_WAIT_SECONDS"], env["LOCK_WAIT_CAP_SECONDS"], env["LOCK_QUEUED_WAIT_SECONDS"], env["LOCK_MIGRATE_WAIT_SECONDS"],
                              env["LOCK_POLL_SECONDS"]), ("1800", "7200", "3600", "900", "30"))
        self.assertEqual(RELEASE["jobs"]["prepare"]["timeout-minutes"], 135)
        self.assertEqual(RELEASE["jobs"]["finish"]["timeout-minutes"], 60)
        self.assertEqual(OPERATE["jobs"]["lock"]["timeout-minutes"], 135)
        self.assertEqual(OPERATE["jobs"]["unlock"]["timeout-minutes"], 60)

    def test_the_keys_are_per_environment_in_its_deploy_bucket(self):
        self.assertIn('LOCK_BUCKET="${NAME}-deploy"', TAKE)
        self.assertIn('LOCK_LEGACY_KEY="locks/release.json"', TAKE)

    def test_the_app_secrets_are_required_and_their_token_reads_the_holders_state(self):
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

    def test_deploy_from_dev_is_off_unless_the_input_turns_it_on(self):
        script = job_step(RELEASE, "prepare", "Deploy from dev is off")
        self.assertIs(RELEASE[True]["workflow_call"]["inputs"]["deploy-from-dev"]["default"], False)
        for ref, flag, code in (("refs/heads/dev", "false", 1), ("refs/heads/dev", "true", 0), ("refs/heads/release", "false", 0)):
            r = subprocess.run(["bash", "-eo", "pipefail", "-c", script], capture_output=True, text=True,
                               env={**os.environ, "GITHUB_REF": ref, "DEPLOY_FROM_DEV": flag})  # fmt: skip
            self.assertEqual(r.returncode, code, (ref, flag))

    def test_prod_is_promote_only(self):
        script = job_step(RELEASE, "prepare", "Prod is promote-only")
        refused = subprocess.run(["bash", "-eo", "pipefail", "-c", script], capture_output=True, text=True, env={**os.environ, "ENVIRONMENT_NAME": "prod"})
        self.assertEqual(refused.returncode, 1)
        self.assertIn("promote.yml", refused.stdout)

    def test_operate_runs_from_release_only_until_launch(self):
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

    def test_the_reusable_db_bootstrap_call_is_skipped_outside_the_locking_attempt(self):
        self.assertIn("needs.prepare.outputs.attempt == format('{0}', github.run_attempt)", RELEASE["jobs"]["db-bootstrap"]["if"])

    def test_the_check_fails_on_another_attempt_and_passes_on_the_locking_one(self):
        script = self.guard(RELEASE, "deploy")["run"]
        for locked, attempt, code in (("1", "1", 0), ("1", "2", 1), ("", "1", 1)):
            out = subprocess.run(["bash", "-eo", "pipefail", "-c", script], capture_output=True, text=True,
                                 env={**os.environ, "LOCKED_ATTEMPT": locked, "GITHUB_RUN_ATTEMPT": attempt})  # fmt: skip
            self.assertEqual(out.returncode, code, (locked, attempt))

    def test_the_guard_is_the_same_everywhere(self):
        runs = {self.guard(RELEASE, j)["run"] for j in GUARDED} | {self.guard(OPERATE, "operate")["run"]}
        self.assertEqual(len(runs), 1)


class SameLockEverywhere(unittest.TestCase):
    def test_every_step_that_touches_a_key_carries_the_same_lock_functions(self):
        steps = [s["run"] for doc in (RELEASE, OPERATE, LOCKWF) for j in doc["jobs"].values() for s in j.get("steps", []) if "lock_" in str(s.get("run", ""))]
        blocks = {re.search(r"# --- release-lock begin.*?# --- release-lock end ---", s, re.S).group(0) for s in steps}
        self.assertEqual(len(blocks), 1)
        self.assertIn("lock_take", ACTION)
        self.assertEqual(TAKE, job_step(OPERATE, "lock", "Take the release lock"))


INDEX = json.dumps({"onepeek-preprod-api": {"kind": "service", "order": 1, "service": "api", "image": "backend"},
                    "onepeek-preprod-pgbouncer": {"kind": "daemon", "order": 0, "service": "pgbouncer", "image": "backend"},
                    "onepeek-preprod-migrate": {"kind": "one-off", "order": 0, "service": "migrate", "image": "backend"},
                    "onepeek-preprod-web": {"kind": "service", "order": 1, "service": "web", "image": "web"}})  # fmt: skip
ORDER = job_step(RELEASE, "prepare", "Order.")
SCOPE = job_step(OPERATE, "lock", "The service's scope")
OPERATE_TAKE = step_of(OPERATE, "lock", "Take the release lock")


class OperateScope(unittest.TestCase):
    """operate.yml derives the scope from the service's image and refuses another repo's service (playbook §5.4a)."""

    def scope(self, service: str, repo: str = REPO):
        return run(SCOPE, files={"index.json": INDEX}, SERVICE=service, GITHUB_REPOSITORY=repo)

    def test_a_service_of_the_callers_own_image_is_its_scope(self):
        for service in ("api", "pgbouncer", "all"):
            out = self.scope(service)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            self.assertIn("scope=backend", out.output)
        out = self.scope("web", "OnePeek/onepeek-web")
        self.assertIn("scope=web", out.output)

    def test_another_repos_service_and_an_unknown_or_one_off_service_are_refused(self):
        for service, repo, says in (("web", REPO, "run operate.yml there"), ("api", "OnePeek/onepeek-web", "belongs to backend-service"),
                                    ("migrate", REPO, "not one long-running service"), ("nope", REPO, "not one long-running service"),
                                    ("all", "OnePeek/.github", "owns no app scope")):
            out = self.scope(service, repo)
            self.assertEqual(out.returncode, 1, (service, repo))
            self.assertIn(says, out.stdout)
            self.assertNotIn("scope=", out.output)

    def test_the_take_uses_that_scope_and_the_infra_edge(self):
        self.assertEqual(OPERATE_TAKE["env"]["LOCK_SCOPE"], "${{ steps.scope.outputs.scope }}")
        self.assertEqual(OPERATE_TAKE["env"]["LOCK_INFRA_EDGE"], "1")
        self.assertEqual(OPERATE["jobs"]["lock"]["outputs"]["scope"], "${{ steps.scope.outputs.scope }}")
        for job in ("operate", "unlock"):
            env = [s.get("env", {}) for s in OPERATE["jobs"][job]["steps"] if "lock_" in str(s.get("run", ""))]
            self.assertTrue(env and all(e["LOCK_SCOPE"] == "${{ needs.lock.outputs.scope }}" for e in env), job)
        # prepare runs the infra edge's queued-or-running part too (§5.4a "Taking keys and waiting" item 4; E5), until the
        # order check's settled() is built (.github #56); no other step sets it.
        edge = [(j, s.get("id")) for j, job in RELEASE["jobs"].items() for s in job.get("steps", []) if "LOCK_INFRA_EDGE" in (s.get("env") or {})]
        self.assertEqual(edge, [("prepare", "take")])
        self.assertEqual(step_of(RELEASE, "prepare", "Take the release lock")["env"]["LOCK_INFRA_EDGE"], "1")


INFRA_RUNS = 'case "$*" in *workflows/release.yml/runs*) %s;; *) echo in_progress;; esac'


class OperateInfraEdge(unittest.TestCase):
    """An operate.yml take waits while an infra release run is queued or running, or infra.json is held (§5.4a item 1)."""

    def test_an_operate_take_waits_while_an_infra_release_run_is_queued(self):
        out = run(TAKE, gh=INFRA_RUNS % "echo 99", LOCK_INFRA_EDGE="1", LOCK_QUEUED_WAIT_SECONDS="2", LOCK_WAIT_CAP_SECONDS="30")
        self.assertEqual(out.returncode, 1)
        self.assertIn("::error::waiting for OnePeek/onepeek-infra (infra, run 99); re-run", out.stdout)
        self.assertNotIn("put-object", out.calls)
        self.assertLess(out.stdout.count("Waiting for"), 6)   # the 60-minute queued limit, not the cap

    def test_it_waits_while_infra_json_is_held_by_a_live_run(self):
        held = record(INFRA, "77", scope="infra")
        out = run(TAKE, keys={INFRA_KEY: held}, gh=INFRA_RUNS % "echo", LOCK_INFRA_EDGE="1", LOCK_WAIT_CAP_SECONDS="1")
        self.assertEqual(out.returncode, 1)
        self.assertIn("waiting for OnePeek/onepeek-infra (infra, run 77); re-run", out.stdout)
        self.assertEqual(out.keys, {INFRA_KEY: held})

    def test_an_infra_run_that_started_during_the_take_makes_it_give_back_its_keys_and_wait(self):
        counted = 'n=$(cat "$FAKE_S3/n" 2>/dev/null || echo 0); echo $((n+1)) > "$FAKE_S3/n"; [ "$n" -ge 1 ] && echo 99 || echo'
        out = run(TAKE, gh=INFRA_RUNS % counted, LOCK_INFRA_EDGE="1", LOCK_WAIT_CAP_SECONDS="1")
        self.assertEqual(out.returncode, 1)
        self.assertIn("put-object --bucket onepeek-preprod-deploy --key locks/backend.json", out.calls)
        self.assertIn("delete-object --bucket onepeek-preprod-deploy --key locks/backend.json", out.calls)
        self.assertEqual(out.keys, {})

    def test_with_no_infra_run_and_infra_json_free_it_takes(self):
        out = run(TAKE, gh=INFRA_RUNS % "echo", LOCK_INFRA_EDGE="1")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(set(out.keys), {LEGACY, BACKEND})


class Frozen(unittest.TestCase):
    """preprod-frozen (playbook §5.4, §5.6): every scope's take reads it, infra included."""

    def test_a_frozen_environment_refuses_runs_from_other_branches_at_once_with_the_reason(self):
        for repo, ref, scope in ((REPO, "refs/heads/release", "backend"), (INFRA, "refs/heads/release", "infra"),
                                 ("OnePeek/onepeek-web", "refs/heads/hotfix/x", "web")):
            out = run(TAKE, frozen_by=frozen(), GITHUB_REPOSITORY=repo, GITHUB_REF=ref, LOCK_SCOPE=scope)
            self.assertEqual(out.returncode, 1, (repo, ref))
            self.assertIn("preprod is frozen by founder (hotfix hotfix/x) for the hotfix branch OnePeek/backend-service refs/heads/hotfix/x", out.stdout)
            self.assertNotIn("put-object", out.calls)

    def test_any_run_from_the_frozen_hotfix_branch_takes_the_lock(self):
        for run_id in ("90", RUN):
            out = run(TAKE, frozen_by=settled_frozen(), GITHUB_RUN_ID=run_id, GITHUB_REF="refs/heads/hotfix/x")
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            self.assertEqual(json.loads(out.keys[BACKEND])["run_id"], run_id)

    def test_an_unreadable_freeze_means_no_deploy(self):
        for junk in ("not json", json.dumps({"repo": REPO}), frozen(ref="refs/heads/release")):
            out = run(TAKE, frozen_by=junk, GITHUB_REF="refs/heads/release")
            self.assertEqual(out.returncode, 1, junk)
            self.assertNotIn("put-object", out.calls)

    def test_freeze_is_written_from_a_hotfix_branch_naming_it(self):
        out = freeze()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        written = json.loads(out.frozen)
        self.assertEqual((written["repo"], written["ref"], written["who"]), (REPO, "refs/heads/hotfix/x", "founder"))
        self.assertNotIn("token", written)

    def test_freeze_from_any_other_branch_is_refused(self):
        out = run(ACTION, ACTION="freeze", LOCK_SCOPE="infra", GITHUB_REF="refs/heads/dev")
        self.assertEqual(out.returncode, 1)
        self.assertNotIn("put-object", out.calls)

    def test_thaw_removes_the_freeze_on_its_etag(self):
        out = run(ACTION, frozen_by=frozen(), ACTION="thaw", LOCK_SCOPE="infra")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn('delete-object --bucket onepeek-preprod-deploy --key locks/frozen.json --if-match "frozen-etag"', out.calls)
        self.assertIsNone(out.frozen)


# A stand-in for `release_order.py settled --scope infra --json` at the workflow's own commit: prints STUB_SETTLED and
# exits STUB_RC; records its arguments and the GH_TOKEN it was given.
SETTLED_STUB = """import json, os, sys
open("settled-call", "w").write(json.dumps({"argv": sys.argv[1:], "gh_token": os.environ.get("GH_TOKEN")}))
print(os.environ.get("STUB_SETTLED", ""))
sys.exit(int(os.environ.get("STUB_RC", "0")))
"""
SETTLED = json.dumps({"verdict": "settled", "reason": "released", "success_head": INFRA_HEAD, "error": False})
HOTFIX = {"GITHUB_REF": "refs/heads/hotfix/x", "GITHUB_REF_NAME": "hotfix/x", "GITHUB_ACTOR": "founder"}


def freeze(settled: str = SETTLED, files: dict | None = None, **extra):
    env = {"ACTION": "freeze", "LOCK_SCOPE": "infra", "LOCK_ORDER_SCRIPT": "settled.py", "STUB_SETTLED": settled, **HOTFIX, **extra}
    return run(ACTION, files={"settled.py": SETTLED_STUB, **(files or {})}, **env)


def frozen_puts(out) -> list[dict]:
    return [json.loads(line.split(" ", 1)[1]) for line in out.puts.splitlines() if line.startswith("locks/frozen.json ")]


class FreezeAndThaw(unittest.TestCase):
    """Playbook §5.4a "Freeze and thaw": freeze writes frozen.json without infra_head first, waits while a live run holds
    infra.json, then records infra_head and infra_settled from settled(); a hotfix take refuses until then."""

    def test_freeze_writes_frozen_json_without_infra_head_first_then_records_infra_head_and_infra_settled(self):
        out = freeze()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        first, second = frozen_puts(out)
        self.assertNotIn("infra_head", first)
        self.assertNotIn("infra_settled", first)
        self.assertEqual((second["infra_head"], second["infra_settled"]), (INFRA_HEAD, True))
        self.assertEqual({k: second[k] for k in first}, first)
        puts = [l for l in out.calls.splitlines() if "put-object" in l]
        self.assertIn("--if-none-match *", puts[0])
        self.assertRegex(puts[1], r'--if-match "[0-9a-f]{32}"')   # on the ETag of its own first write
        self.assertEqual(json.loads(out.frozen)["infra_head"], INFRA_HEAD)

    def test_settled_is_read_at_the_workflows_own_script_with_the_lock_app_token_for_infra(self):
        with tempfile.TemporaryDirectory() as d:
            calls = Path(d) / "c"
            stub = SETTLED_STUB.replace('"settled-call"', repr(str(calls)))
            out = freeze(files={"settled.py": stub})
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            seen = json.loads(calls.read_text())
        self.assertEqual(seen, {"argv": ["settled", "--scope", "infra", "--json"], "gh_token": "t"})
        step = step_of(LOCKWF, "lock", "Take, release, freeze or thaw")
        self.assertEqual(step["env"]["LOCK_ORDER_SCRIPT"].split()[0], "_gh/scripts/release_order.py")

    def test_every_infra_take_is_refused_from_the_first_write_on(self):
        for record_ in (frozen(), settled_frozen()):
            out = run(ACTION, frozen_by=record_, ACTION="take", LOCK_SCOPE="infra", GITHUB_REPOSITORY=INFRA, GITHUB_REF="refs/heads/release")
            self.assertEqual(out.returncode, 1)
            self.assertIn("::error::The preprod release lock is refused: preprod is frozen by founder", out.stdout)
            self.assertNotIn("put-object", out.calls)

    def test_a_freeze_written_just_after_a_take_makes_the_take_give_back_every_key_and_fail(self):
        # The race of "freeze twice"/"hotfix take racing the first write": the take read frozen.json before the freeze
        # existed; it reads it again after its keys, so a freeze written meanwhile still refuses it.
        out = run(ACTION, ACTION="take", LOCK_SCOPE="infra", GITHUB_REPOSITORY=INFRA, GITHUB_REF="refs/heads/release",
                  FROZEN_AFTER_PUTS="1", FROZEN_BODY=frozen())
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("::error::The preprod release lock is refused: preprod is frozen by founder", out.stdout)
        self.assertEqual(out.keys, {})
        self.assertNotIn("capped", out.output)
        out = run(TAKE, GITHUB_REF="refs/heads/hotfix/x", FROZEN_AFTER_PUTS="2", FROZEN_BODY=frozen())
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("has not recorded infra_head yet", out.stdout)
        self.assertEqual(out.keys, {})

    def test_a_hotfix_take_refuses_while_frozen_json_has_no_infra_head(self):
        out = run(TAKE, frozen_by=frozen(), GITHUB_REF="refs/heads/hotfix/x")
        self.assertEqual(out.returncode, 1)
        self.assertIn("::error::The preprod release lock is refused: preprod is frozen for this hotfix branch OnePeek/backend-service "
                      "refs/heads/hotfix/x, but freeze has not recorded infra_head yet", out.stdout)
        self.assertNotIn("put-object", out.calls)
        out = run(TAKE, frozen_by=frozen(infra_head=INFRA_HEAD, infra_settled=False), GITHUB_REF="refs/heads/hotfix/x")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_a_malformed_infra_head_or_infra_settled_means_no_deploy(self):
        for fields in ({"infra_head": "e" * 39, "infra_settled": True}, {"infra_head": "E" * 40, "infra_settled": True},
                       {"infra_head": INFRA_HEAD}, {"infra_head": INFRA_HEAD, "infra_settled": "true"}, {"infra_head": 7, "infra_settled": True}):
            out = run(TAKE, frozen_by=frozen(**fields), GITHUB_REF="refs/heads/hotfix/x")
            self.assertEqual(out.returncode, 1, fields)
            self.assertIn("is unreadable, so nothing is deployed", out.stdout)
            self.assertNotIn("put-object", out.calls)

    def test_freeze_waits_while_a_live_infra_run_holds_infra_json_and_at_the_cap_records_capped_and_no_infra_head(self):
        held = record(INFRA, "77", scope="infra", phase="locked")
        out = freeze(keys={INFRA_KEY: held}, LOCK_WAIT_CAP_SECONDS="1")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)   # the cap step fails the job
        # The cap is wall-clock time, so a loaded machine may reach it before the first "Freeze waits" line (pitfall 33).
        self.assertIn("::error::waiting for OnePeek/onepeek-infra (infra, run 77); re-run", out.stdout)
        self.assertIn("capped=true", out.output.splitlines())
        self.assertNotIn("infra_head", json.loads(out.frozen))
        self.assertEqual(out.keys, {INFRA_KEY: held})   # the infra run keeps its keys
        self.assertEqual(len(frozen_puts(out)), 1)

    def test_a_holder_whose_run_state_cannot_be_read_counts_as_live(self):
        held = record(INFRA, "77", scope="infra", phase="locked")
        out = freeze(keys={INFRA_KEY: held}, gh="exit 1", LOCK_WAIT_CAP_SECONDS="1")
        self.assertIn("counts as running", out.stdout + out.stderr)
        self.assertIn("capped=true", out.output.splitlines())
        self.assertNotIn("infra_head", json.loads(out.frozen))

    def test_an_infra_release_that_ends_and_unlocks_mid_wait_lets_freeze_record_its_verdict(self):
        # The holder fails and unlocks while freeze waits: freeze then records what settled() says (here: not settled).
        held = record(INFRA, "77", scope="infra", phase="locked")
        unlock = 'n=$(cat "$FAKE_S3/n" 2>/dev/null || echo 0); echo $((n+1)) > "$FAKE_S3/n"; if [ "$n" -ge 1 ]; then rm -f "$FAKE_S3/s3_locks_infra.json"; fi; echo in_progress'
        failed = json.dumps({"verdict": "fail", "reason": "infra's release ended failure", "success_head": INFRA_HEAD, "error": False})
        out = freeze(settled=failed, keys={INFRA_KEY: held}, gh=unlock, LOCK_WAIT_CAP_SECONDS="60")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("Freeze waits: s3://onepeek-preprod-deploy/locks/infra.json is held by https://github.com/OnePeek/onepeek-infra/actions/runs/77", out.stdout)
        rec = json.loads(out.frozen)
        self.assertEqual((rec["infra_head"], rec["infra_settled"], rec["infra_settled_reason"]), (INFRA_HEAD, False, "infra's release ended failure"))

    def test_a_stale_infra_json_whose_run_has_finished_is_not_waited_for_and_is_left_for_the_next_taker(self):
        held = record(INFRA, "77", scope="infra", phase="locked")
        waiting = json.dumps({"verdict": "wait", "reason": "infra.json is held", "success_head": INFRA_HEAD, "error": False})
        out = freeze(settled=waiting, keys={INFRA_KEY: held}, gh="echo completed")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertNotIn("Freeze waits", out.stdout)
        self.assertIn("which is not running: not waited for", out.stdout)
        self.assertEqual(out.keys, {INFRA_KEY: held})
        self.assertIs(json.loads(out.frozen)["infra_settled"], False)

    def test_infra_settled_is_true_only_on_a_clean_settled_verdict(self):
        for verdict, error, want in (("settled", False, True), ("settled", True, False), ("wait", False, False), ("fail", True, False),
                                     ("settled", None, False)):
            doc = json.dumps({"verdict": verdict, "reason": "r", "success_head": INFRA_HEAD, "error": error})
            out = freeze(settled=doc)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            self.assertIs(json.loads(out.frozen)["infra_settled"], want, (verdict, error))

    def test_a_settled_read_error_records_infra_settled_false_with_the_error_never_true(self):
        err = json.dumps({"verdict": "fail", "reason": "read error: GET compare: 502", "success_head": INFRA_HEAD, "error": True})
        out = freeze(settled=err)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        rec = json.loads(out.frozen)
        self.assertEqual((rec["infra_settled"], rec["infra_settled_reason"]), (False, "read error: GET compare: 502"))

    def test_with_no_infra_head_to_record_freeze_fails_and_frozen_json_keeps_refusing_hotfix_takes(self):
        no_head = json.dumps({"verdict": "fail", "reason": "read error: GET runs: 502", "success_head": None, "error": True})
        for settled, extra in ((no_head, {}), ("not json", {}), (SETTLED, {"STUB_RC": "1"}), ("", {}),
                               (json.dumps({**json.loads(SETTLED), "success_head": "e" * 12}), {}), (SETTLED, {"LOCK_ORDER_SCRIPT": "missing.py"})):
            out = freeze(settled=settled, **extra)
            self.assertEqual(out.returncode, 1, (settled, extra))
            self.assertIn("::error::infra_head cannot be recorded", out.stdout)
            self.assertNotIn("capped", out.output)
            self.assertNotIn("infra_head", json.loads(out.frozen))

    def test_freeze_twice_records_once_and_finishes_an_unfinished_freeze_of_its_own_branch(self):
        done = settled_frozen()
        out = freeze(frozen_by=done)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("already frozen for this branch (infra_head " + INFRA_HEAD, out.stdout)
        self.assertEqual(out.frozen, done)
        self.assertEqual(frozen_puts(out), [])
        out = freeze(frozen_by=frozen())
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn('--key locks/frozen.json --body lock-frozen-record.json --if-match "frozen-etag"', out.calls)
        self.assertEqual(json.loads(out.frozen)["infra_head"], INFRA_HEAD)
        out = freeze(frozen_by=frozen(ref="refs/heads/hotfix/other"))
        self.assertEqual(out.returncode, 1)
        self.assertIn("::error::preprod is already frozen for OnePeek/backend-service refs/heads/hotfix/other.", out.stdout)

    def test_a_thaw_while_freeze_waits_stops_it_and_records_nothing(self):
        held = record(INFRA, "77", scope="infra", phase="locked")
        thaw = 'rm -f "$FAKE_S3/s3_locks_frozen.json" "$FAKE_S3/etag_locks_frozen.json"; echo in_progress'
        out = freeze(keys={INFRA_KEY: held}, gh=thaw, LOCK_WAIT_CAP_SECONDS="60")
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("::error::preprod-frozen changed while freeze waited (thawed, or written again)", out.stdout)
        self.assertIsNone(out.frozen)
        self.assertNotIn("capped", out.output)
        self.assertNotIn("settled-call", out.calls)

    def test_a_thaw_or_another_write_before_the_record_makes_freeze_record_nothing(self):
        out = freeze(CHANGE_BEFORE_PUT=frozen(run="91"))
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("::error::preprod-frozen changed before infra_head could be recorded", out.stdout)
        self.assertNotIn("infra_head", json.loads(out.frozen))

    def test_an_unreadable_infra_json_or_frozen_json_stops_freeze_with_no_infra_head(self):
        out = freeze(keys={INFRA_KEY: "not json"})
        self.assertEqual(out.returncode, 1)
        self.assertIn("locks/infra.json holds no readable record, so infra_head is not recorded", out.stdout)
        self.assertNotIn("infra_head", json.loads(out.frozen))
        out = freeze(frozen_by="not json")
        self.assertEqual(out.returncode, 1)
        self.assertIn("::error::preprod-frozen could not be read back.", out.stdout)
        out = freeze(FAIL_GET_OBJECT="AccessDenied")
        self.assertEqual(out.returncode, 1)
        self.assertNotIn("capped", out.output)
        self.assertEqual(len(frozen_puts(out)), 1)

    def test_thaw_is_unchanged_it_removes_frozen_json_with_or_without_infra_head(self):
        for rec in (frozen(), settled_frozen()):
            out = run(ACTION, frozen_by=rec, ACTION="thaw", LOCK_SCOPE="infra")
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            self.assertIsNone(out.frozen)


def workflow_sha_check(value: str):
    step = step_of(LOCKWF, "lock", "This workflow's own commit")
    return subprocess.run(["bash", "-eo", "pipefail", "-c", step["run"]], capture_output=True, text=True, env={**os.environ, "WORKFLOW_SHA": value})


class FreezeCheckout(unittest.TestCase):
    """Playbook §5.4a "How freeze reaches settled()": a sparse _gh/ checkout at job.workflow_sha, only under freeze, after
    a check that refuses an empty or malformed value."""

    def test_the_checkout_and_its_check_exist_only_under_freeze_and_use_job_workflow_sha(self):
        steps = LOCKWF["jobs"]["lock"]["steps"]
        gh = [s for s in steps if "_gh" in json.dumps(s.get("with", {}))]
        self.assertEqual(len(gh), 1)
        co = gh[0]
        self.assertTrue(co["uses"].startswith("actions/checkout@"))
        self.assertEqual(co["if"], "${{ inputs.action == 'freeze' }}")
        self.assertEqual({k: co["with"][k] for k in ("repository", "ref", "path", "persist-credentials")},
                         {"repository": "OnePeek/.github", "ref": "${{ job.workflow_sha }}", "path": "_gh", "persist-credentials": False})
        self.assertEqual(co["with"]["sparse-checkout"].split(), ["scripts"])
        check = step_of(LOCKWF, "lock", "This workflow's own commit")
        self.assertEqual(check["if"], "${{ inputs.action == 'freeze' }}")
        self.assertEqual(check["env"]["WORKFLOW_SHA"], "${{ job.workflow_sha }}")
        names = [s.get("name", s.get("uses")) for s in steps]
        self.assertLess(names.index(check["name"]), names.index(co["name"]))
        self.assertLess(names.index(co["name"]), next(i for i, s in enumerate(steps) if s.get("id") == "lock"))
        # Nothing else in the job checks anything out.
        self.assertEqual(sum("actions/checkout@" in str(s.get("uses", "")) for s in steps), 1)

    def test_an_empty_or_short_workflow_sha_fails_before_the_checkout(self):
        for bad in ("", "abc123", "A" * 40, "g" * 40, "a" * 41, "a" * 39, "${{ job.workflow_sha }}"):
            r = workflow_sha_check(bad)
            self.assertEqual(r.returncode, 1, bad)
            self.assertIn("::error::job.workflow_sha is", r.stdout)
        self.assertEqual(workflow_sha_check("0123456789abcdef0123456789abcdef01234567").returncode, 0)

    def test_actionlint_ignores_only_the_unknown_workflow_sha_property(self):
        conf = yaml.safe_load((WORKFLOWS.parent / "actionlint.yaml").read_text(encoding="utf-8"))
        ignores = [i for entry in conf["paths"].values() for i in entry.get("ignore", [])]
        self.assertEqual(ignores, ['property "workflow_sha" is not defined in object type'])
        self.assertEqual(set(conf), {"paths"})

    def test_freeze_arms_the_same_cap_trap_as_take(self):
        # A freeze at its cap passes its step with capped=true, so the job fails at "Wait cap reached (re-run)" alone.
        traps = re.findall(r"^\s*(take|freeze)\)\n(?:.*\n)*?\s*(trap '.*' EXIT)$", ACTION, re.M)
        self.assertEqual(sorted(a for a, _ in traps), ["freeze", "take"])
        self.assertEqual(len({t for _, t in traps}), 1)


def order(running: str, git: str, **extra: str):
    aws = {"AWS_DESCRIBE_SERVICES": "arn:td/api:7", "AWS_DESCRIBE_TASK_DEFINITION": running, **extra}
    return run(ORDER, files={"index.json": INDEX}, git=git, IMAGE="backend", **aws)


class Order(unittest.TestCase):
    """Playbook §5.4 "Order, not only one at a time": an older run never undoes a newer one."""

    def test_a_newer_commit_running_makes_this_release_a_skip_with_a_notice(self):
        out = order("sha-" + "2" * 12, 'case "$1" in rev-parse) echo ' + "2" * 40 + ';; merge-base) exit 0;; esac')
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("skip=true", out.output)
        self.assertIn("::notice::api already runs sha-222222222222", out.stdout)

    def test_an_older_commit_running_deploys(self):
        out = order("sha-" + "2" * 12, 'case "$1" in rev-parse) echo ' + "2" * 40 + ';; merge-base) exit 1;; esac')
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("skip=false", out.output)

    def test_a_commit_that_cannot_be_compared_means_no_deploy(self):
        for running, git in (("sha-" + "2" * 12, "exit 1"), ("latest", "exit 9")):
            out = order(running, git)
            self.assertEqual(out.returncode, 1, running)
            self.assertNotIn("skip=", out.output)



class FreshCredentials(unittest.TestCase):
    """A take may wait up to the cap and renews its session only in its own shell, so no later step of its job runs on the
    job-start session or App token (playbook §5.4a "Waits that fit the runners" rule 4)."""

    def test_every_step_after_a_waiting_take_has_fresh_credentials(self):
        import check_lock_limits
        checked = 0
        for doc, wf in ((RELEASE, "release-ecs"), (OPERATE, "operate"), (LOCKWF, "release-lock")):
            steps = doc["jobs"][LOCK_JOB[wf]]["steps"]
            take = next(i for i, s in enumerate(steps) if check_lock_limits.waits(s))
            role = next(s for s in steps[:take] if str(s.get("uses", "")).startswith("aws-actions/configure-aws-credentials@"))["with"]
            fresh = False
            for step in steps[take + 1:]:
                if str(step.get("uses", "")).startswith("aws-actions/configure-aws-credentials@"):
                    self.assertEqual(step["with"], role, wf)   # the same role, region and session name
                    fresh = True
                    continue
                self.assertNotIn("steps.app.outputs.token", json.dumps(step), (wf, step.get("name")))   # minted before the wait
                if re.search(r"(^|[\s;|&(])aws ", str(step.get("run", ""))):
                    self.assertTrue(fresh, (wf, step.get("name")))
                    checked += 1
        self.assertGreater(checked, 0)   # prepare's order check reads S3 and ECS after the take


class EdgeLimits(unittest.TestCase):
    """§5.4a "Every wait and its limit": a live infra run holding infra.json gets the job's cap; only a run that is
    queued, or live but holding no key, gets the 60 minutes."""

    def waits(self, keys):
        out = run(TAKE, keys=keys, gh=INFRA_RUNS % "echo 77", LOCK_INFRA_EDGE="1", LOCK_QUEUED_WAIT_SECONDS="1", LOCK_WAIT_CAP_SECONDS="6")
        self.assertEqual(out.returncode, 1)
        self.assertIn("::error::waiting for OnePeek/onepeek-infra (infra, run 77); re-run", out.stdout)
        self.assertNotIn("put-object", out.calls)
        return out

    def test_a_live_infra_run_that_holds_infra_json_is_waited_for_up_to_the_cap(self):
        # The cap is wall-clock time (bash SECONDS), so it is asserted as time: more than 5 real seconds of the 6 s cap.
        # The 1-second queued limit would end it after one wait. A count of waits shrinks on a loaded machine (pitfall 33).
        self.assertGreaterEqual(self.waits({INFRA_KEY: record(INFRA, "77", scope="infra")}).elapsed, 5)

    def test_a_queued_infra_run_or_one_that_holds_no_key_gets_the_60_minutes(self):
        self.assertLessEqual(self.waits({}).stdout.count("Waiting for the preprod release lock"), 2)


class InfraOwnScope(unittest.TestCase):
    def test_infra_waits_for_another_infra_run_under_the_cap_not_the_30_minute_app_limit(self):
        held = record(INFRA, "77", scope="infra")
        out = run(ACTION, keys={INFRA_KEY: held}, ACTION="take", LOCK_SCOPE="infra", GITHUB_REPOSITORY=INFRA,
                  LOCK_WAIT_SECONDS="1", LOCK_WAIT_CAP_SECONDS="6")
        self.assertEqual(out.returncode, 0)  # the cap: capped=true, and the "Wait cap reached (re-run)" step fails the job
        self.assertIn("capped=true", out.output)
        self.assertIn("::error::waiting for OnePeek/onepeek-infra (infra, run 77); re-run", out.stdout)
        # Asserted as time, not as a count of waits (pitfall 33): more than 5 real seconds of the 6 s cap, where the
        # 1-second app limit would end it after one wait.
        self.assertGreaterEqual(out.elapsed, 5)
        self.assertEqual(out.keys, {INFRA_KEY: held})


class ConflictETag(unittest.TestCase):
    def test_a_409_after_which_the_etag_changed_means_the_key_is_lost(self):
        # §5.4a "Answers to a conditional write": retry only while the ETag is still the one read and the record its own.
        out = run(call("lock_write_phase rolling", "echo changed-something"), keys={LEGACY: mine(), BACKEND: mine()}, LOCK_TOKEN=TOKEN,
                  CONFLICT_PUTS="1", CHANGE_ON_CONFLICT="1")
        self.assertEqual(out.returncode, 1)
        self.assertIn("changed during a conflicting write", out.stdout)
        self.assertNotIn("changed-something", out.stdout)
        self.assertEqual(json.loads(out.keys[LEGACY])["phase"], "locked")
        self.assertEqual(out.puts, "")


API_TD = PREV["api"]
RECORD_PREVIOUS = step_of(RELEASE, "deploy", "Record every service's current task definition")
ROLLBACK = step_of(RELEASE, "verify", "Put every service back on the revision recorded")


class FirstDeploy(unittest.TestCase):
    """A service first deployed by this release has no previous revision: it is recorded first-deploy and the put-back
    scales it to 0 rather than refusing the whole put-back (playbook §5.4a, provisional 2026-11-06)."""

    def test_previous_records_first_deploy_for_a_service_with_no_revision(self):
        for missing in ("None", ""):
            out = run(RECORD_PREVIOUS["run"], keys={LEGACY: mine(), BACKEND: mine()}, LOCK_TOKEN=TOKEN, DRY_RUN="false",
                      SERVICES='["api", "worker"]', SEQ_DESCRIBE_SERVICES=json.dumps([API_TD, missing]))
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            self.assertEqual(json.loads(out.keys[BACKEND])["previous"], {"api": API_TD, "worker": "first-deploy"}, missing)

    def test_operate_records_first_deploy_the_same_way(self):
        self.assertIn('prev[svc] = td if td and td != "None" else "first-deploy"', job_step(OPERATE, "operate", "Redeploy or roll back"))

    def test_the_put_back_scales_a_first_deployed_service_to_0_and_unlocks(self):
        held = mine("rolling", previous={"api": API_TD, "worker": "first-deploy"})
        out = run(FINISH, keys={LEGACY: held, BACKEND: held}, LOCK_TOKEN=TOKEN, GH_TOKEN="gh")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn(f"aws ecs update-service --cluster onepeek-preprod --service api --task-definition {API_TD}", out.calls)
        self.assertIn("aws ecs update-service --cluster onepeek-preprod --service worker --desired-count 0", out.calls)
        self.assertIn("aws ecs wait services-stable --cluster onepeek-preprod --services api worker", out.calls)
        self.assertEqual(out.keys, {})

    def test_a_previous_entry_that_is_neither_a_revision_nor_first_deploy_still_refuses_the_put_back(self):
        held = mine("rolling", previous={"api": "None"})
        out = run(FINISH, keys={LEGACY: held, BACKEND: held}, LOCK_TOKEN=TOKEN)
        self.assertEqual(out.returncode, 1)
        self.assertNotIn("update-service", out.calls)
        self.assertEqual(out.keys, {LEGACY: held, BACKEND: held})

    def test_verifys_rollback_scales_a_first_deployed_service_to_0(self):
        out = run(ROLLBACK["run"], PREVIOUS=json.dumps({"api": API_TD, "worker": "first-deploy"}), HAS_SANDBOX="")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn(f"--service api --task-definition {API_TD}", out.calls)
        self.assertIn("--service worker --desired-count 0", out.calls)
        self.assertNotIn("--task-definition first-deploy", out.calls)


class PutBackInactive(unittest.TestCase):
    """F589: a recorded revision deregistered since the release recorded it (an infra apply replacing a skeleton revision)
    is INACTIVE, and ECS refuses UpdateService onto it; the put-back registers it again from its recorded definition and
    moves the service to that new revision, and fails closed (the key stays rolling) when it cannot."""

    API_INACTIVE = PREV["api"]
    READ_ONLY = ("taskDefinitionArn", "revision", "status", "compatibilities", "requiresAttributes", "registeredAt",
                 "registeredBy", "deregisteredAt")  # fmt: skip

    def finish(self, **extra):
        held = mine("rolling", previous=PREV)
        return held, run(FINISH, keys={LEGACY: held, BACKEND: held}, LOCK_TOKEN=TOKEN, GH_TOKEN="gh", **extra)

    def test_an_inactive_revision_is_registered_again_from_its_definition_and_the_service_moves_to_it(self):
        _, out = self.finish(TD_INACTIVE=self.API_INACTIVE)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn(f"aws ecs describe-task-definition --task-definition {self.API_INACTIVE} --include TAGS --output json", out.calls)
        bodies = [json.loads(b) for b in out.registered.splitlines()]
        self.assertEqual(len(bodies), 1)   # only the inactive one
        body = bodies[0]
        self.assertEqual(body["family"], "onepeek-preprod-api")
        self.assertEqual(body["containerDefinitions"], [{"name": "api", "image": "r/x@sha256:" + "a" * 64}])
        self.assertEqual((body["executionRoleArn"], body["networkMode"], body["cpu"], body["memory"], body["requiresCompatibilities"]),
                         ("arn:aws:iam::111111111111:role/exec", "awsvpc", "256", "512", ["FARGATE"]))
        self.assertEqual(body["tags"], [{"key": "commit", "value": "sha-" + "1" * 12}, {"key": "release-run", "value": "4000"}])
        for field in self.READ_ONLY:
            self.assertNotIn(field, body)
        new = "arn:aws:ecs:ap-south-1:111111111111:task-definition/onepeek-preprod-api:100"
        self.assertIn(f"aws ecs update-service --cluster onepeek-preprod --service api --task-definition {new}", out.calls)
        self.assertNotIn(f"--task-definition {self.API_INACTIVE} >", out.calls)
        self.assertNotIn(f"--service api --task-definition {self.API_INACTIVE}", out.calls)
        self.assertIn(f"aws ecs update-service --cluster onepeek-preprod --service worker --task-definition {PREV['worker']}", out.calls)
        self.assertIn("onepeek-preprod-api:6 is inactive", out.stdout)
        self.assertIn("onepeek-preprod-api:100", out.stdout)
        self.assertEqual(out.keys, {})

    def test_an_active_revision_is_used_as_recorded_and_nothing_is_registered(self):
        _, out = self.finish()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(out.registered, "")
        self.assertNotIn("register-task-definition", out.calls)
        for svc, td in PREV.items():
            self.assertIn(f"aws ecs update-service --cluster onepeek-preprod --service {svc} --task-definition {td}", out.calls)

    def test_a_revision_that_cannot_be_made_active_fails_the_put_back_and_keeps_the_key_rolling(self):
        other_family = json.dumps({"taskDefinition": {"family": "onepeek-preprod-web", "status": "ACTIVE", "containerDefinitions": [{"name": "web"}]}})
        no_container = json.dumps({"taskDefinition": {"family": "onepeek-preprod-api", "status": "INACTIVE", "containerDefinitions": []}})
        wrong_arn = "arn:aws:ecs:ap-south-1:111111111111:task-definition/onepeek-preprod-web:9"
        cases = {
            "describe fails": {"FAIL_DESCRIBE_TASK_DEFINITION": "ClientException"},
            "describe unreadable": {"AWS_DESCRIBE_TASK_DEFINITION": "not json"},
            "describe names another family": {"AWS_DESCRIBE_TASK_DEFINITION": other_family},
            "no container to register": {"AWS_DESCRIBE_TASK_DEFINITION": no_container},
            "neither active nor inactive": {"TD_STATUS": "DELETE_IN_PROGRESS"},
            "register fails": {"TD_INACTIVE": self.API_INACTIVE, "FAIL_REGISTER_TASK_DEFINITION": "AccessDeniedException"},
            "register answers another family": {"TD_INACTIVE": self.API_INACTIVE, "AWS_REGISTER_TASK_DEFINITION": wrong_arn},
            "register answers nothing": {"TD_INACTIVE": self.API_INACTIVE, "AWS_REGISTER_TASK_DEFINITION": "None"},
        }  # fmt: skip
        for name, extra in cases.items():
            with self.subTest(name):
                held, out = self.finish(**extra)
                self.assertEqual(out.returncode, 1, name + ": " + out.stdout + out.stderr)
                self.assertNotIn("update-service", out.calls, name)
                self.assertIn("::error::api could not be put back", out.stdout, name)
                self.assertIn("cancel-release.yml", out.stdout, name)
                self.assertEqual(out.keys, {LEGACY: held, BACKEND: held}, name)

    def test_every_copy_of_the_lock_functions_carries_the_re_registration(self):
        for text in (RELEASE_TEXT, OPERATE_TEXT, (WORKFLOWS / "release-lock.yml").read_text(encoding="utf-8")):
            for block in re.findall(r"# --- release-lock begin.*?# --- release-lock end ---", text, re.S):
                self.assertIn("lock_live_revision", block)


class ManualPath(unittest.TestCase):
    """Until cancel-release.yml exists (.github #55), every message naming it also gives the manual path."""

    def says_the_manual_path(self, text):
        self.assertIn("cancel-release.yml", text)
        self.assertIn("python3 .github/workspace/lock_status.py preprod", text)
        self.assertIn("the founder only deletes it with the conditional delete (aws s3api delete-object --if-match <its ETag>)", text)

    def test_a_rolling_key_names_cancel_release_and_the_manual_path(self):
        out = run(TAKE, keys={BACKEND: record(REPO, "88", scope="backend", phase="rolling")}, gh="echo completed")
        self.assertEqual(out.returncode, 1)
        self.says_the_manual_path(out.stdout)

    def test_a_failed_put_back_names_cancel_release_and_the_manual_path(self):
        held = mine("rolling", previous=PREV)
        out = run(FINISH, keys={LEGACY: held, BACKEND: held}, LOCK_TOKEN=TOKEN, FAIL_UPDATE_SERVICE="ServiceNotFound")
        self.assertEqual(out.returncode, 1)
        self.says_the_manual_path(out.stdout)


OPERATE_STEP = job_step(OPERATE, "operate", "Redeploy or roll back")
SERVICES_FILE = "services:\n  api:\n    image: backend\n    triggers: [http]\n"
TEMPLATE = json.dumps({"family": "onepeek-preprod-api", "containerDefinitions": [{"name": "api", "image": ""}]})


# docker and cosign read the password from stdin as the real tools do: a stub that exits without reading it breaks the pipe
# under `aws ecr get-login-password | docker login --password-stdin`, and the fake aws then exits 120 (Linux runners race this).
READS_PASSWORD = 'case " $* " in *" --password-stdin "*) cat >/dev/null ;; esac\nexit 0'
TOOL_STUBS = {"docker": READS_PASSWORD, "cosign": READS_PASSWORD, "curl": "exit 0"}


class OperateRolling(unittest.TestCase):
    """operate.yml's phases (playbook §5.4a "Rollback on cancel", operate.yml): a redeploy writes rolling before its first
    update-service, so a failure puts it back; a rollback never puts back, and a failed one stays locked with an alert."""

    def operate(self, action):
        stubs = TOOL_STUBS
        return run(OPERATE_STEP, keys={LEGACY: mine(), BACKEND: mine()}, files={"services.yaml": SERVICES_FILE}, stubs=stubs,
                   ACTION=action, SERVICE="api", TARGET="sha-" + "2" * 12 if action == "rollback" else "", REASON="test", LOCK_TOKEN=TOKEN,
                   AWS_REGION="ap-south-1", DOMAIN_PREPROD="preprod.example", AWS_GET_CALLER_IDENTITY="111111111111",
                   AWS_DESCRIBE_SERVICES=API_TD, AWS_CP=TEMPLATE, AWS_DESCRIBE_IMAGES="sha256:" + "a" * 64,
                   SEQ_DESCRIBE_TASK_DEFINITION=json.dumps(["sha-" + "1" * 12, json.dumps({"containerDefinitions": [{"name": "api"}]})]),
                   AWS_REGISTER_TASK_DEFINITION=API_TD.replace(":6", ":9"), AWS_DESCRIBE_ALARMS="onepeek-preprod-deploy-api-errors")  # fmt: skip

    def test_a_redeploy_writes_rolling_with_previous_before_its_first_update_service(self):
        out = self.operate("redeploy")
        self.assertEqual(out.returncode, 1)   # the watch saw an alarm
        self.assertIn("Deploy alarms firing", out.stdout)
        rec = json.loads(out.keys[BACKEND])
        self.assertEqual((rec["phase"], rec["previous"]), ("rolling", {"api": API_TD}))   # so the unlock job puts it back
        lines = out.calls.splitlines()
        rolling = next(i for i, l in enumerate(lines) if "put-object" in l and BACKEND in l)
        update = next(i for i, l in enumerate(lines) if "update-service" in l)
        self.assertLess(rolling, update)

    def test_a_failed_rollback_stays_locked_never_rolling_and_fails_with_an_alert(self):
        out = self.operate("rollback")
        self.assertEqual(out.returncode, 1)
        self.assertIn("update-service --cluster onepeek-preprod --service api --task-definition", out.calls)
        self.assertEqual({k: json.loads(v)["phase"] for k, v in out.keys.items()}, {LEGACY: "locked", BACKEND: "locked"})
        self.assertNotIn("previous", out.keys[BACKEND])
        self.assertEqual(out.puts, "")
        self.assertIn("::error::The rollback of api in preprod failed, and nothing is put back", out.stdout)
        self.assertIn("The unlock job frees the key (phase locked, nothing put back)", out.stdout)
        self.assertIn("cancel-release.yml", out.stdout)
        self.assertNotIn("delete", out.stdout.lower())   # never tells anyone to delete the key by hand
        self.assertNotIn("succeeded but", out.stdout)
        self.assertIn("The rollback of api in preprod failed", out.summary)

    def test_a_rollback_whose_final_phase_write_fails_is_not_reported_as_a_failed_rollback(self):
        quick = OPERATE_STEP.replace("SECONDS + 300", "SECONDS + 0")   # no alarm: the watch ends at once
        self.assertNotEqual(quick, OPERATE_STEP)
        out = run(quick, keys={LEGACY: mine(), BACKEND: mine()}, files={"services.yaml": SERVICES_FILE},
                  stubs=TOOL_STUBS, FAIL_PUT_OBJECT="AccessDenied",
                  ACTION="rollback", SERVICE="api", TARGET="sha-" + "2" * 12, REASON="test", LOCK_TOKEN=TOKEN,
                  AWS_REGION="ap-south-1", DOMAIN_PREPROD="preprod.example", AWS_GET_CALLER_IDENTITY="111111111111",
                  AWS_DESCRIBE_SERVICES=API_TD, AWS_CP=TEMPLATE, AWS_DESCRIBE_IMAGES="sha256:" + "a" * 64,
                  SEQ_DESCRIBE_TASK_DEFINITION=json.dumps(["sha-" + "1" * 12, json.dumps({"containerDefinitions": [{"name": "api"}]})]),
                  AWS_REGISTER_TASK_DEFINITION=API_TD.replace(":6", ":9"), AWS_DESCRIBE_ALARMS="")  # fmt: skip
        self.assertEqual(out.returncode, 1)
        self.assertIn("The rollback of api in preprod succeeded but its phase write failed", out.stdout)
        self.assertNotIn("preprod failed", out.stdout)
        self.assertIn("succeeded but its phase write failed", out.summary)


if __name__ == "__main__":
    unittest.main()
