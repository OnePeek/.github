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
CFG = {"repos": ["onepeek-docs", ".github", "onepeek-infra", "onepeek-web"], "authors": ["Karan-98-Mehta", "app/dependabot"],
       "known_flakes": ["flaky / job"], "depends_on_check": "depends-on / depends-on"}
NOW = 1_800_000_000.0
GREEN = [{"name": "depends-on / depends-on", "bucket": "pass", "link": "https://x/runs/11/job/1"}]
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

    def run_pass(self, fake, now=NOW, state=None, reviews=None):
        """One pass; every pull request has a recorded review at its head unless `reviews` says otherwise."""
        self.out.mkdir(parents=True, exist_ok=True)
        if state is not None:
            (self.out / "state.json").write_text(state if isinstance(state, str) else json.dumps(state))
        if reviews is None:
            reviews = {f"{p['_repo']}#{p['number']}": p["headRefOid"] for p in fake.prs}
        (self.out / "reviews.json").write_text(reviews if isinstance(reviews, str) else json.dumps(reviews))
        with mock.patch.object(sw, "GH_RUN", fake), mock.patch.dict(sw.os.environ, {"ONEPEEK_WORKSPACE": self.tmp.name}):
            return sw.one_pass(CFG, self.out, now)


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
        for args in (["pr", "merge", "1"], ["run", "rerun", "1"], ["pr", "close", "1"], ["-R", "x", "run", "cancel"],
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
        for args in (["pr", "list"], ["api", "repos/x"], ["api", "-X", "GET", "repos/x"], ["api", "--method=get", "repos/x"],
                     ["api", "--paginate", "repos/x", "--jq", ".[].sha"], ["api", "repos/x", "-H", "Accept: raw"],
                     ["api", "graphql", "-fquery=query { x }"], ["api", "graphql", "--raw-field=query=query { x }"],
                     ["api", "graphql", "-f", f"query={sw.scs.REVIEW_THREADS}", "-F", "n=1"]):
            self.assertTrue(sw.allowed(args), args)


class Main(Base):
    def test_once_writes_the_status_and_exits_1_on_a_read_error(self):
        ws = pathlib.Path(self.tmp.name)
        fake = FakeGh(world(("onepeek-web", pr("onepeek-web", 1))))
        (ws / ".ship").mkdir()
        (ws / ".ship" / "reviews.json").write_text('{"onepeek-web#1": "h1"}')
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
