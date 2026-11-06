"""Tests for workspace/ship_watch.py: classification, merge queue, re-runs, alerts, outputs, fail closed (no network)."""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "ship_watch", pathlib.Path(__file__).resolve().parents[1] / "workspace" / "ship_watch.py")
sw = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sw)
CFG = {"repos": ["onepeek-docs", ".github", "onepeek-infra", "onepeek-web"], "authors": ["Karan-98-Mehta", "app/dependabot"],
       "known_flakes": ["flaky / job"], "depends_on_check": "depends-on / depends-on",
       "warn": [("onepeek-infra", "sandbox", "apply-test")]}
NOW = 1_800_000_000.0
GREEN = [{"name": "depends-on / depends-on", "bucket": "pass", "link": "https://x/runs/11/job/1"}]
HOT = {"body": "Lane: hotfix"}
BOT_DONE = {"name": "CodeRabbit", "bucket": "pass", "link": "https://coderabbit.ai"}


def pr(repo, n, **kw):
    base = {"onepeek-docs": "main", ".github": "main"}.get(repo, "dev")
    return {"number": n, "title": f"t{n}", "url": f"u{n}", "author": {"login": "Karan-98-Mehta"}, "isDraft": False,
            "mergeable": "MERGEABLE", "headRefOid": f"h{n}", "headRefName": f"feat/{n}", "baseRefName": base,
            "body": "", "createdAt": "2026-11-06T00:00:00Z", "isCrossRepository": False,
            "headRepositoryOwner": {"login": "OnePeek"}, **kw}


def done(r):
    return subprocess.CompletedProcess([], 0, r if isinstance(r, str) else json.dumps(r), "")


class FakeGh:
    """Stands in for `gh`: answers each read from the given world, records every call, fails the ones in `fail`."""

    def __init__(self, prs, checks=None, threads=None, states=None, online=1, queued=0, sandbox=None, fail=(),
                 bot=BOT_DONE, pins=None, docs_commits=None):
        self.prs, self.checks, self.threads, self.states = prs, checks or {}, threads or {}, states or {}
        self.bot, self.pins, self.docs_commits, self.required = bot, pins or {}, docs_commits or {}, None
        self.online, self.queued, self.sandbox, self.fail, self.calls = online, queued, sandbox, fail, []

    def __call__(self, args):
        self.calls.append(args)
        if any(f in " ".join(args) for f in self.fail):
            return subprocess.CompletedProcess(args, 1, "", "HTTP 502")
        a = args
        if a[:2] == ["pr", "list"]:
            return done([p for p in self.prs if p["_repo"] == a[3].split("/")[1]])
        if a[:2] == ["pr", "checks"]:
            cs = self.checks.get(f"{a[4].split('/')[1]}#{a[2]}", GREEN)
            if "--required" in a and self.required is not None:
                return done(self.required)
            return done(cs if "--required" in a or not isinstance(cs, list) else cs + ([self.bot] if self.bot else []))
        if a[:1] == ["api"] and "/contents/docs-version.txt" in a[1]:
            pin = self.pins.get(a[1].split("/")[2])
            return done(f"onepeek-docs@{pin}\n") if pin else subprocess.CompletedProcess(a, 1, "", "HTTP 404")
        if a[:1] == ["api"] and a[2].endswith("/commits?per_page=100"):
            return done("".join(c + "\n" for c in self.docs_commits.get(int(a[2].split("/")[4]), [])))
        if a[:2] == ["api", "graphql"]:
            f = dict(x.split("=", 1) for x in a if "=" in x and not x.startswith("query"))
            return done("".join(json.dumps([t, 1]) + "\n" for t in self.threads.get(f"{f['repo']}#{f['n']}", [])))
        if a[:2] == ["pr", "view"] and a[-1] == "state":
            return done({"state": self.states[f"{a[4].split('/')[1]}#{a[2]}"]})
        if a[:2] == ["pr", "view"]:
            return done({"createdAt": "2026-11-06T00:00:00Z", "mergedAt": "2026-11-06T03:30:00Z",
                         "comments": [{"body": "Round 1 findings"}, {"body": "Round 2: ready"}, {"body": "ok"}]})
        if a[:1] == ["api"] and a[2].endswith("/comments?per_page=100"):
            return done("101\n102\n")
        if a[:2] == ["run", "rerun"]:
            return done("")
        if a[:1] == ["api"] and "runners" in a[2]:
            return done("".join(json.dumps(["online" if i < self.online else "offline", True]) + "\n" for i in range(2)))
        if a[:1] == ["api"] and "status=queued" in a[1]:
            return done(str(self.queued if "onepeek-infra" in a[1] else 0))
        if a[:2] == ["run", "list"]:
            if "release.yml" in a and "onepeek-web" in a[3]:
                return done([{"status": "in_progress", "url": "r1"}, {"status": "completed", "url": "r0"}])
            return done([self.sandbox] if "sandbox.yml" in a and self.sandbox else [])
        raise AssertionError(f"unexpected gh call {args}")


def world(*prs):
    return [dict(p, _repo=r) for r, p in prs]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = pathlib.Path(self.tmp.name) / ".ship"
        self.addCleanup(self.tmp.cleanup)
        self.merged, self.merge_rc, self.merge_cmds = [], {}, []

        def merge(cmd, cwd):
            self.merged.append(cmd[2])
            self.merge_cmds.append(cmd)
            return subprocess.CompletedProcess(cmd, self.merge_rc.get(cmd[2], 0), "", "STOP: red check")
        p = mock.patch.object(sw, "MERGE_RUN", merge)
        p.start()
        self.addCleanup(p.stop)

    def reruns(self, fake):
        return [c[2] for c in fake.calls if c[:2] == ["run", "rerun"]]

    def run_pass(self, fake, now=NOW, state=None, reviews=None, dry=False, cfg=CFG):
        """One pass; every pull request has a recorded review at its head unless `reviews` says otherwise."""
        self.out.mkdir(parents=True, exist_ok=True)
        if state is not None:
            (self.out / "state.json").write_text(state if isinstance(state, str) else json.dumps(state))
        if reviews is None:
            reviews = {f"{p['_repo']}#{p['number']}": p["headRefOid"] for p in fake.prs}
        (self.out / "reviews.json").write_text(reviews if isinstance(reviews, str) else json.dumps(reviews))
        with mock.patch.object(sw, "GH_RUN", fake), mock.patch.dict(sw.os.environ, {"ONEPEEK_WORKSPACE": self.tmp.name}):
            return sw.one_pass(cfg, self.out, dry, now, pathlib.Path(self.tmp.name))


class Classify(unittest.TestCase):
    def facts(self, **kw):
        return dict(pr("onepeek-web", 5), repo="onepeek-web", label="onepeek-web#5", checks=GREEN, threads=[],
                    deps=[], dep_states={}, all_checks=GREEN + [BOT_DONE], docs_pr=None, **kw)

    def test_each_class(self):
        cases = [
            ("READY", "", {}),
            ("HOLD", "release", {"baseRefName": "release"}),
            ("HOLD", "draft", {"isDraft": True}),
            ("HOLD", "isDraft is not false", {"isDraft": None}),
            ("HOLD", "not in ship_watch.yaml", {"author": {"login": "someone"}}),
            ("HOLD", "not in ship_watch.yaml", {"author": {"login": "app/other", "is_bot": True}}),
            ("BLOCKED", "unreadable", {"error": "unreadable: HTTP 502"}),
            ("BLOCKED", "targets main", {"baseRefName": "main"}),
            ("READY", "", {"repo": "marketing-website", "label": "marketing-website#5", "baseRefName": "main",
                           "reviews": {"marketing-website#5": "h5"}}),
            ("BLOCKED", "targets dev, not main", {"repo": "marketing-website", "label": "marketing-website#5"}),
            ("BLOCKED", "fork", {"isCrossRepository": True}),
            ("BLOCKED", "CONFLICTING", {"mergeable": "CONFLICTING"}),
            ("BLOCKED", "waiting for onepeek-docs#3", {"dep_states": {"onepeek-docs#3": "OPEN"}}),
            ("BLOCKED", "docs pin is a commit of open onepeek-docs#2", {"docs_pr": "onepeek-docs#2"}),
            ("BLOCKED", "red: x", {"checks": [{"name": "x", "bucket": "fail"}]}),
            ("BLOCKED", "red: c", {"checks": [{"name": "c", "bucket": "cancel"}]}),
            ("BLOCKED", "red: y", {"checks": [{"name": "y", "bucket": "neutral"}]}),
            ("BLOCKED", "red: z", {"checks": [{"name": "z"}]}),
            ("BLOCKED", "pending: x", {"checks": [{"name": "x", "bucket": "pending"}]}),
            ("READY", "", {"checks": [{"name": "s", "bucket": "skipping"}] + GREEN}),
            ("BLOCKED", "no checks", {"checks": []}),
            ("BLOCKED", "1 unresolved review thread(s): a.py:3", {"threads": ["a.py:3"]}),
            ("BLOCKED", "no review at h5", {"reviews": {"onepeek-web#5": "h4"}}),
            ("BLOCKED", "no review at h5", {"reviews": {}}),
            ("BLOCKED", "no review at h5: CodeRabbit pending", {"all_checks": [{"name": "CodeRabbit", "bucket": "pending"}]}),
            ("BLOCKED", "CodeRabbit has not reported", {"all_checks": GREEN}),
            ("READY", "", {"reviews": {"onepeek-web#5": {"sha": "h5", "class": "mechanical"}}}),
            ("READY", "", {"reviews": {"onepeek-web#5": {"sha": "h5", "class": "docs only"}}}),
            ("BLOCKED", "no review at h5", {"reviews": {"onepeek-web#5": {"sha": "h4", "class": "docs only"}}}),
            ("BLOCKED", "no review at h5", {"reviews": {"onepeek-web#5": {"sha": "h5", "class": "normal code"}}}),
            ("READY", "", {"author": {"login": "app/dependabot", "is_bot": True}, "all_checks": GREEN, "reviews": {}}),
            ("BLOCKED", "no review at h5", {"author": {"login": "app/dependabot", "is_bot": False}, "reviews": {}}),
            # operating-model §3, ruling T-53: a hotfix needs green checks only; review and threads come after the merge
            ("READY", "post-merge review due", {**HOT, "threads": ["a.py:3"], "reviews": {},
                                                "all_checks": [{"name": "CodeRabbit", "bucket": "pending"}]}),
            ("BLOCKED", "red: x", {**HOT, "checks": [{"name": "x", "bucket": "fail"}]}),
            ("BLOCKED", "pending: x", {**HOT, "checks": [{"name": "x", "bucket": "pending"}]}),
            ("BLOCKED", "no checks", {**HOT, "checks": []}),
            ("BLOCKED", "CONFLICTING", {**HOT, "mergeable": "CONFLICTING"}),
            ("HOLD", "not in ship_watch.yaml", {**HOT, "author": {"login": "someone"}}),
            ("HOLD", "release", {**HOT, "baseRefName": "release"}),
            ("BLOCKED", "1 unresolved review thread(s)", {"body": "Lane: hot fix", "threads": ["a.py:3"]}),
            ("BLOCKED", "no review at h5", {"body": "fix it\nLane: hotfix", "reviews": {}}),  # ruling T-53 round 3
            ("BLOCKED", "no review at h5", {"body": "Lane: hotfix\n---", "reviews": {}}),
            ("BLOCKED", "1 unresolved review thread(s)", {"body": "Lane: docs", "threads": ["a.py:3"]}),
            ("BLOCKED", "no review at h5", {"body": "Lane: docs", "reviews": {}}),
            ("READY", "post-merge review due", {"body": "<!-- hint -->\nLane: hotfix\nLane: feature", "reviews": {}}),
        ]
        for want, why_part, change in cases:
            with self.subTest(want=want, change=change):
                change = dict(change)
                cfg = dict(CFG, reviews=change.pop("reviews", {"onepeek-web#5": "h5"}))
                p = self.facts()
                p.update(change)
                cls, why = sw.classify(p, cfg)
                self.assertEqual(cls, want)
                self.assertIn(why_part, why)


class Queue(Base):
    def test_docs_first_then_the_fixed_order(self):
        fake = FakeGh(world(("onepeek-web", pr("onepeek-web", 4)), (".github", pr(".github", 2)),
                            ("onepeek-infra", pr("onepeek-infra", 3)), ("onepeek-docs", pr("onepeek-docs", 1))))
        self.run_pass(fake)
        self.assertEqual(self.merged, ["onepeek-docs#1", ".github#2", "onepeek-infra#3", "onepeek-web#4"])

    def test_a_failed_run_is_skipped_and_the_queue_goes_on(self):
        fake = FakeGh(world(("onepeek-docs", pr("onepeek-docs", 1)), ("onepeek-web", pr("onepeek-web", 4))))
        self.merge_rc["onepeek-docs#1"] = 1
        s = self.run_pass(fake)
        self.assertEqual(self.merged, ["onepeek-docs#1", "onepeek-web#4"])
        self.assertIn("skipped onepeek-docs#1: STOP: red check", s["actions"])
        self.assertIn("merged onepeek-web#4", s["actions"])
        self.assertEqual(self.merge_cmds[1][3:], ["--go", "--config", str(sw.HERE / "ship_watch.yaml")])

    def test_config_is_passed_to_the_merge_tool(self):
        self.run_pass(FakeGh(world(("onepeek-web", pr("onepeek-web", 4)))), cfg=dict(CFG, path="/x/other.yaml"))
        self.assertEqual(self.merge_cmds[0][3:], ["--go", "--config", "/x/other.yaml"])

    def test_never_merges_release_draft_hold_or_unresolved_threads(self):
        fake = FakeGh(world(("onepeek-web", pr("onepeek-web", 1, baseRefName="release")),
                            ("onepeek-web", pr("onepeek-web", 2, isDraft=True)),
                            ("onepeek-web", pr("onepeek-web", 3, author={"login": "app/other"})),
                            ("onepeek-web", pr("onepeek-web", 4)), ("onepeek-web", pr("onepeek-web", 5))),
                      threads={"onepeek-web#4": ["a.py"]})
        self.run_pass(fake)
        self.assertEqual(self.merged, ["onepeek-web#5"])
        self.merged.clear()
        with mock.patch.object(sw, "classify", lambda p, cfg: ("READY", "")):  # the guard before the merge holds alone
            self.run_pass(fake)
        self.assertEqual(self.merged, ["onepeek-web#3", "onepeek-web#5"])
        self.merged.clear()
        fake.prs[1]["isDraft"] = None  # not exactly false: the guard holds too
        with mock.patch.object(sw, "classify", lambda p, cfg: ("READY", "")):
            self.run_pass(fake)
        self.assertNotIn("onepeek-web#2", self.merged)

    def test_dry_run_merges_and_reruns_nothing(self):
        red = [{"name": "flaky / job", "bucket": "fail", "link": "https://x/runs/9/job/1"}]
        fake = FakeGh(world(("onepeek-web", pr("onepeek-web", 1)), ("onepeek-web", pr("onepeek-web", 2))),
                      checks={"onepeek-web#2": red})
        s = self.run_pass(fake, dry=True)
        self.assertEqual((self.merged, self.reruns(fake)), ([], []))
        self.assertEqual(s["actions"], ["would re-run onepeek-web#2@h2:flaky / job (run 9)", "would merge onepeek-web#1"])

    def test_measures_written_for_each_merge(self):
        self.run_pass(FakeGh(world(("onepeek-web", pr("onepeek-web", 1)))), state={"reruns": {"onepeek-web#1@h0:x": 1}})
        rows = [json.loads(x) for x in (self.out / "measures.jsonl").read_text().splitlines()]
        self.assertEqual(rows, [{"pr": "onepeek-web#1", "merged_at": "2026-11-06T03:30:00Z", "hours_open_to_merge": 3.5,
                                 "review_rounds": 2, "bot_threads": 2, "reruns": 1, "post_merge_review_due": False}])

    def test_a_hotfix_merges_past_threads_and_review_and_records_the_review_due(self):
        hot = {"body": "Lane: hotfix"}
        fake = FakeGh(world(("onepeek-web", pr("onepeek-web", 1, **hot)), ("onepeek-web", pr("onepeek-web", 2))),
                      threads={"onepeek-web#1": ["a.py"], "onepeek-web#2": ["b.py"]},
                      bot={"name": "CodeRabbit", "bucket": "pending"})
        s = self.run_pass(fake, reviews={})
        self.assertEqual(self.merged, ["onepeek-web#1"])
        self.assertIn("merged onepeek-web#1; post-merge review due", s["actions"])
        self.assertEqual(s["post_merge_review_due"], ["onepeek-web#1"])
        rows = [json.loads(x) for x in (self.out / "measures.jsonl").read_text().splitlines()]
        self.assertEqual([(r["pr"], r["post_merge_review_due"]) for r in rows], [("onepeek-web#1", True)])


class MergeTimeout(Base):
    """Ruling T-56: a merge run past MERGE_TIMEOUT_S is killed with its whole process group, alerted and measured,
    and the queue goes on."""

    def test_a_hung_run_and_its_children_are_killed(self):
        ticks = pathlib.Path(self.tmp.name) / "ticks"
        cmd = ["sh", "-c", f'(for i in $(seq 50); do echo x >> "{ticks}"; sleep 0.1; done) & wait']
        start = time.monotonic()
        with mock.patch.object(sw, "MERGE_TIMEOUT_S", 0.5), self.assertRaises(sw.MergeTimeout) as e:
            sw._merge_process(cmd, pathlib.Path(self.tmp.name))
        self.assertLess(time.monotonic() - start, 4)
        self.assertEqual(e.exception.args[0], 0.5)
        size = ticks.stat().st_size
        time.sleep(0.6)
        self.assertEqual(ticks.stat().st_size, size)  # the grandchild in the group stopped too

    def test_a_run_that_finishes_in_time_returns_its_output(self):
        r = sw._merge_process([sys.executable, "-c", "print('ok'); raise SystemExit(3)"], pathlib.Path(self.tmp.name))
        self.assertEqual((r.returncode, r.stdout), (3, "ok\n"))

    def test_the_queue_goes_on_after_a_timeout(self):
        def merge(cmd, cwd):
            self.merged.append(cmd[2])
            if cmd[2] == "onepeek-docs#1":
                raise sw.MergeTimeout(3600)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        fake = FakeGh(world(("onepeek-docs", pr("onepeek-docs", 1)), ("onepeek-web", pr("onepeek-web", 4))),
                      states={"onepeek-docs#1": "OPEN"})
        with mock.patch.object(sw, "MERGE_RUN", merge):
            s = self.run_pass(fake)
        self.assertEqual(self.merged, ["onepeek-docs#1", "onepeek-web#4"])
        self.assertIn("merge run for onepeek-docs#1 timed out after 3600 s", s["alerts"])
        self.assertIn("merged onepeek-web#4", s["actions"])
        rows = [json.loads(x) for x in (self.out / "measures.jsonl").read_text().splitlines()]
        self.assertEqual([r["pr"] for r in rows], ["onepeek-docs#1", "onepeek-web#4"])
        self.assertEqual(rows[0], {"pr": "onepeek-docs#1", "merge_timed_out_after_s": 3600, "post_merge_review_due": False,
                                   "merged": False})

    def interrupted(self, exc):
        """A merge run whose wait is broken by `exc` (Ctrl-C is KeyboardInterrupt) once the grandchild has ticked."""
        ticks = pathlib.Path(self.tmp.name) / f"ticks-{type(exc).__name__}"  # fresh for each subtest
        cmd = ["sh", "-c", f'(for i in $(seq 50); do echo x >> "{ticks}"; sleep 0.1; done) & wait']
        tries = [0]

        def fire(*_):  # every 50 ms; raises once the first tick is written, or after 200 tries (counted, pitfall 33)
            tries[0] += 1
            if (ticks.exists() and ticks.stat().st_size) or tries[0] >= 200:
                sw.signal.setitimer(sw.signal.ITIMER_REAL, 0)
                raise exc
        old = sw.signal.signal(sw.signal.SIGALRM, fire)
        self.addCleanup(sw.signal.signal, sw.signal.SIGALRM, old)
        sw.signal.setitimer(sw.signal.ITIMER_REAL, 0.05, 0.05)
        try:
            with self.assertRaises(type(exc)):
                sw._merge_process(cmd, pathlib.Path(self.tmp.name))
        finally:
            sw.signal.setitimer(sw.signal.ITIMER_REAL, 0)
        size = ticks.stat().st_size
        time.sleep(0.6)
        return size, ticks.stat().st_size

    def test_ctrl_c_or_any_error_in_the_wait_kills_the_group_and_goes_on_up(self):  # T-56 round 2
        for exc in (KeyboardInterrupt(), RuntimeError("boom")):
            with self.subTest(exc=type(exc).__name__):
                before, after = self.interrupted(exc)
                self.assertEqual(before, after)  # the grandchild stopped too

    def test_sigterm_or_sighup_kills_the_group_before_it_merges(self):  # #82 round 2 review, finding 1
        here = pathlib.Path(self.tmp.name)
        for sig in (sw.signal.SIGTERM, sw.signal.SIGHUP):
            with self.subTest(sig=sig.name):
                started, merged = here / f"started-{sig.name}", here / f"merged-{sig.name}"
                code = ("import importlib.util, pathlib, sys\n"
                        "spec = importlib.util.spec_from_file_location('ship_watch', sys.argv[1])\n"
                        "sw = importlib.util.module_from_spec(spec); spec.loader.exec_module(sw)\n"
                        "sw.exit_on_signals()\n"
                        "sw._merge_process(['sh', '-c', 'touch \"$0\"; sleep 1.5; touch \"$1\"', sys.argv[2], sys.argv[3]],"
                        " pathlib.Path('.'))\n")
                watcher = subprocess.Popen([sys.executable, "-c", code, sw.__file__, str(started), str(merged)],
                                           cwd=here, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                for _ in range(200):  # counted, never a wall-clock cap on turns (pitfall 33)
                    if started.exists():
                        break
                    time.sleep(0.05)
                self.assertTrue(started.exists())
                watcher.send_signal(sig)
                self.assertEqual(watcher.wait(timeout=10), 128 + sig)
                time.sleep(2.5)  # past the run's own merge at 1.5 s
                self.assertFalse(merged.exists())

    def test_a_group_holding_only_a_zombie_still_times_out(self):  # #82 round 2 review, finding 2 (macOS EPERM)
        class Exited(subprocess.Popen):
            def communicate(self, *a, **k):
                os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOWAIT)  # it exited and is not reaped: a zombie
                raise subprocess.TimeoutExpired(self.args, 0)
        with mock.patch.object(sw.subprocess, "Popen", Exited), self.assertRaises(sw.MergeTimeout):
            sw._merge_process(["true"], pathlib.Path(self.tmp.name))

    def test_a_timed_out_record_survives_a_pass_that_cannot_list_its_repo(self):  # #82 round 2 review, finding 3
        def merge(cmd, cwd):
            self.merged.append(cmd[2])
            raise sw.MergeTimeout(3600)
        fake = FakeGh(world(("onepeek-web", pr("onepeek-web", 4))), states={"onepeek-web#4": "OPEN"})
        with mock.patch.object(sw, "MERGE_RUN", merge):
            self.run_pass(fake)
            fake.fail = ("pr list -R OnePeek/onepeek-web",)
            self.run_pass(fake, now=NOW + 60)
            self.assertEqual(json.loads((self.out / "state.json").read_text())["timed_out"], {"onepeek-web#4": "h4"})
            fake.fail = ()
            s = self.run_pass(fake, now=NOW + 120)
        self.assertEqual(self.merged, ["onepeek-web#4"])
        self.assertEqual(s["prs"][0]["class"], "BLOCKED")

    def test_a_timed_out_hotfix_records_the_review_due_and_whether_it_merged(self):
        def merge(cmd, cwd):
            self.merged.append(cmd[2])
            raise sw.MergeTimeout(3600)
        for state, merged, due in (("MERGED", True, ["onepeek-web#4"]), ("OPEN", False, [])):
            with self.subTest(state=state):
                (self.out / "measures.jsonl").unlink(missing_ok=True)
                fake = FakeGh(world(("onepeek-web", pr("onepeek-web", 4, **HOT))), states={"onepeek-web#4": state})
                with mock.patch.object(sw, "MERGE_RUN", merge):
                    s = self.run_pass(fake, state={})
                row = json.loads((self.out / "measures.jsonl").read_text())
                self.assertEqual((row["post_merge_review_due"], row["merged"]), (True, merged))
                self.assertEqual(s["post_merge_review_due"], due)

    def test_an_unreadable_state_after_a_timeout_is_an_alert(self):
        def merge(cmd, cwd):
            raise sw.MergeTimeout(3600)
        fake = FakeGh(world(("onepeek-web", pr("onepeek-web", 4, **HOT))), fail=("--json state",))
        with mock.patch.object(sw, "MERGE_RUN", merge):
            s = self.run_pass(fake)
        self.assertTrue(any("state after the timed-out run unreadable" in a for a in s["alerts"]), s["alerts"])
        self.assertIsNone(json.loads((self.out / "measures.jsonl").read_text())["merged"])
        self.assertEqual(s["post_merge_review_due"], [])

    def test_a_timed_out_head_waits_for_a_new_head(self):
        def merge(cmd, cwd):
            self.merged.append(cmd[2])
            raise sw.MergeTimeout(3600)
        fake = FakeGh(world(("onepeek-web", pr("onepeek-web", 4))), states={"onepeek-web#4": "OPEN"})
        with mock.patch.object(sw, "MERGE_RUN", merge):
            self.run_pass(fake)
            s = self.run_pass(fake, now=NOW + 60)
        self.assertEqual(self.merged, ["onepeek-web#4"])
        self.assertEqual((s["prs"][0]["class"], s["prs"][0]["reason"]),
                         ("BLOCKED", "merge run timed out at h4; waits a new head"))
        fake.prs[0]["headRefOid"] = "h5"
        s = self.run_pass(fake, reviews={"onepeek-web#4": "h5"}, now=NOW + 120)
        self.assertEqual(self.merged, ["onepeek-web#4", "onepeek-web#4"])

    def test_the_merge_timeout_covers_the_checks_wait(self):
        self.assertEqual(sw.MERGE_TIMEOUT_S, sw.scs.CHECK_TIMEOUT_S + 15 * 60)

    def test_a_measures_write_error_does_not_abort_the_pass(self):  # #71 follow-up
        (self.out / "measures.jsonl").mkdir(parents=True)
        fake = FakeGh(world(("onepeek-docs", pr("onepeek-docs", 1)), ("onepeek-web", pr("onepeek-web", 4))))
        s = self.run_pass(fake)
        self.assertEqual(self.merged, ["onepeek-docs#1", "onepeek-web#4"])
        self.assertTrue(any(a.startswith("onepeek-docs#1: measures unreadable") for a in s["alerts"]), s["alerts"])


class WarnOnly(Base):
    """Decision 173: the sandbox checks warn but never block, while ship_watch.yaml lists them."""

    def infra(self, sandbox, other="pass", cfg=CFG, repo="onepeek-infra"):
        cs = [{"name": "tests", "bucket": other, "workflow": "checks", "link": "https://x/runs/1/job/1"},
              {"name": "apply-test", "bucket": sandbox, "workflow": "sandbox", "link": "https://x/runs/77/job/9"}]
        fake = FakeGh(world(("onepeek-infra", pr("onepeek-infra", 5))), checks={"onepeek-infra#5": cs})
        if repo != "onepeek-infra":
            fake = FakeGh(world((repo, pr(repo, 5))), checks={f"{repo}#5": cs})
        return self.run_pass(fake, cfg=cfg)

    def test_a_pending_warn_check_merges(self):
        self.infra("pending")
        self.assertEqual(self.merged, ["onepeek-infra#5"])

    def test_a_red_warn_check_merges_and_alerts(self):
        s = self.infra("fail")
        self.assertEqual(self.merged, ["onepeek-infra#5"])
        self.assertIn("sandbox red on onepeek-infra#5: https://x/runs/77", s["alerts"])
        self.assertEqual(s["warned"], [{"pr": "onepeek-infra#5", "check": "apply-test", "link": "https://x/runs/77/job/9"}])

    def test_any_other_red_check_blocks(self):
        s = self.infra("pass", other="fail")
        self.assertEqual(self.merged, [])
        self.assertEqual(s["prs"][0]["reason"], "red: tests")

    def test_without_the_list_the_warn_check_blocks_again(self):
        s = self.infra("pending", cfg=dict(CFG, warn=[]))
        self.assertEqual((self.merged, s["prs"][0]["reason"]), ([], "pending: apply-test"))
        self.assertFalse([a for a in s["alerts"] if "sandbox red" in a])

    def test_another_repo_or_workflow_is_not_warn_only(self):
        s = self.infra("pending", repo="onepeek-web")
        self.assertEqual((self.merged, s["prs"][0]["reason"]), ([], "pending: apply-test"))

    def test_the_list_is_read_from_ship_watch_yaml(self):
        cfg = sw.load_config(sw.HERE / "ship_watch.yaml")
        self.assertEqual(cfg["path"], str(sw.HERE / "ship_watch.yaml"))  # passed on to the merge tool (N3)
        self.assertIn(("onepeek-infra", "sandbox", "apply-test"), cfg["warn"])
        self.assertEqual({r for r, _, _ in cfg["warn"]}, {"onepeek-infra"})


class Rerun(Base):
    def red(self, name, deps="", state=None, dep_state="MERGED", n=7):
        body = "".join(f"Depends on: OnePeek/{d}\n" for d in deps.split()) if deps else ""
        fake = FakeGh(world(("onepeek-web", pr("onepeek-web", n, body=body))), states={d: dep_state for d in deps.split()},
                      checks={f"onepeek-web#{n}": [{"name": name, "bucket": "fail", "link": "https://x/runs/42/job/1"}]})
        return fake, self.run_pass(fake, state=state)

    def test_depends_on_reruns_once_its_dependencies_merged(self):
        fake, s = self.red("depends-on / depends-on", "onepeek-docs#3 .github#9")
        self.assertEqual(self.reruns(fake), ["42"])
        self.assertIn("onepeek-web#7@h7:depends-on / depends-on", json.loads((self.out / "state.json").read_text())["reruns"])

    def test_depends_on_not_rerun_while_a_dependency_is_open(self):
        fake, _ = self.red("depends-on / depends-on", "onepeek-docs#3", dep_state="OPEN")
        self.assertEqual(self.reruns(fake), [])

    def test_depends_on_not_rerun_without_named_dependencies(self):
        fake, _ = self.red("depends-on / depends-on")
        self.assertEqual(self.reruns(fake), [])

    def test_known_flake_reruns_once(self):
        fake, _ = self.red("flaky / job")
        self.assertEqual(self.reruns(fake), ["42"])

    def test_any_bucket_but_pass_skipping_or_pending_is_red_for_a_rerun(self):
        cs = [{"name": "flaky / job", "bucket": "neutral", "link": "https://x/runs/43/job/1"},
              {"name": "flaky / job", "bucket": "skipping", "link": "https://x/runs/44/job/1"}]
        fake = FakeGh(world(("onepeek-web", pr("onepeek-web", 8))), checks={"onepeek-web#8": cs})
        self.run_pass(fake)
        self.assertEqual(self.reruns(fake), ["43"])

    def test_other_red_check_not_rerun(self):
        fake, _ = self.red("tests / unit")
        self.assertEqual(self.reruns(fake), [])

    def test_one_run_with_two_red_checks_is_rerun_once(self):  # #71 follow-up
        cs = [{"name": n, "bucket": "fail", "link": "https://x/runs/42/job/1"} for n in ("flaky / job", "flaky / other")]
        fake = FakeGh(world(("onepeek-web", pr("onepeek-web", 7))), checks={"onepeek-web#7": cs})
        self.run_pass(fake, cfg=dict(CFG, known_flakes=["flaky / job", "flaky / other"]))
        self.assertEqual(self.reruns(fake), ["42"])
        self.assertEqual(set(json.loads((self.out / "state.json").read_text())["reruns"]),
                         {"onepeek-web#7@h7:flaky / job", "onepeek-web#7@h7:flaky / other"})

    def test_never_rerun_twice_on_the_same_head(self):
        fake, s = self.red("flaky / job", state={"reruns": {"onepeek-web#7@h7:flaky / job": NOW - 60}})
        self.assertEqual(self.reruns(fake), [])

    def test_a_warn_check_is_never_rerun(self):
        cs = [{"name": "apply-test", "bucket": "fail", "workflow": "sandbox", "link": "https://x/runs/42/job/1"}]
        fake = FakeGh(world(("onepeek-infra", pr("onepeek-infra", 7))), checks={"onepeek-infra#7": cs})
        self.run_pass(fake, cfg=dict(CFG, known_flakes=["apply-test"]))
        self.assertEqual(self.reruns(fake), [])


class Alerts(Base):
    def test_no_online_runner_alerts_after_ten_minutes(self):
        fake = FakeGh([], online=0, queued=3)
        s = self.run_pass(fake)
        self.assertFalse([a for a in s["alerts"] if "no online runner" in a])
        s = self.run_pass(fake, now=NOW + 10 * 60)
        self.assertIn("queued runs with no online runner for 10 min: {'onepeek-infra': 3}", s["alerts"])
        s = self.run_pass(FakeGh([], online=1, queued=3), now=NOW + 11 * 60)
        self.assertFalse([a for a in s["alerts"] if "no online runner" in a])

    def test_no_progress_for_two_hours(self):
        fake = FakeGh(world(("onepeek-web", pr("onepeek-web", 1, mergeable="CONFLICTING"))))
        self.run_pass(fake)
        s = self.run_pass(fake, now=NOW + 2 * 3600 - 1)
        self.assertFalse([a for a in s["alerts"] if "no progress" in a])
        s = self.run_pass(fake, now=NOW + 2 * 3600)
        self.assertIn("onepeek-web#1: no progress for 2.0 h (BLOCKED mergeable is CONFLICTING)", s["alerts"])

    def test_hold_never_raises_no_progress(self):
        fake = FakeGh(world(("onepeek-web", pr("onepeek-web", 1, isDraft=True))))
        self.run_pass(fake)
        s = self.run_pass(fake, now=NOW + 5 * 3600)
        self.assertEqual(s["prs"][0]["class"], "HOLD")
        self.assertFalse([a for a in s["alerts"] if "no progress" in a], s["alerts"])

    def test_a_required_check_red_twice_on_the_same_head(self):
        def checks(bucket, job):
            return {"onepeek-web#1": [{"name": "tests", "bucket": bucket, "link": f"https://x/runs/5/job/{job}"},
                                      {"name": "tests", "bucket": "fail", "link": "https://x/runs/6/job/1"}]}
        fake = FakeGh(world(("onepeek-web", pr("onepeek-web", 1))), checks=checks("fail", 1))
        self.run_pass(fake)
        s = self.run_pass(fake, now=NOW + 60)  # still the same red run: once
        self.assertFalse([a for a in s["alerts"] if "red twice" in a], s["alerts"])
        fake.checks = checks("pending", 2)  # someone re-ran it
        self.run_pass(fake, now=NOW + 120)
        fake.checks = checks("fail", 2)
        s = self.run_pass(fake, now=NOW + 180)
        self.assertEqual([a for a in s["alerts"] if "red twice" in a],
                         ["onepeek-web#1: required check tests red twice on h1"])
        fake.prs[0]["headRefOid"] = "h1b"  # a new head starts again
        s = self.run_pass(fake, now=NOW + 240)
        self.assertFalse([a for a in s["alerts"] if "red twice" in a], s["alerts"])

    def test_a_code_pull_request_pinned_to_an_open_docs_pull_request_waits(self):
        fake = FakeGh(world(("onepeek-docs", pr("onepeek-docs", 2)), ("onepeek-web", pr("onepeek-web", 1))),
                      pins={"onepeek-web": "a" * 40}, docs_commits={2: ["b" * 40, "a" * 40]})
        s = self.run_pass(fake)
        web = [p for p in s["prs"] if p["label"] == "onepeek-web#1"][0]
        self.assertEqual(web["class"], "BLOCKED")
        self.assertIn("open onepeek-docs#2", web["reason"])
        fake.pins = {"onepeek-web": "c" * 40}  # a pin on docs main
        s = self.run_pass(fake)
        self.assertEqual({p["label"]: p["class"] for p in s["prs"]}, {"onepeek-docs#2": "READY", "onepeek-web#1": "READY"})

    def test_red_sandbox_run(self):
        run = {"status": "completed", "conclusion": "failure", "url": "https://x/runs/5"}
        s = self.run_pass(FakeGh([], sandbox=run))
        self.assertIn("sandbox run red (failure): https://x/runs/5", s["alerts"])
        s = self.run_pass(FakeGh([], sandbox=dict(run, conclusion="success")))
        self.assertFalse([a for a in s["alerts"] if "sandbox" in a])

    def test_open_release_runs_are_reported(self):
        s = self.run_pass(FakeGh([]))
        self.assertEqual(s["ci"]["release_runs"], [{"status": "in_progress", "url": "r1", "repo": "onepeek-web"}])
        self.assertIn("Release runs open: onepeek-web r1", (self.out / "ship-status.md").read_text())

    def test_dependency_cycle(self):
        fake = FakeGh(world(("onepeek-web", pr("onepeek-web", 1, body="Depends on: OnePeek/onepeek-infra#2")),
                            ("onepeek-infra", pr("onepeek-infra", 2, body="Depends on: OnePeek/onepeek-web#1"))))
        s = self.run_pass(fake)
        self.assertTrue([a for a in s["alerts"] if "cycle" in a], s["alerts"])


class FailClosed(Base):
    def test_a_read_error_merges_and_reruns_nothing(self):
        for broken in ("actions/runners", "pr checks", "api graphql", "pr list -R OnePeek/onepeek-infra"):
            with self.subTest(broken):
                self.merged.clear()
                red = [{"name": "flaky / job", "bucket": "fail", "link": "https://x/runs/9/job/1"}]
                fake = FakeGh(world(("onepeek-web", pr("onepeek-web", 1)), ("onepeek-web", pr("onepeek-web", 2))),
                              checks={"onepeek-web#2": red}, fail=(broken,))
                s = self.run_pass(fake)
                self.assertEqual((self.merged, self.reruns(fake)), ([], []))
                self.assertTrue([a for a in s["alerts"] if a.startswith("read failed")], s["alerts"])

    def test_a_read_error_is_an_alert(self):
        for broken in ("actions/runners", "pr checks", "api graphql", "pr list -R OnePeek/onepeek-infra"):
            with self.subTest(broken):
                s = self.run_pass(FakeGh(world(("onepeek-web", pr("onepeek-web", 1))), fail=(broken,)))
                self.assertTrue([a for a in s["alerts"] if a.startswith("read failed")], s["alerts"])

    def test_every_odd_shape_is_a_read_error(self):
        one = world(("onepeek-web", pr("onepeek-web", 1)))
        no_field = world(("onepeek-web", {k: v for k, v in pr("onepeek-web", 1).items() if k != "isDraft"}))
        cases = {
            "missing field": (FakeGh(no_field), None, None),
            "checks not a list": (FakeGh(one, checks={"onepeek-web#1": {"name": "x"}}), None, None),
            "required check without bucket": (FakeGh(one), None, None),
            "check without bucket": (FakeGh(one, checks={"onepeek-web#1": [{"name": "x"}]}), None, None),
            "state.json corrupt": (FakeGh(one), "{not json", None),
            "state.json not an object": (FakeGh(one), "[1]", None),
            "reviews.json corrupt": (FakeGh(one), None, "{"),
            "reviews.json not an object": (FakeGh(one), None, "[]"),
        }
        cases["required check without bucket"][0].required = [{"name": "x"}]  # no bucket
        for name, (fake, state, reviews) in cases.items():
            with self.subTest(name):
                (self.out / "state.json").unlink(missing_ok=True)
                s = self.run_pass(fake, state=state, reviews=reviews)
                self.assertTrue([a for a in s["alerts"] if a.startswith("read failed")], s["alerts"])

    def test_the_loop_keeps_running_after_a_broken_pass(self):
        calls = []

        def one_pass(*a):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            raise KeyboardInterrupt
        with mock.patch.object(sw, "one_pass", one_pass), mock.patch.object(sw.time, "sleep"), \
                mock.patch.dict(sw.os.environ, {"ONEPEEK_WORKSPACE": self.tmp.name}), mock.patch("builtins.print"), \
                self.assertRaises(KeyboardInterrupt):
            sw.main([])
        self.assertEqual(len(calls), 2)

    def test_a_full_page_of_pull_requests_is_a_read_error(self):
        with mock.patch.object(sw, "PR_LIMIT", 1):
            s = self.run_pass(FakeGh(world(("onepeek-web", pr("onepeek-web", 1)))))
        self.assertTrue([a for a in s["alerts"] if "cut short" in a], s["alerts"])

    def test_the_layer_refuses_every_write(self):
        for args in (["pr", "merge", "1"], ["run", "cancel", "1"], ["pr", "close", "1"], ["-R", "x", "run", "cancel"],
                     ["pr", "comment", "1"],
                     ["api", "-X", "POST", "repos/x"], ["api", "--method=PATCH", "repos/x"], ["api", "repos/x", "-f", "a=b"],
                     ["api", "repos/x/pulls/1/reviews", "-f", "query=query {}"],
                     ["api", "graphql", "-f", "query=mutation { resolveReviewThread }"], ["api", "--input", "f", "x"],
                     ["api", "--input=f", "x"], ["api", "-XPOST", "repos/x"], ["api", "-X", "DELETE", "repos/x"],
                     ["api", "--method", "PUT", "repos/x"], ["api", "repos/x", "-fa=b"], ["api", "repos/x", "-Fa=1"],
                     ["api", "repos/x", "--field=a=b"], ["api", "repos/x", "--raw-field", "a=b"], ["api", "repos/x", "-ifa=b"],
                     ["api", "graphql", "-fquery=mutation { x }"], ["api", "graphql", "--raw-field=query=mutation{x}"],
                     ["api", "graphql", "-f", "query=query { x }", "-f", "operationName=Do"],
                     ["api", "graphql", "-f", "query=query { x } mutation { y }"], ["api", "graphql"]):
            with self.subTest(args):
                self.assertFalse(sw.allowed(args))
                with self.assertRaises(sw.ReadError):
                    sw.gh(*args)
        for args in (["pr", "list"], ["run", "rerun", "1"], ["api", "repos/x"], ["api", "-X", "GET", "repos/x"], ["api", "--method=get", "repos/x"],
                     ["api", "--paginate", "repos/x", "--jq", ".[].sha"], ["api", "repos/x", "-H", "Accept: raw"],
                     ["api", "graphql", "-fquery=query { x }"], ["api", "graphql", "--raw-field=query=query { x }"],
                     ["api", "graphql", "-f", f"query={sw.scs.REVIEW_THREADS}", "-F", "n=1"]):
            self.assertTrue(sw.allowed(args), args)


class Main(Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(sw, "exit_on_signals")
        self.signals = p.start()
        self.addCleanup(p.stop)

    def test_main_turns_sigterm_and_sighup_into_an_exit(self):  # #82 round 2 review, finding 1
        ws = pathlib.Path(self.tmp.name)
        with mock.patch.object(sw, "one_pass", side_effect=KeyboardInterrupt), \
                mock.patch.dict(sw.os.environ, {"ONEPEEK_WORKSPACE": str(ws)}), self.assertRaises(KeyboardInterrupt):
            sw.main(["--once", "--dry-run", "--out", str(ws / "o")])
        self.signals.assert_called_once_with()

    def test_first_run_is_dry_then_merges(self):
        ws = pathlib.Path(self.tmp.name)
        (ws / ".github" / ".git").mkdir(parents=True)
        (ws / ".ship").mkdir()
        (ws / ".ship" / "reviews.json").write_text('{"onepeek-web#1": "h1"}')
        fake = FakeGh(world(("onepeek-web", pr("onepeek-web", 1))))
        with mock.patch.object(sw, "GH_RUN", fake), mock.patch.dict(sw.os.environ, {"ONEPEEK_WORKSPACE": str(ws)}), \
                mock.patch("builtins.print"):
            self.assertEqual(sw.main(["--once"]), 0)
            self.assertEqual(self.merged, [])
            self.assertEqual(sw.main(["--once"]), 0)
            self.assertEqual(self.merged, ["onepeek-web#1"])
            self.assertTrue((ws / ".ship" / "ship-status.md").read_text().startswith("# Ship status"))
            self.assertEqual(json.loads((ws / ".ship" / "ship-status.json").read_text())["mode"], "go")
            fake.fail = ("actions/runners",)
            self.assertEqual(sw.main(["--once", "--go"]), 1)

    def test_once_writes_the_status_and_exits_1_on_a_read_error(self):
        ws = pathlib.Path(self.tmp.name) / "w"
        ws.mkdir()
        fake = FakeGh(world(("onepeek-web", pr("onepeek-web", 1))))
        (ws / ".ship").mkdir()
        (ws / ".ship" / "reviews.json").write_text('{"onepeek-web#1": "h1"}')
        with mock.patch.object(sw, "GH_RUN", fake), mock.patch.dict(sw.os.environ, {"ONEPEEK_WORKSPACE": str(ws)}), \
                mock.patch("builtins.print"):
            self.assertEqual(sw.main(["--once"]), 0)
            self.assertTrue((ws / ".ship" / "ship-status.md").read_text().startswith("# Ship status"))
            self.assertEqual(json.loads((ws / ".ship" / "ship-status.json").read_text())["prs"][0]["class"], "READY")
            fake.fail = ("actions/runners",)
            self.assertEqual(sw.main(["--once", "--dry-run"]), 1)

    def test_status_never_inside_a_repo(self):
        repo = pathlib.Path(self.tmp.name) / "r"
        (repo / ".git").mkdir(parents=True)
        with self.assertRaises(SystemExit):
            sw.out_dir(str(repo / "out"))
        self.assertEqual(sw.out_dir(str(pathlib.Path(self.tmp.name) / "o")).name, "o")


if __name__ == "__main__":
    unittest.main()
