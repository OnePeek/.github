"""cancel-release.yml, lock_status.py, cancel_release.py and the P19 hook's cancel rules (onepeek-docs
architecture/lld/infra/foundation-playbook.md §5.4a "lock-status", "Rollback on cancel", "cancel-release.yml";
delivery-pipelines.md UC-DEL-009, E11), run for real against the fake S3 and ECS of test_release_lock.
Run: python -m unittest discover -s scripts -p 'test_*.py'"""

import importlib.util
import io
import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import yaml

import test_release_lock as L

ROOT = Path(__file__).resolve().parents[1]
CANCELWF = yaml.safe_load((L.WORKFLOWS / "cancel-release.yml").read_text(encoding="utf-8"))
CANCEL_TEXT = (L.WORKFLOWS / "cancel-release.yml").read_text(encoding="utf-8")
CANCEL = L.job_step(CANCELWF, "cancel", "Cancel the release")
WHO = L.job_step(CANCELWF, "authorise", "Only the founder")
AGAIN = L.job_step(CANCELWF, "cancel", "Both actors again")


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lock_status = load("lock_status", ROOT / "workspace" / "lock_status.py")
cancel_release = load("cancel_release", ROOT / "workspace" / "cancel_release.py")
p19 = load("p19_guard", ROOT / "workspace" / "hooks" / "p19_guard.py")

HOLDER = "77"
# gh: the holder's run status (a file the cancel call flips), its jobs, the cancel call itself.
GH = """case "$*" in
*runs/77/cancel*) touch "$FAKE_S3/cancelled"; %(on_cancel)s;;
*runs/77/jobs*) echo %(apply)s;;
*"runs/77 --jq .status"*) if [ -e "$FAKE_S3/cancelled" ] && [ -n "%(finish)s" ]; then echo completed; else echo %(status)s; fi;;
*"runs/9 --jq .status"*) echo %(repair)s;;
*runs/77*) echo cancelled;;
*) echo completed;;
esac"""


def gh(status="completed", on_cancel=":", apply="0", finish="", repair="completed") -> str:
    return GH % {"status": status, "on_cancel": on_cancel, "apply": apply, "finish": finish, "repair": repair}


def held(phase="locked", repo=L.REPO, **fields) -> str:
    return L.record(repo, HOLDER, scope="backend" if repo == L.REPO else "infra", phase=phase, **fields)


def cancel(keys: dict, ghs: str, **extra):
    return L.run(CANCEL, keys=keys, gh=ghs, **{"LOCK_CANCEL_TOKEN": "gt", "LOCK_CANCEL_WAIT_SECONDS": "3", "LOCK_CANCEL_POLL_SECONDS": "1",
                                               "REASON": "drill", "GITHUB_ACTOR": "founder", **extra})


class Cancel(unittest.TestCase):
    def test_a_free_key_reports_lock_free_and_ends_green(self):
        out = cancel({}, gh())
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("Lock free, nothing held.", out.summary)

    def test_a_record_naming_another_repo_is_refused(self):
        rec = held(repo=L.INFRA)
        out = cancel({L.BACKEND: rec}, gh(status="in_progress"))
        self.assertEqual(out.returncode, 1)
        self.assertIn("cancel from onepeek-infra", out.stdout)
        self.assertEqual(out.keys, {L.BACKEND: rec})
        self.assertNotIn("/cancel", out.calls)

    def test_a_finished_run_left_locked_or_done_is_removed(self):
        for phase in ("locked", "done"):
            out = cancel({L.LEGACY: held(phase), L.BACKEND: held(phase)}, gh())
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            self.assertEqual(out.keys, {})

    def test_a_migrating_key_with_a_running_task_is_never_removed_and_the_task_never_stopped(self):
        rec = held("migrating", migrate_task=L.TASK)
        out = cancel({L.BACKEND: rec}, gh(), AWS_DESCRIBE_TASKS=L.tasks((L.TASK, L.MIGRATE_TD, "RUNNING")))
        self.assertEqual(out.returncode, 1)
        self.assertIn("still running", out.stdout)
        self.assertEqual(out.keys, {L.BACKEND: rec})
        self.assertNotIn("stop-task", out.calls)
        out = cancel({L.BACKEND: rec}, gh(), AWS_DESCRIBE_TASKS=L.tasks((L.TASK, L.MIGRATE_TD, "STOPPED")))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(out.keys, {})

    def test_a_rolling_key_is_claimed_put_back_and_removed(self):
        rec = held("rolling", previous=L.PREV)
        out = cancel({L.LEGACY: rec, L.BACKEND: rec}, gh(), GH_TOKEN="gh")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        claims = [json.loads(p.split(" ", 1)[1]) for p in out.puts.splitlines()]
        self.assertEqual({(c["repair_run"], c["phase"]) for c in claims}, {(L.RUN + "-1", "rolling")})
        self.assertRegex(claims[0]["repair_token"], r"^[0-9a-f]{32}$")
        for svc, td in L.PREV.items():
            self.assertIn(f"update-service --cluster onepeek-preprod --service {svc} --task-definition {td}", out.calls)
        self.assertEqual(out.keys, {})
        self.assertIn("api: recorded onepeek-preprod-api:6", out.summary)
        put_line = out.calls.index("put-object")
        self.assertLess(put_line, out.calls.index("update-service"))   # the claim before any change

    def test_two_repairs_at_once_leave_exactly_one_claim(self):
        rec = held("rolling", previous=L.PREV)
        other = held("rolling", previous=L.PREV, repair_run="9-1", repair_token="d" * 32)
        out = cancel({L.BACKEND: rec}, gh(), CHANGE_BEFORE_PUT=other)
        self.assertEqual(out.returncode, 1)
        self.assertIn("lost to another repairer", out.stdout)
        self.assertNotIn("update-service", out.calls)
        self.assertEqual(out.keys, {L.BACKEND: other})

    def test_a_record_being_repaired_by_a_live_run_is_left_alone(self):
        rec = held("rolling", previous=L.PREV, repair_run="9-1", repair_token="d" * 32)
        out = cancel({L.BACKEND: rec}, gh(repair="in_progress"))
        self.assertEqual(out.returncode, 1)
        self.assertIn("Being repaired by", out.stdout)
        self.assertEqual(out.puts, "")
        out = cancel({L.BACKEND: rec}, gh(repair="completed"), GH_TOKEN="gh")   # its repair run has finished: claim again
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(out.keys, {})

    def test_a_failed_put_back_keeps_the_key_rolling_with_this_claim(self):
        rec = held("rolling", previous=L.PREV)
        out = cancel({L.BACKEND: rec}, gh(), FAIL_UPDATE_SERVICE="ServiceNotFound")
        self.assertEqual(out.returncode, 1)
        left = json.loads(out.keys[L.BACKEND])
        self.assertEqual((left["phase"], left["repair_run"]), ("rolling", L.RUN + "-1"))

    def test_a_live_run_is_cancelled_never_force_cancelled_and_its_finish_frees_the_key(self):
        rec = held("rolling", previous=L.PREV)
        frees = 'rm -f "$FAKE_S3"/s3_locks_* "$FAKE_S3"/etag_locks_*'
        out = cancel({L.BACKEND: rec}, gh(status="in_progress", on_cancel=frees))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("gh token=gt api -X POST repos/OnePeek/backend-service/actions/runs/77/cancel", out.calls)
        self.assertNotIn("force-cancel", out.calls)
        self.assertNotIn("delete-object", out.calls)
        self.assertNotIn("update-service", out.calls)

    def test_a_live_run_whose_key_stays_fails_with_the_re_run_message_and_removes_nothing(self):
        rec = held("rolling", previous=L.PREV)
        out = cancel({L.BACKEND: rec}, gh(status="in_progress"))
        self.assertEqual(out.returncode, 1)
        self.assertIn("::error::waiting for OnePeek/backend-service (backend, run 77); re-run", out.stdout)
        self.assertNotIn("delete-object", out.calls)
        self.assertEqual(out.keys, {L.BACKEND: rec})

    def test_a_run_that_ends_with_the_key_still_there_takes_the_finished_branch(self):
        rec = held("rolling", previous=L.PREV)
        out = cancel({L.BACKEND: rec}, gh(status="in_progress", finish="yes"), GH_TOKEN="gh")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("update-service", out.calls)
        self.assertEqual(out.keys, {})

    def test_it_refuses_during_an_infra_apply(self):
        rec = L.record(L.INFRA, HOLDER, scope="infra")
        keys = {k: rec for k in (L.LEGACY, L.INFRA_KEY, L.BACKEND, L.WEB, L.MCP)}
        out = cancel(keys, gh(status="in_progress", apply="1"), LOCK_SCOPE="infra", GITHUB_REPOSITORY=L.INFRA)
        self.assertEqual(out.returncode, 1)
        self.assertIn("an apply is never cancelled", out.stdout)
        self.assertNotIn("/cancel", out.calls.replace("cancel-release", ""))
        self.assertEqual(len(out.keys), 5)

    def test_infra_acts_on_every_key_naming_its_run(self):
        rec = L.record(L.INFRA, HOLDER, scope="infra")
        keys = {k: rec for k in (L.LEGACY, L.INFRA_KEY, L.BACKEND, L.WEB, L.MCP)}
        out = cancel(keys, gh(), LOCK_SCOPE="infra", GITHUB_REPOSITORY=L.INFRA)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(out.keys, {})

    def test_the_status_it_prints_never_holds_the_token(self):
        rec = held("rolling", previous=L.PREV)
        out = L.run(L.LIB + "lock_status_all\n", keys={L.BACKEND: rec})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("locks/backend.json: holder OnePeek/backend-service", out.stdout)
        self.assertIn("previous api, worker", out.stdout)
        self.assertNotIn("f" * 32, out.stdout + out.summary)
        self.assertIn("locks/web.json: free", out.stdout)


APPROVERS = "version: 1\nfounder: Karan-98-Mehta\nagents_app: onepeek-agents\n"


class WhoMayRunIt(unittest.TestCase):
    """Both github.actor and github.triggering_actor must be the founder; the App slug must be the policy's."""

    def who(self, approvers=APPROVERS, **env):
        ghs = f"cat <<'Y'\n{approvers}Y"
        return L.run(WHO, gh=ghs, **{"APP_SLUG": "onepeek-agents", "ACTOR": "Karan-98-Mehta", "TRIGGERING_ACTOR": "Karan-98-Mehta",
                                    "SCOPE": "backend", "GITHUB_REPOSITORY_OWNER": "OnePeek", **env})

    def test_the_founder_as_both_actors_through_the_policys_app_passes(self):
        out = self.who()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("founder=Karan-98-Mehta", out.output)
        self.assertIn("contents/policy/approvers.yaml?ref=release", out.calls)

    def test_anyone_else_is_refused(self):
        for env in ({"ACTOR": "someone"}, {"TRIGGERING_ACTOR": "someone"}, {"APP_SLUG": "other-app"},
                    {"ACTOR": "onepeek-agents[bot]", "TRIGGERING_ACTOR": "onepeek-agents[bot]"}):
            out = self.who(**env)
            self.assertEqual(out.returncode, 1, env)
            self.assertIn("refused", out.stdout)
            self.assertNotIn("founder=", out.output)
        out = self.who(approvers="version: 1\nfounder: Karan-98-Mehta\n")   # no agents_app yet
        self.assertEqual(out.returncode, 1)

    def test_the_acting_job_checks_both_actors_again_before_any_credential(self):
        for actor, trig, code in (("f", "f", 0), ("x", "f", 1), ("f", "x", 1)):
            out = L.run(AGAIN, FOUNDER="f", ACTOR=actor, TRIGGERING_ACTOR=trig)
            self.assertEqual(out.returncode, code, (actor, trig))
        out = L.run(AGAIN, FOUNDER="", ACTOR="", TRIGGERING_ACTOR="")
        self.assertEqual(out.returncode, 1)
        steps = CANCELWF["jobs"]["cancel"]["steps"]
        self.assertTrue(steps[0]["name"].startswith("Both actors again"))
        creds = next(i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith("aws-actions/configure-aws-credentials@"))
        self.assertGreater(creds, 0)

    def test_the_shape(self):
        jobs = CANCELWF["jobs"]
        self.assertNotIn("id-token", jobs["authorise"]["permissions"])
        self.assertEqual(jobs["cancel"]["needs"], "authorise")
        self.assertEqual(jobs["cancel"]["concurrency"], {"group": "cancel-release-${{ inputs.environment }}-${{ needs.authorise.outputs.scope }}",
                                                         "cancel-in-progress": False})
        self.assertEqual(jobs["cancel"]["timeout-minutes"], 110)
        self.assertEqual(jobs["cancel"]["permissions"]["actions"], "write")
        self.assertEqual(set(CANCELWF[True]["workflow_call"]["inputs"]), {"runner", "environment", "reason", "role-to-assume"})
        code = "\n".join(l for l in CANCEL_TEXT.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("force-cancel", code)
        self.assertNotIn("stop-task", code)
        creds = next(s for s in CANCELWF["jobs"]["cancel"]["steps"] if str(s.get("uses", "")).startswith("aws-actions/configure-aws-credentials@"))
        self.assertEqual(creds["with"]["role-to-assume"], "${{ inputs.role-to-assume }}")

    def test_the_scope_is_the_callers_own(self):
        script = L.job_step(CANCELWF, "authorise", "The inputs and the caller's scope")
        for repo, scope in (("OnePeek/onepeek-infra", "infra"), ("OnePeek/backend-service", "backend"), ("OnePeek/onepeek-web", "web"),
                            ("OnePeek/mcp-athena", "mcp")):
            out = L.run(script, GITHUB_REPOSITORY=repo, TARGET_ENV="preprod", REASON="r")
            self.assertIn("scope=" + scope, out.output)
        for repo, env, reason in (("OnePeek/.github", "preprod", "r"), (L.REPO, "dev", "r"), (L.REPO, "prod", "")):
            self.assertEqual(L.run(script, GITHUB_REPOSITORY=repo, TARGET_ENV=env, REASON=reason).returncode, 1)


FAKE_AWS_CLI = """#!/usr/bin/env python3
import json, os, shutil, sys
args = sys.argv[1:]
open(os.environ["CALLS"], "a").write(" ".join(args) + "\\n")
store = json.loads(os.environ["OBJECTS"])
if args[1] == "list-objects-v2":
    if os.environ.get("FAIL_LIST"): sys.exit(254)
    print(json.dumps({"Contents": [{"Key": k} for k in store]}))
elif args[1] == "get-object":
    key = args[args.index("--key") + 1]
    if key in os.environ.get("FAIL_KEYS", ""): sys.exit(254)
    open(args[args.index("--key") + 2], "w").write(store[key])
"""


class LockStatus(unittest.TestCase):
    def status(self, objects: dict, **env):
        with tempfile.TemporaryDirectory() as d:
            aws = Path(d) / "aws"
            aws.write_text(FAKE_AWS_CLI)
            aws.chmod(aws.stat().st_mode | stat.S_IEXEC)
            calls = Path(d) / "calls"
            with mock.patch.dict(os.environ, {"PATH": f"{d}:{os.environ['PATH']}", "OBJECTS": json.dumps(objects), "CALLS": str(calls), **env}):
                lines, rc = lock_status.status("preprod", "lock-reader")
            return lines, rc, calls.read_text() if calls.exists() else ""

    def test_each_scope_is_free_or_its_fixed_fields_and_never_the_token(self):
        rolling = held("rolling", previous=L.PREV, repair_run="9-1", repair_token="d" * 32)
        lines, rc, calls = self.status({L.BACKEND: rolling, "locks/frozen.json": L.frozen(), L.LEGACY: held()})
        self.assertEqual(rc, 0)
        text = "\n".join(lines)
        self.assertIn("locks/infra.json: free", text)
        self.assertIn("locks/backend.json: repo OnePeek/backend-service; run ?; commit sha-111111111111; phase rolling; age", text)
        self.assertIn("repair_run 9-1; previous api, worker", text)
        self.assertIn("locks/frozen.json: who founder; branch OnePeek/backend-service refs/heads/hotfix/x", text)
        self.assertIn("locks/release.json:", text)
        self.assertNotIn("f" * 32, text)
        self.assertNotIn("d" * 32, text)
        self.assertIn("--profile lock-reader", calls)
        self.assertIn("list-objects-v2 --bucket onepeek-preprod-deploy --prefix locks/", calls)

    def test_it_fails_closed(self):
        lines, rc, _ = self.status({L.BACKEND: held()}, FAIL_KEYS=L.BACKEND)
        self.assertEqual(rc, 1)
        self.assertTrue(any(l.startswith("ERROR locks/backend.json") and "not free" in l for l in lines))
        lines, rc, _ = self.status({L.BACKEND: "not json"})
        self.assertEqual(rc, 1)
        lines, rc, _ = self.status({}, FAIL_LIST="1")
        self.assertEqual(rc, 1)
        self.assertFalse(any("free" in l and l.startswith("locks/") for l in lines))

    def test_the_read_files_are_private_and_removed(self):
        made = []
        real = tempfile.mkdtemp
        with mock.patch.object(lock_status.tempfile, "mkdtemp", side_effect=lambda *a, **k: made.append(real(*a, **k)) or made[-1]):
            self.status({L.BACKEND: held()})
        self.assertTrue(made)
        self.assertFalse(os.path.exists(made[0]))

    def test_it_only_reads(self):
        _, _, calls = self.status({L.BACKEND: held()})
        self.assertEqual({c.split()[1] for c in calls.splitlines()}, {"list-objects-v2", "get-object"})


class Wrapper(unittest.TestCase):
    """cancel_release.py: the one way agents start a cancel; it refuses prod in an agent session."""

    def test_prod_is_refused_in_an_agent_session(self):
        real = cancel_release.agent_session
        for env, tty in (({"CLAUDECODE": "1"}, True), ({"CLAUDE_CODE_ENTRYPOINT": "cli"}, True), ({}, False)):
            with mock.patch.object(cancel_release, "agent_session", return_value=real(env=env, interactive=tty)), \
                    mock.patch.object(cancel_release, "dispatch") as dispatched:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cancel_release.main(["backend-service", "prod", "--reason", "drill"])
                self.assertEqual(rc, 2, env)
                self.assertIn("REFUSED", buf.getvalue())
                dispatched.assert_not_called()

    def test_preprod_dispatches_on_release_with_the_persons_login(self):
        with mock.patch.object(cancel_release, "agent_session", return_value=True), \
                mock.patch.object(cancel_release, "gh") as g:
            g.side_effect = [subprocess.CompletedProcess([], 0, "", ""),
                             subprocess.CompletedProcess([], 0, json.dumps([{"url": "https://run/1", "createdAt": "2999-01-01T00:00:00Z"}]), "")]
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cancel_release.main(["backend-service", "preprod", "--reason", "drill"])
        self.assertEqual(rc, 0)
        self.assertEqual(g.call_args_list[0].args[0], ["workflow", "run", "cancel-release.yml", "--repo", "OnePeek/backend-service", "--ref", "release",
                                                       "-f", "environment=preprod", "-f", "reason=drill"])
        self.assertIn("https://run/1", buf.getvalue())

    def test_the_founders_own_terminal_may_cancel_prod(self):
        self.assertFalse(cancel_release.agent_session(env={}, interactive=True))
        self.assertTrue(cancel_release.agent_session(env={"CLAUDECODE": "1"}, interactive=True))

    def test_only_the_releasing_repos_and_two_environments(self):
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            for argv in (["onepeek-docs", "preprod", "--reason", "x"], ["backend-service", "sandbox", "--reason", "x"], ["backend-service", "preprod"]):
                with self.assertRaises(SystemExit):
                    cancel_release.main(argv)


class P19Hook(unittest.TestCase):
    """The hook refuses every other way to dispatch or cancel from an agent session (playbook §5.4a, P19)."""

    def hook(self, command: str, tool: str = "Bash"):
        return subprocess.run(["python3", str(ROOT / "workspace" / "hooks" / "p19_guard.py")], capture_output=True, text=True,
                              input=json.dumps({"tool_name": tool, "tool_input": {"command": command}}))

    def test_direct_dispatches_and_cancels_are_refused(self):
        for cmd in ("gh workflow run cancel-release.yml --repo OnePeek/backend-service -f environment=prod",
                    "gh workflow run operate.yml -R OnePeek/backend-service", "gh run cancel 123 --repo OnePeek/onepeek-web",
                    "gh api -X POST repos/OnePeek/backend-service/actions/runs/77/cancel",
                    "gh api --method POST /repos/OnePeek/backend-service/actions/runs/77/force-cancel",
                    "gh api repos/OnePeek/backend-service/actions/workflows/cancel-release.yml/dispatches -f ref=release",
                    "cd /x && GH_TOKEN=t gh run cancel 5", "echo ok; env A=1 gh workflow run x.yml", "bash -c 'gh run cancel 5'",
                    "x=$(gh api -X POST repos/o/r/actions/runs/1/cancel)",
                    # a global flag with a value before the subcommand (review of #55, 2026-11-06)
                    "gh -R OnePeek/onepeek-web run cancel 1", "gh --repo OnePeek/x workflow run a.yml",
                    "gh --hostname github.com run cancel 2"):
            r = self.hook(cmd)
            self.assertEqual(r.returncode, 2, cmd)
            self.assertIn("cancel_release.py", r.stderr)

    def test_the_wrapper_and_reads_pass(self):
        for cmd in ("python3 .github/workspace/cancel_release.py backend-service preprod --reason drill",
                    "gh run list --workflow cancel-release.yml", "gh run view 77", "gh api repos/OnePeek/x/actions/runs/77",
                    "gh pr view 54", "git log --grep cancel"):
            self.assertEqual(self.hook(cmd).returncode, 0, cmd)
        self.assertEqual(self.hook("gh run cancel 5", tool="Read").returncode, 0)

    def test_it_is_registered_for_bash_in_the_workspace_settings(self):
        settings = json.loads((ROOT / "workspace" / "settings.json").read_text())
        (entry,) = settings["hooks"]["PreToolUse"]
        self.assertEqual(entry["matcher"], "Bash")
        self.assertIn(".github/workspace/hooks/p19_guard.py", entry["hooks"][0]["command"])
        setup = load("setup_ws", ROOT / "workspace" / "setup.py")
        self.assertEqual(setup.wanted()[setup.ROOT / ".claude" / "settings.json"], setup.HERE / "settings.json")


if __name__ == "__main__":
    unittest.main()
