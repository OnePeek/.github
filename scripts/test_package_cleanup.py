"""Tests for package-cleanup.yml (foundation-playbook §8a C3): which versions go, the dry run, and a 403 failing."""
from __future__ import annotations

import contextlib
import datetime
import io
import json
import os
import pathlib
import urllib.error
import unittest
from unittest import mock

import yaml

WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / ".github/workflows/package-cleanup.yml"
DOC = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
STEP = DOC["jobs"]["cleanup"]["steps"][0]
SCRIPT = STEP["run"].split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
NOW = datetime.datetime(2026, 11, 4, tzinfo=datetime.timezone.utc)


def load() -> dict:
    ns: dict = {"__name__": "package_cleanup"}
    exec(compile(SCRIPT, str(WORKFLOW), "exec"), ns)  # the workflow's own code, not a copy
    return ns


def v(i: int, name: str, days_old: float) -> dict:
    return {"id": i, "name": name, "created_at": (NOW - datetime.timedelta(days=days_old)).isoformat().replace("+00:00", "Z")}


class Plan(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = load()["plan"]

    def test_old_snapshots_beyond_the_latest_ten_go(self) -> None:
        snaps = [v(i, f"0.3.0-next.2026{i:04d}", 40 + i) for i in range(15)]  # all older than 30 days
        gone = self.plan(snaps, NOW)
        self.assertEqual(sorted(x["id"] for x in gone), [10, 11, 12, 13, 14])  # the 5 oldest

    def test_versions_younger_than_30_days_stay_even_beyond_ten(self) -> None:
        snaps = [v(i, f"0.3.0-next.{i}", 2 + i) for i in range(15)]  # 2..16 days old
        self.assertEqual(self.plan(snaps, NOW), [])

    def test_only_versions_both_old_and_beyond_ten_go(self) -> None:
        snaps = [v(i, f"0.3.0-next.{i}", d) for i, d in enumerate([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 20, 31, 45])]
        self.assertEqual([x["id"] for x in self.plan(snaps, NOW)], [11, 12])

    def test_ten_are_kept_per_kind(self) -> None:
        snaps = [v(i, f"1.0.0-next.{i}", 50 + i) for i in range(11)]
        previews = [v(100 + i, f"0.0.0-pr{i}.abc1234", 50 + i) for i in range(11)]
        gone = self.plan(snaps + previews, NOW)
        self.assertEqual(sorted(x["id"] for x in gone), [10, 110])

    def test_released_and_other_versions_are_never_touched(self) -> None:
        others = [v(i, name, 400) for i, name in enumerate(
            ["1.0.0", "0.3.0", "1.0.0-rc.1", "0.0.0-next.1x", "2.0.0-beta.next.1"] * 4)]
        self.assertEqual(self.plan(others, NOW), [])


class Run(unittest.TestCase):
    def fake(self, calls: list, versions: list, fail: int | None = None, fail_on=lambda method, url: True):
        def urlopen(req, timeout=0):
            calls.append((req.get_method(), req.full_url))
            if fail and fail_on(req.get_method(), req.full_url):
                raise urllib.error.HTTPError(req.full_url, fail, "no", {}, io.BytesIO(b""))
            body = json.dumps(versions).encode() if req.get_method() == "GET" else b""
            return mock.MagicMock(__enter__=lambda s: mock.MagicMock(read=lambda: body), __exit__=lambda *a: False)
        return urlopen

    def run_main(self, dry: str, versions: list, fail: int | None = None, packages: str = "api-client", **kw) -> list:
        ns, calls = load(), []
        env = {"OWNER": "OnePeek", "PACKAGES": packages, "DRY_RUN": dry, "GH_TOKEN": "t"}
        old = [v(i, f"0.3.0-next.{i}", 40 + i) for i in range(12)] if versions is None else versions
        with mock.patch.dict(os.environ, env), mock.patch("urllib.request.urlopen", self.fake(calls, old, fail, **kw)), \
                mock.patch("datetime.datetime", wraps=datetime.datetime) as dt:
            dt.now.return_value = NOW
            ns["main"]()
        return calls

    def test_a_dry_run_lists_and_deletes_nothing(self) -> None:
        calls = self.run_main("true", None)
        self.assertEqual([m for m, _ in calls], ["GET"])

    def test_a_real_run_deletes_exactly_the_planned_versions(self) -> None:
        calls = self.run_main("false", None)
        deletes = [u for m, u in calls if m == "DELETE"]
        self.assertEqual(deletes, [f"https://api.github.com/orgs/OnePeek/packages/npm/api-client/versions/{i}" for i in (10, 11)])

    def test_a_403_fails_the_run(self) -> None:
        with self.assertRaises(SystemExit) as stop:
            self.run_main("false", [], fail=403)
        self.assertIn("HTTP 403", str(stop.exception))

    def test_a_404_for_a_package_not_yet_published_is_a_notice_and_the_others_still_run(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            calls = self.run_main("false", None, fail=404, packages="tokens ui",
                                  fail_on=lambda method, url: "/npm/tokens/" in url)
        self.assertIn("::notice::@onepeek/tokens is not published yet (HTTP 404)", out.getvalue())
        self.assertEqual([u.split("/npm/")[1] for m, u in calls if m == "DELETE"], ["ui/versions/10", "ui/versions/11"])

    def test_a_404_on_a_delete_still_fails(self) -> None:
        with self.assertRaises(SystemExit) as stop:
            self.run_main("false", None, fail=404, fail_on=lambda method, url: method == "DELETE")
        self.assertIn("HTTP 404", str(stop.exception))

    def test_pull_requests_are_always_a_dry_run(self) -> None:
        self.assertEqual(STEP["env"]["DRY_RUN"], "${{ inputs.dry-run || github.event_name == 'pull_request' }}")
        self.assertEqual(DOC["jobs"]["cleanup"]["permissions"]["packages"], "write")


if __name__ == "__main__":
    unittest.main()
