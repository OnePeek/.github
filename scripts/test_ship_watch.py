"""Tests for workspace/ship_watch.py: classification, alerts, outputs, fail closed (no network)."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "ship_watch", pathlib.Path(__file__).resolve().parents[1] / "workspace" / "ship_watch.py")
sw = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sw)
CFG = {"repos": ["onepeek-docs", ".github", "onepeek-infra", "onepeek-web"], "authors": ["Karan-98-Mehta"],
       "known_flakes": ["flaky / job"], "depends_on_check": "depends-on / depends-on"}
NOW = 1_800_000_000.0
GREEN = [{"name": "depends-on / depends-on", "bucket": "pass", "link": "https://x/runs/11/job/1"}]


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

    def __init__(self, prs, checks=None, threads=None, states=None, online=1, queued=0, sandbox=None, fail=()):
        self.prs, self.checks, self.threads, self.states = prs, checks or {}, threads or {}, states or {}
        self.online, self.queued, self.sandbox, self.fail, self.calls = online, queued, sandbox, fail, []

    def __call__(self, args):
        self.calls.append(args)
        if any(f in " ".join(args) for f in self.fail):
            return subprocess.CompletedProcess(args, 1, "", "HTTP 502")
        a = args
        if a[:2] == ["pr", "list"]:
            return done([p for p in self.prs if p["_repo"] == a[3].split("/")[1]])
        if a[:2] == ["pr", "checks"]:
            return done(self.checks.get(f"{a[4].split('/')[1]}#{a[2]}", GREEN))
        if a[:2] == ["api", "graphql"]:
            f = dict(x.split("=", 1) for x in a if "=" in x and not x.startswith("query"))
            return done("".join(json.dumps([t, 1]) + "\n" for t in self.threads.get(f"{f['repo']}#{f['n']}", [])))
        if a[:2] == ["pr", "view"] and a[-1] == "state":
            return done({"state": self.states[f"{a[4].split('/')[1]}#{a[2]}"]})
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

    def run_pass(self, fake, now=NOW, state=None):
        if state is not None:
            self.out.mkdir(parents=True, exist_ok=True)
            (self.out / "state.json").write_text(json.dumps(state))
        with mock.patch.object(sw, "GH_RUN", fake):
            return sw.one_pass(CFG, self.out, now)


class Classify(unittest.TestCase):
    def facts(self, **kw):
        return dict(pr("onepeek-web", 5), repo="onepeek-web", label="onepeek-web#5", checks=GREEN, threads=[],
                    deps=[], dep_states={}, **kw)

    def test_each_class(self):
        cases = {
            "READY": {},
            "HOLD:release": {"baseRefName": "release"},
            "HOLD:draft": {"isDraft": True},
            "HOLD:not in ship_watch.yaml": {"author": {"login": "someone"}},
            "BLOCKED:unreadable": {"error": "unreadable: HTTP 502"},
            "BLOCKED:targets main": {"baseRefName": "main"},
            "BLOCKED:fork": {"isCrossRepository": True},
            "BLOCKED:CONFLICTING": {"mergeable": "CONFLICTING"},
            "BLOCKED:waiting for onepeek-docs#3": {"dep_states": {"onepeek-docs#3": "OPEN"}},
            "BLOCKED:red: x": {"checks": [{"name": "x", "bucket": "fail"}]},
            "BLOCKED:pending: x": {"checks": [{"name": "x", "bucket": "pending"}]},
            "BLOCKED:no checks": {"checks": []},
            "BLOCKED:1 unresolved review thread(s): a.py:3": {"threads": ["a.py:3"]},
        }
        for want, change in cases.items():
            with self.subTest(want):
                p = self.facts()
                p.update(change)
                cls, why = sw.classify(p, CFG)
                self.assertEqual(cls, want.split(":")[0])
                self.assertIn(want.partition(":")[2], why)


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
    def test_a_read_error_is_an_alert(self):
        for broken in ("actions/runners", "pr checks", "api graphql", "pr list -R OnePeek/onepeek-infra"):
            with self.subTest(broken):
                s = self.run_pass(FakeGh(world(("onepeek-web", pr("onepeek-web", 1))), fail=(broken,)))
                self.assertTrue([a for a in s["alerts"] if a.startswith("read failed")], s["alerts"])

    def test_a_full_page_of_pull_requests_is_a_read_error(self):
        with mock.patch.object(sw, "PR_LIMIT", 1):
            s = self.run_pass(FakeGh(world(("onepeek-web", pr("onepeek-web", 1)))))
        self.assertTrue([a for a in s["alerts"] if "cut short" in a], s["alerts"])

    def test_the_layer_refuses_every_write(self):
        for args in (["pr", "merge", "1"], ["run", "rerun", "1"], ["pr", "close", "1"], ["-R", "x", "run", "cancel"],
                     ["pr", "comment", "1"],
                     ["api", "-X", "POST", "repos/x"], ["api", "--method=PATCH", "repos/x"], ["api", "repos/x", "-f", "a=b"],
                     ["api", "repos/x/pulls/1/reviews", "-f", "query=query {}"],
                     ["api", "graphql", "-f", "query=mutation { resolveReviewThread }"], ["api", "--input", "f", "x"]):
            with self.subTest(args):
                self.assertFalse(sw.allowed(args))
                with self.assertRaises(sw.ReadError):
                    sw.gh(*args)
        for args in (["pr", "list"], ["api", "repos/x"],
                     ["api", "graphql", "-f", f"query={sw.scs.REVIEW_THREADS}", "-F", "n=1"]):
            self.assertTrue(sw.allowed(args), args)


class Main(Base):
    def test_once_writes_the_status_and_exits_1_on_a_read_error(self):
        ws = pathlib.Path(self.tmp.name)
        fake = FakeGh(world(("onepeek-web", pr("onepeek-web", 1))))
        with mock.patch.object(sw, "GH_RUN", fake), mock.patch.dict(sw.os.environ, {"ONEPEEK_WORKSPACE": str(ws)}), \
                mock.patch("builtins.print"):
            self.assertEqual(sw.main(["--once"]), 0)
            self.assertTrue((ws / ".ship" / "ship-status.md").read_text().startswith("# Ship status"))
            self.assertEqual(json.loads((ws / ".ship" / "ship-status.json").read_text())["prs"][0]["class"], "READY")
            fake.fail = ("actions/runners",)
            self.assertEqual(sw.main(["--once"]), 1)

    def test_status_never_inside_a_repo(self):
        repo = pathlib.Path(self.tmp.name) / "r"
        (repo / ".git").mkdir(parents=True)
        with self.assertRaises(SystemExit):
            sw.out_dir(str(repo / "out"))
        self.assertEqual(sw.out_dir(str(pathlib.Path(self.tmp.name) / "o")).name, "o")


if __name__ == "__main__":
    unittest.main()
