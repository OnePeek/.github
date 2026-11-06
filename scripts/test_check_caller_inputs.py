"""Tests for scripts/check_caller_inputs.py and its pins.yml step (delivery-pipelines.md §6.4a, F35)."""
from __future__ import annotations

import contextlib
import io
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

import yaml

import check_caller_inputs as cci

WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / ".github/workflows/pins.yml"
CALLEE = """on:
  workflow_call:
    inputs:
      scope: {type: string, required: true}
      action: {type: string, required: true, default: take}
      runner: {type: string, default: github}
    secrets:
      APP_ID: {required: true}
      OPTIONAL_KEY:
jobs:
  j:
    runs-on: ubuntu-latest
    steps:
      - run: true
"""


def git_env() -> dict[str, str]:
    """The environment without Git's repository-local variables (pitfall 27)."""
    local = subprocess.run(["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True).stdout.split()
    return {k: v for k, v in os.environ.items() if k not in local}


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.shared = self.tmp / "shared"
        (self.shared / ".github/workflows").mkdir(parents=True)
        (self.shared / ".github/workflows/lock.yml").write_text(CALLEE)
        (self.shared / ".github/workflows/plain.yml").write_text("on: [push]\njobs: {}\n")
        (self.shared / ".github/workflows/broken.yml").write_text("on: [workflow_call\n")
        (self.shared / ".github/workflows/bare.yml").write_text("on: workflow_call\njobs: {}\n")
        env = git_env()
        for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "a"]):
            subprocess.run(["git", *args], cwd=self.shared, env=env, check=True, capture_output=True)
        self.sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.shared, env=env, capture_output=True, text=True,
                                  check=True).stdout.strip()
        self.repo = self.tmp / "repo"
        (self.repo / ".github/workflows").mkdir(parents=True)

    def call(self, body: str, file: str = "lock.yml", ref: str | None = None, comment: str = " # v1.16.0") -> str:
        return f"    uses: OnePeek/.github/.github/workflows/{file}@{self.sha if ref is None else ref}{comment}\n" + body

    def caller(self, *jobs: str, name: str = "release.yml") -> list[str]:
        text = "on: [pull_request]\njobs:\n" + "".join(f"  j{i}:\n{j}" for i, j in enumerate(jobs))
        (self.repo / ".github/workflows" / name).write_text(text)
        return cci.check(self.repo, self.shared)


OK_WITH = "    with: {scope: infra, action: take}\n    secrets: {APP_ID: x}\n"


class Inputs(Fixture):
    def test_a_missing_required_input_is_one_line_with_file_line_job_and_tag(self) -> None:
        out = self.caller(self.call("    with: {action: take}\n    secrets: inherit\n"))
        self.assertEqual(out, ["release.yml:4: j0 calls lock.yml@v1.16.0 without required input scope"])

    def test_required_wins_over_a_default(self) -> None:
        out = self.caller(self.call("    with: {scope: infra}\n    secrets: inherit\n"))
        self.assertEqual(out, ["release.yml:4: j0 calls lock.yml@v1.16.0 without required input action"])

    def test_a_default_making_an_input_optional_is_never_flagged(self) -> None:
        self.assertEqual(self.caller(self.call(OK_WITH)), [])  # `runner` has a default and no `required: true`

    def test_an_unknown_input(self) -> None:
        out = self.caller(self.call("    with: {scope: a, action: b, scopes: c}\n    secrets: inherit\n"))
        self.assertEqual(out, ["release.yml:4: j0 calls lock.yml@v1.16.0 with unknown input scopes"])

    def test_no_with_at_all_lists_every_required_input(self) -> None:
        out = self.caller(self.call("    secrets: inherit\n"))
        self.assertEqual([o.rsplit(" ", 1)[1] for o in out], ["scope", "action"])


class Secrets(Fixture):
    def test_a_required_secret_missing(self) -> None:
        out = self.caller(self.call("    with: {scope: a, action: b}\n"))
        self.assertEqual(out, ["release.yml:4: j0 calls lock.yml@v1.16.0 without required secret APP_ID"])
        out = self.caller(self.call("    with: {scope: a, action: b}\n    secrets: {OPTIONAL_KEY: y}\n"))
        self.assertEqual(out, ["release.yml:4: j0 calls lock.yml@v1.16.0 without required secret APP_ID"])

    def test_inherit_covers_a_required_secret(self) -> None:
        self.assertEqual(self.caller(self.call("    with: {scope: a, action: b}\n    secrets: inherit\n")), [])

    def test_an_unknown_secret(self) -> None:
        out = self.caller(self.call("    with: {scope: a, action: b}\n    secrets: {APP_ID: x, APP_KEY: y}\n"))
        self.assertEqual(out, ["release.yml:4: j0 calls lock.yml@v1.16.0 with unknown secret APP_KEY"])


class FailClosed(Fixture):
    def test_a_pin_commit_not_found(self) -> None:
        out = self.caller(self.call(OK_WITH, ref="e" * 40))
        self.assertEqual(out, ["release.yml:4: j0 calls lock.yml@v1.16.0: pin commit not found"])

    def test_a_ref_that_is_not_a_commit_sha(self) -> None:
        for ref in ("v1.16.0", "main", self.sha.upper(), ""):
            with self.subTest(ref=ref):
                out = self.caller(self.call(OK_WITH, ref=ref, comment=""))
                self.assertEqual(len(out), 1)
                self.assertTrue(out[0].endswith(": ref is not a commit sha"), out)

    def test_a_file_not_at_the_pin(self) -> None:
        out = self.caller(self.call(OK_WITH, file="gone.yml"))
        self.assertEqual(out, ["release.yml:4: j0 calls gone.yml@v1.16.0: file not found"])

    def test_a_callee_that_does_not_parse(self) -> None:
        self.assertEqual(self.caller(self.call("", file="broken.yml")), ["release.yml:4: j0 calls broken.yml@v1.16.0: cannot parse"])

    def test_a_caller_that_does_not_parse(self) -> None:
        (self.repo / ".github/workflows/bad.yml").write_text("jobs:\n  j: [\n")
        self.assertEqual(len(self.caller(self.call(OK_WITH))), 1)
        self.assertTrue(cci.check(self.repo, self.shared)[0].startswith("bad.yml:"))
        self.assertTrue(cci.check(self.repo, self.shared)[0].endswith(": cannot parse"))

    def test_not_a_reusable_workflow(self) -> None:
        out = self.caller(self.call("", file="plain.yml"))
        self.assertEqual(out, ["release.yml:4: j0 calls plain.yml@v1.16.0: not a reusable workflow"])

    def test_on_workflow_call_as_a_bare_string_declares_nothing(self) -> None:
        self.assertEqual(self.caller(self.call("", file="bare.yml")), [])
        out = self.caller(self.call("    with: {a: b}\n", file="bare.yml"))
        self.assertEqual(out, ["release.yml:4: j0 calls bare.yml@v1.16.0 with unknown input a"])

    def test_a_repo_dir_that_does_not_exist_fails(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = cci.main([str(self.tmp / "nowhere"), "--shared", str(self.shared)])
        self.assertEqual(rc, 1)
        self.assertIn("not a directory", out.getvalue())


class Calls(Fixture):
    def test_a_local_call_is_read_from_the_callers_tree(self) -> None:
        (self.repo / ".github/workflows/lock.yml").write_text(CALLEE)
        out = self.caller("    uses: ./.github/workflows/lock.yml\n    with: {scope: a}\n    secrets: inherit\n")
        self.assertEqual(out, ["release.yml:4: j0 calls ./.github/workflows/lock.yml without required input action"])

    def test_a_local_call_to_a_missing_file(self) -> None:
        out = self.caller("    uses: ./.github/workflows/gone.yml\n")
        self.assertEqual(out, ["release.yml:4: j0 calls ./.github/workflows/gone.yml: file not found"])

    def test_without_a_tag_comment_the_label_is_twelve_hex(self) -> None:
        out = self.caller(self.call("    with: {action: b}\n    secrets: inherit\n", comment=""))
        self.assertEqual(out, [f"release.yml:4: j0 calls lock.yml@{self.sha[:12]} without required input scope"])

    def test_quoted_flow_and_letter_case_forms_are_found_with_their_line(self) -> None:
        text = ("on:\n  pull_request:\njobs:\n  steps-only:\n    runs-on: x\n    steps: [{uses: actions/checkout@v1}]\n"
                f"  flow: {{uses: 'onepeek/.GITHUB/.github/workflows/lock.yml@{self.sha}', with: {{action: b}}, secrets: inherit}}\n")
        (self.repo / ".github/workflows/c.yml").write_text(text)
        out = cci.check(self.repo, self.shared)
        self.assertEqual(out, [f"c.yml:7: flow calls lock.yml@{self.sha[:12]} without required input scope"])

    def test_every_call_in_every_file_is_checked_and_main_exits_1(self) -> None:
        (self.repo / ".github/workflows/a.yml").write_text("jobs:\n  x:\n" + self.call("    secrets: inherit\n"))
        (self.repo / ".github/workflows/b.yml").write_text("jobs:\n  ok:\n" + self.call(OK_WITH) + "  y:\n" + self.call(""))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = cci.main([str(self.repo), "--shared", str(self.shared)])
        lines = out.getvalue().splitlines()
        self.assertEqual(rc, 1)
        self.assertEqual({line.split(":")[0] for line in lines}, {"a.yml", "b.yml"})
        self.assertTrue(any(line.startswith("b.yml:7: y calls") for line in lines), lines)
        for f in ("a.yml", "b.yml"):
            (self.repo / ".github/workflows" / f).write_text("jobs:\n  ok:\n" + self.call(OK_WITH))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cci.main([str(self.repo), "--shared", str(self.shared)]), 0)

    def test_the_infra_release_lock_call_passes_once_it_passes_scope(self) -> None:
        """release-lock.yml's required inputs and secrets (v1.16.0 on), as infra's release.yml calls it (F35)."""
        (self.repo / ".github/workflows/release-lock.yml").write_text(
            "on:\n  workflow_call:\n    inputs:\n" + "".join(f"      {k}: {{type: string, required: true}}\n" for k in
                                                         ("environment", "action", "scope", "role-to-assume"))
            + "      runner: {type: string, default: github}\n    secrets:\n"
            "      AGENTS_APP_CLIENT_ID: {required: true}\n      AGENTS_APP_PRIVATE_KEY: {required: true}\njobs: {}\n")
        body = ("    uses: ./.github/workflows/release-lock.yml\n    with:\n      environment: preprod\n      action: take\n"
                "      role-to-assume: r\n      runner: github\n    secrets: inherit\n")
        self.assertEqual(self.caller(body), ["release.yml:4: j0 calls ./.github/workflows/release-lock.yml without required input scope"])
        self.assertEqual(self.caller(body.replace("      runner:", "      scope: infra\n      runner:")), [])


class PinsStep(unittest.TestCase):
    def step(self) -> dict:
        steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["pins"]["steps"]
        return next(s for s in steps if "check_caller_inputs.py" in str(s.get("run")))

    def test_it_runs_on_every_event_and_not_as_the_last_step(self) -> None:
        steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["pins"]["steps"]
        step = self.step()
        self.assertNotIn("if", step)
        self.assertLess(steps.index(step), len(steps) - 1)
        self.assertGreater(steps.index(step), next(i for i, s in enumerate(steps) if (s.get("with") or {}).get("path") == "_shared"))

    def test_it_uses_the_pinned_checkout_or_this_repo(self) -> None:
        run = self.step()["run"]
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        for where in ("_shared/scripts", "scripts"):
            (d / where).mkdir(parents=True)
            (d / where / "check_caller_inputs.py").write_text(f"import sys; print('{where}', *sys.argv[1:])\n")
        for sha, want in (("a" * 40, "_shared/scripts . --shared _shared"), ("", "scripts . --shared .")):
            r = subprocess.run(["bash", "-eo", "pipefail", "-c", run], cwd=d, capture_output=True, text=True,
                               env={"SHA": sha, "PATH": os.environ["PATH"]})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), want)


if __name__ == "__main__":
    unittest.main()
