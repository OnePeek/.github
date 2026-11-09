"""The release order's read side, scripts/release_order.py (onepeek-docs architecture/lld/infra/foundation-playbook.md
§5.4a "The release order", "Settled", "The run's GitHub reads and their grants", "Plan-relevant paths"), run against a
fake `gh` and a fake `aws` on PATH: no live call.
Run: python -m unittest discover -s scripts -p 'test_*.py'"""

import datetime
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_order as ro  # noqa: E402

SCRIPT = Path(__file__).resolve().parent / "release_order.py"
INFRA = "repos/OnePeek/onepeek-infra"
RUNS = f"{INFRA}/actions/workflows/release.yml/runs?branch=release"
HEAD, OLD, OTHER = "a" * 40, "b" * 40, "c" * 40
NOW = datetime.datetime(2026, 11, 10, 12, 0, tzinfo=datetime.timezone.utc)

# A fake `gh api <path>`: answers from responses.json ({path: {"json": ..., "rc": n, "stderr": s, "sleep": s}}); a path
# it does not know is a 404. Every call is logged to `calls`.
FAKE_GH = r"""#!/usr/bin/env python3
import json, os, sys, time
d = os.environ["FAKE_DIR"]
open(os.path.join(d, "calls"), "a").write("gh " + " ".join(sys.argv[1:]) + "\n")
r = json.load(open(os.path.join(d, "responses.json"))).get(sys.argv[2] if len(sys.argv) > 2 else "")
if r is None:
    sys.stderr.write("gh: Not Found (HTTP 404)\n"); sys.exit(1)
time.sleep(r.get("sleep", 0))
if r.get("rc", 0):
    sys.stderr.write(r.get("stderr", "")); sys.exit(r["rc"])
print(r["raw"] if "raw" in r else json.dumps(r["json"]))
"""
FAKE_AWS = r"""#!/usr/bin/env python3
import os, sys
d = os.environ["FAKE_DIR"]
open(os.path.join(d, "calls"), "a").write("aws " + " ".join(sys.argv[1:]) + "\n")
mode = os.environ.get("FAKE_KEY", "absent")
if mode == "held":
    print('{"ETag": "x"}'); sys.exit(0)
if mode == "absent":
    sys.stderr.write("An error occurred (404) when calling the HeadObject operation: Not Found\n"); sys.exit(254)
sys.stderr.write("An error occurred (403) when calling the HeadObject operation: Forbidden\n"); sys.exit(254)
"""


def iso(t: datetime.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def run_(id_, sha, status="completed", conclusion="success", ago=60):
    t = NOW - datetime.timedelta(minutes=ago)
    return {"id": id_, "head_sha": sha, "status": status, "conclusion": conclusion, "created_at": iso(t - datetime.timedelta(minutes=20)),
            "updated_at": iso(t)}


def page(runs, total=None):
    return {"json": {"total_count": len(runs) if total is None else total, "workflow_runs": runs}}


def jobs(*steps_per_job, conclusions=None):
    out = []
    for i, steps in enumerate(steps_per_job):
        conclusion = (conclusions or {}).get(i, "failure" if any(c == "failure" for _, c in steps) else "success")
        out.append({"id": 900 + i, "conclusion": conclusion, "steps": [{"name": n, "conclusion": c} for n, c in steps]})
    return {"json": {"total_count": len(out), "jobs": out}}


CAPPED = jobs([("Check the action", "success"), ("Take, release, freeze or thaw the release lock", "success"), (ro.CAP_STEP, "failure")])


class Fake:
    """Writes the fake tools and responses; `.calls` lists what was called."""

    def __init__(self, responses: dict, key: str = "absent"):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        for name, body in (("gh", FAKE_GH), ("aws", FAKE_AWS)):
            f = self.root / name
            f.write_text(body)
            f.chmod(f.stat().st_mode | stat.S_IEXEC)
        (self.root / "responses.json").write_text(json.dumps(responses))
        self.env = {"FAKE_DIR": str(self.root), "FAKE_KEY": key, "PATH": f"{self.root}:{os.environ['PATH']}"}

    def __enter__(self):
        self.saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.dir.cleanup()

    @property
    def calls(self) -> list[str]:
        p = self.root / "calls"
        return p.read_text().splitlines() if p.exists() else []


def responses(runs, head=HEAD, compare=None, extra=None, **kw) -> dict:
    out = {RUNS + "&per_page=100&page=1": page(runs), f"{INFRA}/commits/release": {"json": {"sha": head}}}
    if compare is not None:
        out[f"{INFRA}/compare/{OLD}...{head}"] = {"json": {"files": [{"filename": f} for f in compare]}}
    out.update(extra or {})
    return out


def verdict(resp: dict, key: str = "absent", scope: str = "infra", now=NOW, first_seen=None, independent=False):
    with Fake(resp, key) as f:
        v = ro.settled(scope, ro.GitHub("onepeek-preprod-deploy"), independent=independent, now=now, first_seen=first_seen)
        return v, f.calls


def seen_long_ago():
    return {HEAD: NOW - datetime.timedelta(minutes=6)}


class Settled(unittest.TestCase):
    def test_a_held_key_waits(self):
        v, calls = verdict(responses([run_(1, HEAD)]), key="held")
        self.assertEqual(v.verdict, "wait", v.reason)
        self.assertIn("aws s3api head-object --bucket onepeek-preprod-deploy --key locks/infra.json", calls)

    def test_a_queued_or_running_run_waits_even_on_a_later_page(self):
        for status in ("queued", "in_progress", "waiting"):
            v, _ = verdict(responses([run_(2, HEAD, status, None, ago=1), run_(1, OLD)]))
            self.assertEqual(v.verdict, "wait", status)
        first = [run_(1000 - i, OLD, ago=100 + i) for i in range(100)]
        resp = responses([], extra={RUNS + "&per_page=100&page=1": page(first, total=101),
                                    RUNS + "&per_page=100&page=2": page([run_(5, HEAD, "queued", None, ago=300)], total=101)})
        v, calls = verdict(resp)
        self.assertEqual(v.verdict, "wait", v.reason)
        self.assertEqual(sum("page=" in c for c in calls), 2)

    def test_a_partial_or_endless_run_list_fails_never_settles(self):
        v, _ = verdict(responses([], extra={RUNS + "&per_page=100&page=1": page([run_(1, HEAD)], total=3)}))
        self.assertEqual((v.verdict, v.error), ("fail", True), v.reason)
        full = {RUNS + f"&per_page=100&page={p}": page([run_(p * 1000 + i, OLD) for i in range(100)], total=10**6) for p in range(1, ro.MAX_PAGES + 1)}
        v, _ = verdict(responses([], extra=full))
        self.assertEqual((v.verdict, v.error), ("fail", True), v.reason)

    def test_the_heads_latest_run_succeeded_is_settled_and_reports_the_success_head(self):
        v, _ = verdict(responses([run_(2, HEAD), run_(1, OLD, conclusion="failure")]))
        self.assertEqual((v.verdict, v.success_head), ("settled", HEAD), v.reason)

    def test_the_heads_latest_run_failed_or_was_cancelled_fails(self):
        for conclusion in ("failure", "cancelled", "timed_out"):
            resp = responses([run_(2, HEAD, conclusion=conclusion, ago=30), run_(1, OLD)],
                             extra={f"{INFRA}/actions/runs/2/jobs?filter=latest&per_page=100&page=1": jobs([("apply", "failure")])})
            v, _ = verdict(resp)
            self.assertEqual(v.verdict, "fail", conclusion)
            self.assertEqual(v.success_head, OLD)

    def test_a_run_that_failed_only_at_the_cap_step_counts_as_queued_for_10_minutes(self):
        resp = lambda ago: responses([run_(2, HEAD, conclusion="failure", ago=ago), run_(1, OLD)],
                                     extra={f"{INFRA}/actions/runs/2/jobs?filter=latest&per_page=100&page=1": CAPPED})
        v, calls = verdict(resp(9))
        self.assertEqual(v.verdict, "wait", v.reason)
        self.assertTrue(any("/actions/runs/2/jobs?filter=latest" in c for c in calls))
        v, _ = verdict(resp(11))
        self.assertEqual(v.verdict, "fail", v.reason)

    def test_clock_skew_an_end_time_in_the_future_still_waits(self):
        resp = responses([run_(2, HEAD, conclusion="failure", ago=-30), run_(1, OLD)],
                         extra={f"{INFRA}/actions/runs/2/jobs?filter=latest&per_page=100&page=1": CAPPED})
        self.assertEqual(verdict(resp)[0].verdict, "wait")

    def test_a_cap_and_another_failed_step_or_job_is_failed_not_queued(self):
        both_steps = jobs([("Take", "failure"), (ro.CAP_STEP, "failure")])
        cap_then_another = jobs([(ro.CAP_STEP, "failure"), ("Post the summary", "failure")])
        two_jobs = jobs([(ro.CAP_STEP, "failure")], [("apply", "failure")])
        cancelled = jobs([(ro.CAP_STEP, "failure")], [("apply", "skipped")], conclusions={1: "cancelled"})
        renamed = jobs([("Wait cap reached", "failure")])
        for name, j in (("two steps", both_steps), ("the cap, then another step", cap_then_another), ("two jobs", two_jobs), ("a cancelled job", cancelled), ("another name", renamed)):
            resp = responses([run_(2, HEAD, conclusion="failure", ago=2), run_(1, OLD)],
                             extra={f"{INFRA}/actions/runs/2/jobs?filter=latest&per_page=100&page=1": j})
            self.assertEqual(verdict(resp)[0].verdict, "fail", name)

    def test_the_cap_rule_is_the_step_name_alone(self):
        self.assertTrue(ro.failed_only_at_cap(CAPPED["json"]["jobs"]))
        self.assertFalse(ro.failed_only_at_cap([]))
        self.assertFalse(ro.failed_only_at_cap([{"conclusion": "failure", "steps": [{"name": ro.CAP_STEP, "conclusion": "failure"},
                                                                                   {"name": "x", "conclusion": None}]}]))

    def test_a_head_with_no_run_waits_5_minutes_from_first_seen_then_infra_skips_only_without_plan_paths(self):
        resp = responses([run_(1, OLD)], compare=["README.md", "docs/x.md"])
        seen: dict = {}
        v, calls = verdict(resp, first_seen=seen)
        self.assertEqual(v.verdict, "wait", v.reason)
        self.assertEqual(seen, {HEAD: NOW})  # the check's own clock, never a GitHub time
        self.assertFalse(any("compare" in c for c in calls))
        v, _ = verdict(resp, now=NOW + datetime.timedelta(minutes=4, seconds=59), first_seen=seen)
        self.assertEqual(v.verdict, "wait")
        v, calls = verdict(resp, now=NOW + datetime.timedelta(minutes=5), first_seen=seen)
        self.assertEqual(v.verdict, "settled", v.reason)
        self.assertIn(f"gh api {INFRA}/compare/{OLD}...{HEAD}", calls)
        for path in ("modules/x/main.tf", "config/preprod.yaml", ".github/workflows/plan.yml", "scripts/node_images.py"):
            v, _ = verdict(responses([run_(1, OLD)], compare=["README.md", path]), first_seen=seen_long_ago())
            self.assertEqual(v.verdict, "fail", path)

    def test_a_compare_at_the_300_file_limit_counts_as_plan_relevant(self):
        v, _ = verdict(responses([run_(1, OLD)], compare=[f"docs/{i}.md" for i in range(300)]), first_seen=seen_long_ago())
        self.assertEqual(v.verdict, "fail", v.reason)
        v, _ = verdict(responses([run_(1, OLD)], compare=[f"docs/{i}.md" for i in range(299)]), first_seen=seen_long_ago())
        self.assertEqual(v.verdict, "settled", v.reason)

    def test_no_successful_run_to_compare_with_fails(self):
        v, _ = verdict(responses([run_(1, OLD, conclusion="cancelled")]), first_seen=seen_long_ago())
        self.assertEqual(v.verdict, "fail", v.reason)

    def test_an_app_head_with_no_run_is_never_settled(self):
        app = "repos/OnePeek/backend-service"
        resp = {f"{app}/actions/workflows/release.yml/runs?branch=release&per_page=100&page=1": page([run_(1, OLD)]),
                f"{app}/commits/release": {"json": {"sha": HEAD}}}
        v, calls = verdict(resp, scope="backend", first_seen=seen_long_ago())
        self.assertEqual(v.verdict, "fail", v.reason)
        self.assertFalse(any("compare" in c for c in calls))
        self.assertIn("aws s3api head-object --bucket onepeek-preprod-deploy --key locks/backend.json", calls)

    def test_the_latest_run_for_another_head_is_not_released(self):
        v, _ = verdict(responses([run_(2, OTHER), run_(1, HEAD)]))
        self.assertEqual(v.verdict, "fail", v.reason)

    def test_independent_needs_only_the_free_key_and_nothing_running(self):
        v, calls = verdict(responses([run_(1, OLD, conclusion="failure")]), independent=True)
        self.assertEqual(v.verdict, "settled", v.reason)
        self.assertFalse(any("commits/release" in c for c in calls))
        self.assertEqual(verdict(responses([run_(1, OLD)]), key="held", independent=True)[0].verdict, "wait")
        self.assertEqual(verdict(responses([run_(1, OLD, "queued", None)]), independent=True)[0].verdict, "wait")

    def test_a_read_error_or_timeout_fails_never_settles(self):
        cases = {
            "runs 502": {**responses([run_(1, HEAD)]), RUNS + "&per_page=100&page=1": {"rc": 1, "stderr": "HTTP 502"}},
            "head 404": {RUNS + "&per_page=100&page=1": page([run_(1, HEAD)])},
            "not json": {**responses([run_(1, HEAD)]), f"{INFRA}/commits/release": {"raw": "<html>"}},
            "bad sha": responses([run_(1, HEAD)], head="release"),
            "compare error": {**responses([run_(1, OLD)]), f"{INFRA}/compare/{OLD}...{HEAD}": {"rc": 1, "stderr": "HTTP 500"}},
            "compare no files": {**responses([run_(1, OLD)]), f"{INFRA}/compare/{OLD}...{HEAD}": {"json": {"status": "ahead"}}},
            "no time": {**responses([]), RUNS + "&per_page=100&page=1": page([{**run_(1, HEAD), "updated_at": None}])},
        }
        for name, resp in cases.items():
            v, _ = verdict(resp, first_seen=seen_long_ago())
            self.assertEqual((v.verdict, v.error), ("fail", True), name)
        v, _ = verdict(responses([run_(1, HEAD)]), key="forbidden")
        self.assertEqual((v.verdict, v.error), ("fail", True), v.reason)
        saved = ro.READ_TIMEOUT
        ro.READ_TIMEOUT = 1
        try:
            v, _ = verdict({**responses([run_(1, HEAD)]), f"{INFRA}/commits/release": {"json": {"sha": HEAD}, "sleep": 3}})
        finally:
            ro.READ_TIMEOUT = saved
        self.assertEqual((v.verdict, v.error), ("fail", True), v.reason)
        with Fake(responses([run_(1, HEAD)])):
            self.assertEqual(ro.settled("infra", ro.GitHub(None), now=NOW).verdict, "fail")  # no bucket: no key read

    def test_settled_makes_no_call_outside_the_reads_table(self):
        allowed = [r"^aws s3api head-object --bucket \S+ --key locks/(infra|backend|web|mcp)\.json$",
                   r"^gh api repos/OnePeek/[a-z-]+/actions/workflows/release\.yml/runs\?branch=release&per_page=100&page=\d+$",
                   r"^gh api repos/OnePeek/[a-z-]+/actions/runs/\d+/jobs\?filter=latest&per_page=100&page=\d+$",
                   r"^gh api repos/OnePeek/[a-z-]+/commits/release$",
                   r"^gh api repos/OnePeek/onepeek-infra/compare/[0-9a-f]{40}\.\.\.[0-9a-f]{40}$",
                   r"^gh api repos/OnePeek/[a-z-]+/commits/[0-9a-f]{40}$"]
        every = []
        for resp, seen in ((responses([run_(2, OLD, conclusion="failure", ago=3), run_(1, OLD)], compare=["x.md"],
                                      extra={f"{INFRA}/actions/runs/2/jobs?filter=latest&per_page=100&page=1": jobs([("apply", "failure")])}), seen_long_ago()),
                           (responses([run_(1, HEAD)]), None)):
            every += verdict(resp, first_seen=seen)[1]
        with Fake({f"repos/OnePeek/backend-service/commits/{HEAD}": {"json": {"commit": {"message": "Merge"}}}}) as f:
            ro.merge_lines(ro.GitHub(), "backend-service", HEAD)
            every += f.calls
        self.assertGreater(len(every), 6)
        for c in every:
            self.assertTrue(any(re.match(a, c) for a in allowed), c)


class MergeLines(unittest.TestCase):
    def test_the_lines_come_from_the_commit_api_message(self):
        msg = "Merge pull request #9\n\nrelease-independent-of: onepeek-infra\nrelease-after: backend-service\n"
        with Fake({f"repos/OnePeek/onepeek-web/commits/{HEAD}": {"json": {"commit": {"message": msg}}}}) as f:
            lines = ro.merge_lines(ro.GitHub(), "onepeek-web", HEAD)
            calls = f.calls
        self.assertEqual(lines, {ro.INDEPENDENT: {"onepeek-infra"}, ro.AFTER: {"backend-service"}})
        self.assertEqual(calls, [f"gh api repos/OnePeek/onepeek-web/commits/{HEAD}"])

    def test_a_missing_message_is_a_read_error_never_the_defaults(self):
        for body in ({"commit": {}}, {"sha": HEAD}, {"commit": {"message": None}}):
            with Fake({f"repos/OnePeek/onepeek-web/commits/{HEAD}": {"json": body}}):
                with self.assertRaises(ro.ReadError):
                    ro.merge_lines(ro.GitHub(), "onepeek-web", HEAD)
        with Fake({}):
            with self.assertRaises(ro.ReadError):
                ro.merge_lines(ro.GitHub(), "onepeek-web", HEAD)

    def test_no_line_means_the_defaults_and_any_other_release_line_fails(self):
        self.assertEqual(ro.declared("Merge pull request #9\n\nbody"), {ro.INDEPENDENT: set(), ro.AFTER: set()})
        for bad in ("release-indepedent-of: onepeek-infra", "release-after: onepeek-docs", "release-after:", "Release-After: x"):
            with self.assertRaises(ro.BadLines, msg=bad):
                ro.declared("Merge\n\n" + bad)


class Cli(unittest.TestCase):
    def cli(self, resp, *args, key="absent"):
        with Fake(resp, key) as f:
            out = subprocess.run([sys.executable, "-B", str(SCRIPT), *args], capture_output=True, text=True, timeout=60,
                                 env={**os.environ, "NAME": "onepeek-preprod"})
            return out, f.calls

    def test_settled_scope_infra_json_prints_the_verdict_reason_and_success_head(self):
        resp = responses([run_(2, HEAD), run_(1, OLD)])
        out, calls = self.cli(resp, "settled", "--scope", "infra", "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        doc = json.loads(out.stdout)
        self.assertEqual(set(doc), {"verdict", "reason", "success_head", "error"})
        self.assertEqual((doc["verdict"], doc["success_head"], doc["error"]), ("settled", HEAD, False))
        self.assertIn("aws s3api head-object --bucket onepeek-preprod-deploy --key locks/infra.json", calls)
        with Fake(resp):
            same = ro.settled("infra", ro.GitHub("onepeek-preprod-deploy"))
        self.assertEqual(doc, json.loads(same.as_json()))

    def test_a_read_error_prints_fail_with_error_true(self):
        out, _ = self.cli({}, "settled", "--scope", "infra", "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["verdict"], "fail")
        self.assertTrue(json.loads(out.stdout)["error"])

    def test_the_first_seen_file_keeps_the_5_minutes_across_calls(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen.json"
            resp = responses([run_(1, OLD)], compare=["README.md"])
            out, _ = self.cli(resp, "settled", "--scope", "infra", "--json", "--first-seen-file", str(path))
            self.assertEqual(json.loads(out.stdout)["verdict"], "wait")
            path.write_text(json.dumps({HEAD: json.loads(path.read_text())[HEAD] - 301}))
            out, _ = self.cli(resp, "settled", "--scope", "infra", "--json", "--first-seen-file", str(path))
            self.assertEqual(json.loads(out.stdout)["verdict"], "settled", out.stdout)


PLAN = """      - id: filter
        run: |
          if grep -qE '%s' <<<"${files}"; then
"""


class PlanPaths(unittest.TestCase):
    def test_the_list_is_the_plan_filter_and_drift_either_way_fails(self):
        same = "(" + "|".join(ro.PLAN_RELEVANT_PATHS) + ")"
        self.assertEqual(ro.path_drift(PLAN % same), [])
        more = "(" + "|".join(ro.PLAN_RELEVANT_PATHS + (r"^modules/",)) + ")"
        self.assertEqual(len(ro.path_drift(PLAN % more)), 1)
        fewer = "(" + "|".join(ro.PLAN_RELEVANT_PATHS[1:]) + ")"
        self.assertEqual(len(ro.path_drift(PLAN % fewer)), 1)
        with self.assertRaises(ValueError):
            ro.plan_filter("no filter here")

    def test_the_cli_reads_a_plan_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "plan.yml"
            p.write_text(PLAN % ("(" + "|".join(ro.PLAN_RELEVANT_PATHS) + ")"))
            self.assertEqual(ro.main(["check-paths", "--infra-plan", str(p)]), 0)
            p.write_text(PLAN % r"(\.tf$)")
            self.assertEqual(ro.main(["check-paths", "--infra-plan", str(p)]), 1)
            self.assertEqual(ro.main(["check-paths", "--infra-plan", str(Path(d) / "missing.yml")]), 1)

    def test_the_paths_match_what_the_filter_matches(self):
        for path, relevant in (("main.tf", True), ("a/b.tfbackend", True), ("x.hcl", True), ("config/ci.yaml", True),
                               ("images/x", True), ("policy/approvers.yaml", True), ("scripts/check_plan_policy.py", True),
                               (".github/workflows/plan.yml", True), ("scripts/other.py", False), ("README.md", False),
                               (".github/workflows/release.yml", False), ("main.tf.md", False)):
            self.assertEqual(bool(ro.PLAN_RELEVANT.search(path)), relevant, path)


if __name__ == "__main__":
    unittest.main()
