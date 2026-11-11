"""Tests for scripts/ready.py and ready.yml: the `ci / ready` summary (PIPE-SIMPLE v2 D6, charter principle 4).
Run: python -m unittest discover -s scripts -p 'test_*.py'"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

import yaml

import ready

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/ready.yml"
REF = "OnePeek/backend-service/.github/workflows/checks.yml@refs/pull/7/merge"
CALLER = """name: checks
on: pull_request
jobs:
  fast:
    uses: OnePeek/.github/.github/workflows/fast-checks.yml@{sha} # v1.27.0
  tests:
    runs-on: x
    steps: [{{run: "true"}}]
  build:
    if: ${{{{ false }}}}
    runs-on: x
    steps: [{{run: "true"}}]
  ready:
    name: ci
    if: ${{{{ always() }}}}
    needs: {needs}
    uses: OnePeek/.github/.github/workflows/ready.yml@{sha} # v1.27.0
    with:
      needs: ${{{{ toJSON(needs) }}}}
"""
ALL = "[fast, tests, build]"


def needs(**results: str) -> str:
    return json.dumps({job: {"result": r, "outputs": {}} for job, r in results.items()})


class Case(unittest.TestCase):
    def caller(self, text: str | None = None, name: str = "checks.yml") -> pathlib.Path:
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(d)], check=False))
        (d / ".github/workflows").mkdir(parents=True)
        (d / ".github/workflows" / name).write_text(text if text is not None else CALLER.format(sha="a" * 40, needs=ALL))
        return d

    def run_ready(self, need: str, text: str | None = None, ref: str = REF, **env: str) -> tuple[int, str]:
        return ready.run({"NEEDS": need, "WORKFLOW_REF": ref, **env}, self.caller(text))


class Results(Case):
    def test_all_success_passes_with_a_table(self) -> None:
        code, text = self.run_ready(needs(fast="success", tests="success", build="success"))
        self.assertEqual(code, 0, text)
        self.assertIn("| job | result | how to reproduce locally |", text)
        self.assertIn("| tests | success |  |", text)
        self.assertNotIn("::error::", text)

    def test_a_failed_job_fails_and_says_how_to_reproduce(self) -> None:
        code, text = self.run_ready(needs(fast="success", tests="failure", build="success"),
                                    REPRODUCE="tests: uv run pytest\nnot a line\n")
        self.assertEqual(code, 1)
        self.assertIn("| tests | failure | `uv run pytest` |", text)
        self.assertIn("::error::not ready: tests did not pass", text)

    def test_a_job_without_a_reproduce_line_gets_the_local_ci_command(self) -> None:
        code, text = self.run_ready(needs(fast="failure", tests="success", build="success"))
        self.assertEqual(code, 1)
        self.assertIn("| fast | failure | `vanguard local-ci <worktree> --jobs fast` |", text)

    def test_a_cancelled_job_fails(self) -> None:  # brief failure mode: needed job cancelled -> ready fails
        code, text = self.run_ready(needs(fast="success", tests="cancelled", build="success"))
        self.assertEqual(code, 1)
        self.assertIn("| tests | cancelled |", text)

    def test_an_unknown_or_missing_result_fails(self) -> None:
        for result in ("", "neutral", "SUCCESS", "success "):
            code, text = self.run_ready(needs(fast="success", tests=result, build="success"))
            self.assertEqual(code, 1, result)
            self.assertIn("unknown result", text)
        code, _ = self.run_ready(json.dumps({"fast": {"result": "success"}, "tests": {}, "build": {"result": "success"}}))
        self.assertEqual(code, 1)
        code, _ = self.run_ready(json.dumps({"fast": {"result": "success"}, "tests": "success", "build": {"result": "success"}}))
        self.assertEqual(code, 1)

    def test_a_skipped_job_passes_only_when_listed_skippable(self) -> None:  # brief failure mode: path-skipped job
        skipped = needs(fast="success", tests="success", build="skipped")
        code, text = self.run_ready(skipped)
        self.assertEqual(code, 1)
        self.assertIn("| build | skipped, and not listed skippable |", text)
        code, text = self.run_ready(skipped, SKIPPABLE="build")
        self.assertEqual(code, 0, text)
        self.assertIn("| build | skipped by its own if (listed skippable) |", text)

    def test_a_skippable_job_that_failed_still_fails(self) -> None:
        code, _ = self.run_ready(needs(fast="success", tests="success", build="failure"), SKIPPABLE="build")
        self.assertEqual(code, 1)

    def test_a_skippable_name_that_is_not_needed_or_not_a_job_id_fails(self) -> None:
        all_green = needs(fast="success", tests="success", build="success")
        for bad in ("buidl", "build $(x)", "a|b"):
            code, text = self.run_ready(all_green, SKIPPABLE=bad)
            self.assertEqual(code, 1, bad)
            self.assertIn("::error::", text)

    def test_a_reproduce_command_cannot_break_the_table(self) -> None:
        code, text = self.run_ready(needs(fast="success", tests="failure", build="success"),
                                    REPRODUCE="tests: a | b `c`")
        self.assertEqual(code, 1)
        row = next(line for line in text.splitlines() if line.startswith("| tests |"))
        self.assertEqual(row.count("|"), 4, row)


class Needs(Case):
    def test_unparsable_empty_or_odd_needs_fail(self) -> None:
        for bad in ("", "not json", "[]", "{}", "null", '"x"', json.dumps({"bad name!": {"result": "success"}})):
            code, text = self.run_ready(bad)
            self.assertEqual(code, 1, bad)
            self.assertIn("::error::", text)


class Workflow(Case):
    def test_a_job_missing_from_needs_fails(self) -> None:
        text = CALLER.format(sha="a" * 40, needs="[fast, tests]")
        code, out = self.run_ready(needs(fast="success", tests="success"), text)
        self.assertEqual(code, 1)
        self.assertIn("must need every other job: missing ['build']", out)

    def test_needs_context_that_differs_from_the_file_fails(self) -> None:
        code, out = self.run_ready(needs(fast="success", tests="success"))
        self.assertEqual(code, 1)
        self.assertIn("is not the jobs of", out)

    def test_a_ready_job_without_always_fails(self) -> None:
        text = CALLER.format(sha="a" * 40, needs=ALL).replace("${{ always() }}", "${{ success() }}")
        code, out = self.run_ready(needs(fast="success", tests="success", build="success"), text)
        self.assertEqual(code, 1)
        self.assertIn("needs `if: ${{ always() }}`", out)

    def test_no_or_two_ready_jobs_fail(self) -> None:
        text = CALLER.format(sha="a" * 40, needs=ALL)
        no = text.replace("workflows/ready.yml", "workflows/other.yml")
        two = text + "  ready2:\n    needs: [fast]\n    uses: ./.github/workflows/ready.yml\n"
        for t in (no, two):
            code, out = self.run_ready(needs(fast="success", tests="success", build="success"), t)
            self.assertEqual(code, 1)
            self.assertIn("exactly one job", out)

    def test_a_local_call_and_any_letter_case_count(self) -> None:
        text = CALLER.format(sha="a" * 40, needs=ALL).replace(
            "OnePeek/.github/.github/workflows/ready.yml@" + "a" * 40, "./.github/workflows/ready.yml")
        code, out = self.run_ready(needs(fast="success", tests="success", build="success"), text)
        self.assertEqual(code, 0, out)
        text = CALLER.format(sha="a" * 40, needs=ALL).replace("OnePeek/.github/.github/workflows/ready.yml",
                                                              "onepeek/.GITHUB/.github/workflows/ready.yml")
        self.assertEqual(self.run_ready(needs(fast="success", tests="success", build="success"), text)[0], 0)

    def test_an_unreadable_or_odd_workflow_ref_fails(self) -> None:
        green = needs(fast="success", tests="success", build="success")
        for ref in ("", "checks.yml", "OnePeek/x/.github/workflows/../../etc/passwd@x",
                    "OnePeek/backend-service/.github/workflows/missing.yml@refs/heads/dev"):
            code, out = self.run_ready(green, ref=ref)
            self.assertEqual(code, 1, ref)
            self.assertIn("::error::", out)
        code, out = self.run_ready(green, "jobs: [1, 2]\n")
        self.assertEqual(code, 1)
        code, out = self.run_ready(green, "jobs: {a: [\n")
        self.assertEqual(code, 1)
        self.assertIn("cannot read the calling workflow", out)


class Main(Case):
    def test_main_writes_the_summary_and_exits_with_the_code(self) -> None:
        d = self.caller()
        summary = d / "summary.md"
        env = {"PATH": os.environ["PATH"], "NEEDS": needs(fast="success", tests="failure", build="success"),
               "WORKFLOW_REF": REF, "GITHUB_STEP_SUMMARY": str(summary)}
        out = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/ready.py")], cwd=d, env=env,
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 1, out.stderr)
        self.assertIn("| tests | failure |", summary.read_text())
        self.assertNotIn("::error::", summary.read_text())
        self.assertIn("::error::not ready", out.stdout)


class ReadyWorkflow(unittest.TestCase):
    DOC = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))

    def test_one_job_named_ready_with_the_runner_input_and_no_hard_coded_label(self) -> None:  # F413
        self.assertEqual(list(self.DOC["jobs"]), ["ready"])
        runs_on = self.DOC["jobs"]["ready"]["runs-on"]
        self.assertEqual(runs_on, "${{ inputs.runner == 'onepeek' && 'onepeek-arm64-small' || inputs.runner == "
                                  "'canary' && 'onepeek-arm64-canary' || 'ubuntu-latest' }}")
        inputs = self.DOC[True]["workflow_call"]["inputs"]
        self.assertEqual(sorted(inputs), ["needs", "reproduce", "runner", "skippable"])
        self.assertTrue(inputs["needs"]["required"])

    def test_values_come_in_through_env_and_the_script_runs_at_the_pin(self) -> None:
        steps = self.DOC["jobs"]["ready"]["steps"]
        last = steps[-1]
        self.assertNotIn("${{", last["run"])
        self.assertEqual(last["env"]["NEEDS"], "${{ inputs.needs }}")
        self.assertEqual(last["env"]["WORKFLOW_REF"], "${{ github.workflow_ref }}")
        self.assertIn("_shared/scripts/ready.py", last["run"])
        self.assertIn("pyyaml==6.0.3", last["run"])
        shared = steps[2]
        self.assertEqual(shared["with"]["ref"], "${{ steps.version.outputs.sha }}")

    def test_this_repos_own_ready_job_needs_every_other_job(self) -> None:
        lint = yaml.safe_load((ROOT / ".github/workflows/workflow-lint.yml").read_text(encoding="utf-8"))
        job = lint["jobs"]["ready"]
        self.assertEqual(job["name"], "ci")
        self.assertEqual(job["if"], "${{ always() }}")
        self.assertEqual(sorted(job["needs"]), sorted(set(lint["jobs"]) - {"ready"}))
        self.assertEqual(job["with"]["needs"], "${{ toJSON(needs) }}")
        code, text = ready.run({"NEEDS": needs(**{j: "success" for j in job["needs"]}),
                                "WORKFLOW_REF": "OnePeek/.github/.github/workflows/workflow-lint.yml@refs/pull/1/merge"}, ROOT)
        self.assertEqual(code, 0, text)


if __name__ == "__main__":
    unittest.main()
