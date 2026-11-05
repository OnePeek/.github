"""The OSV gate (check_osv_results.py) and the reusable workflow that embeds it (osv-scan.yml)
(onepeek-docs architecture/lld/infra/foundation-playbook.md §12a slice 4 item 1).
Run: python -m unittest discover -s scripts -p 'test_*.py'"""
import contextlib
import datetime
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

import yaml

import check_osv_results as osv
from check_runner_labels import classify

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/osv-scan.yml"
TODAY = datetime.date(2026, 11, 4)


def vuln(vid="GHSA-1", fixed=True, word=None):
    events = [{"introduced": "0"}] + ([{"fixed": "2.0.0"}] if fixed else [])
    v = {"id": vid, "aliases": [], "affected": [{"package": {"name": "left-pad"}, "ranges": [{"type": "SEMVER", "events": events}]}]}
    if word:
        v["database_specific"] = {"severity": word}
    return v


def results(vulns, score=None):
    groups = [{"ids": [v["id"] for v in vulns], "max_severity": score}] if score is not None else []
    return {"results": [{"source": {"path": "pnpm-lock.yaml"}, "packages": [
        {"package": {"name": "left-pad", "version": "1.0.0"}, "vulnerabilities": vulns, "groups": groups}]}]}


def run(res=None, toml=None, today=TODAY, scan_exit=None):
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        if scan_exit is None:
            scan_exit = 1 if any(p.get("vulnerabilities") for r in (res or {}).get("results") or [] for p in r.get("packages") or []) else 0
        args = ["--scan-exit", str(scan_exit), "--today", today.isoformat(), str(d)]
        if res is not None:
            (d / "osv-results.json").write_text(json.dumps(res))
        args = ["--results", str(d / "osv-results.json"), *args]
        if toml is not None:
            (d / "osv-scanner.toml").write_text(toml)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = osv.main(args)
        return code, out.getvalue()


def waiver(extra='reason = "no fix path yet"\nignoreUntil = 2026-12-01\n', vid="GHSA-1"):
    return f'[[IgnoredVulns]]\nid = "{vid}"\n{extra}'


class Findings(unittest.TestCase):
    def test_no_findings_passes(self):
        self.assertEqual(run(results([]))[0], 0)

    def test_high_score_without_a_fix_blocks(self):
        self.assertEqual(run(results([vuln(fixed=False)], score="7.5"))[0], 1)

    def test_critical_score_blocks(self):
        self.assertEqual(run(results([vuln(fixed=False)], score="9.8"))[0], 1)

    def test_database_severity_word_blocks(self):
        self.assertEqual(run(results([vuln(fixed=False, word="HIGH")]))[0], 1)
        self.assertEqual(run(results([vuln(fixed=False, word="CRITICAL")]))[0], 1)

    def test_low_or_medium_without_a_fix_passes(self):
        self.assertEqual(run(results([vuln(fixed=False, word="MODERATE")], score="5.3"))[0], 0)
        self.assertEqual(run(results([vuln(fixed=False)]))[0], 0)

    def test_low_severity_with_a_fix_blocks(self):
        code, out = run(results([vuln(fixed=True)], score="3.1"))
        self.assertEqual(code, 1)
        self.assertIn("fix is available", out)

    def test_boundary_just_under_high_passes(self):
        self.assertEqual(run(results([vuln(fixed=False)], score="6.9"))[0], 0)

    def test_a_scanner_exit_other_than_0_or_1_fails_even_with_empty_results(self):
        for code in (2, 127, 128):
            got, out = run({"results": []}, scan_exit=code)
            self.assertEqual(got, 1, code)
            self.assertIn(f"osv-scanner exited {code}", out)
        self.assertEqual(run({"results": []}, scan_exit=0)[0], 0)
        self.assertEqual(run(results([vuln(fixed=False)]), scan_exit=1)[0], 0)

    def test_the_scan_exit_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            osv.main(["--results", "x.json"])

    def test_another_packages_fix_does_not_count(self):
        v = vuln(fixed=False)
        v["affected"].append({"package": {"name": "other-pkg"}, "ranges": [{"events": [{"introduced": "0"}, {"fixed": "1"}]}]})
        self.assertEqual(run(results([v]))[0], 0)

    def test_a_name_that_matches_no_affected_entry_has_no_fix(self):
        v = vuln()
        v["affected"][0]["package"]["name"] = "something-else"
        self.assertFalse(osv.has_fix(v, {"name": "left-pad"}))

    def test_pypi_names_are_normalised(self):
        v = {"id": "PYSEC-1", "affected": [{"package": {"name": "Zope.Interface", "ecosystem": "PyPI"},
                                            "ranges": [{"events": [{"introduced": "0"}, {"fixed": "6"}]}]}]}
        self.assertTrue(osv.has_fix(v, {"name": "zope-interface", "ecosystem": "PyPI"}))
        self.assertTrue(osv.has_fix(v, {"name": "zope_interface", "ecosystem": "PyPI"}))
        self.assertFalse(osv.has_fix(v, {"name": "zope-interfaces", "ecosystem": "PyPI"}))

    def test_missing_results_fails(self):
        code, out = run(None)
        self.assertEqual(code, 1)
        self.assertIn("did not run", out)


class Waivers(unittest.TestCase):
    CLEAN = results([])

    def test_a_valid_waiver_passes(self):
        self.assertEqual(run(self.CLEAN, waiver())[0], 0)

    def test_an_expired_waiver_fails(self):
        code, out = run(self.CLEAN, waiver('reason = "x"\nignoreUntil = 2026-11-03\n'))
        self.assertEqual(code, 1)
        self.assertIn("expired", out)

    def test_expiry_day_itself_is_still_valid(self):
        self.assertEqual(run(self.CLEAN, waiver('reason = "x"\nignoreUntil = 2026-11-04\n'))[0], 0)

    def test_a_waiver_without_a_reason_fails(self):
        code, out = run(self.CLEAN, waiver("ignoreUntil = 2026-12-01\n"))
        self.assertEqual(code, 1)
        self.assertIn("reason", out)
        self.assertEqual(run(self.CLEAN, waiver('reason = "  "\nignoreUntil = 2026-12-01\n'))[0], 1)

    def test_a_waiver_without_an_expiry_fails(self):
        code, out = run(self.CLEAN, waiver('reason = "x"\n'))
        self.assertEqual(code, 1)
        self.assertIn("ignoreUntil", out)

    def test_an_unreadable_toml_fails(self):
        self.assertEqual(run(self.CLEAN, "[[IgnoredVulns\n")[0], 1)


class SharedWorkflow(unittest.TestCase):
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    text = WORKFLOW.read_text(encoding="utf-8")

    def test_it_is_reusable_with_the_runner_input_and_light_class(self):
        self.assertIn("workflow_call", self.wf.get(True, self.wf.get("on")))
        inputs = self.wf[True]["workflow_call"]["inputs"]
        self.assertEqual(inputs["runner"]["default"], "github")
        job = self.wf["jobs"]["osv-scan"]
        self.assertEqual(classify(job["runs-on"], "inputs.runner", True), "light")
        self.assertLessEqual(job["timeout-minutes"], 30)

    def test_the_scanner_image_is_pinned_by_digest_in_the_form_the_bump_moves(self):
        import bump_ci_images as bump
        lines = [ln for ln in self.text.splitlines() if "osv-scanner-action@" in ln]
        self.assertEqual(len(lines), 1)
        m = bump.LINE.match(lines[0])
        self.assertIsNotNone(m, lines[0])
        self.assertEqual((m["name"], bump.tag_of(m["comment"])), ("ghcr.io/google/osv-scanner-action", "v2.7.0"))
        self.assertNotIn("uses: google/osv-scanner-action", self.text)

    def test_it_embeds_the_script_verbatim(self):
        src = (ROOT / "scripts/check_osv_results.py").read_text(encoding="utf-8")
        indented = "\n".join(("          " + line) if line else "" for line in src.splitlines())
        self.assertIn(indented, self.text)

    def test_the_scan_exit_code_reaches_the_decision_step(self):
        steps = self.wf["jobs"]["osv-scan"]["steps"]
        scan = next(s for s in steps if s.get("id") == "scan")
        self.assertTrue(scan["run"].startswith("set +e\n"))
        self.assertIn('echo "exit=$?" >> "$GITHUB_OUTPUT"', scan["run"])
        decide = steps[steps.index(scan) + 1]
        self.assertEqual(decide["env"]["SCAN_EXIT"], "${{ steps.scan.outputs.exit }}")
        self.assertIn('--scan-exit "${SCAN_EXIT}"', decide["run"])

    def test_the_scan_step_records_the_scanners_real_exit_code(self):
        import os
        import stat
        import subprocess
        scan = next(s for s in self.wf["jobs"]["osv-scan"]["steps"] if s.get("id") == "scan")
        for code in (0, 1, 128):
            with tempfile.TemporaryDirectory() as d:
                d = Path(d)
                (d / "docker").write_text(f"#!/bin/bash\nexit {code}\n")
                (d / "docker").chmod(stat.S_IRWXU)
                env = {**os.environ, "PATH": f"{d}:{os.environ['PATH']}", "GITHUB_OUTPUT": str(d / "out"), "image": "x"}
                r = subprocess.run(["bash", "-eo", "pipefail", "-c", scan["run"]], cwd=d, env=env, capture_output=True)
                self.assertEqual(r.returncode, 0)
                self.assertEqual((d / "out").read_text(), f"exit={code}\n")


if __name__ == "__main__":
    unittest.main()
