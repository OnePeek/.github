"""Tests of the tiny-job merge (decision 180; onepeek-docs delivery-pipelines.md §9.1 "Tiny jobs merged" and "Cancel only
pull-request runs"; orchestrator rulings R-S2i-1, R-S2i-2, R-S2i-3 and R-S2i-5, provisional, 2026-11-10):
.github/workflows/fast-checks.yml (one job `fast`), the composite actions under actions/ that it and the old check
workflows share, and workflow-lint.yml's merged `lint` job.
Run: python -m unittest discover -s scripts -p 'test_*.py'"""

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
ACTIONS = ROOT / "actions"
# delivery-pipelines.md §8.1 "How a tree-only job is skipped": the promotion pull request.
PROMOTION = "(github.event_name == 'pull_request' && github.base_ref == 'release' && github.head_ref == 'dev')"
# The secrets context inside an expression (not inputs.secrets or steps.secrets, and not prose).
SECRET_CONTEXT = r"\$\{\{[^}]*(?<![\w.-])secrets\."
# The checks fast-checks.yml can run: input, step id, composite action.
CHECKS = [("pins", "pins", "pins"), ("secrets", "secrets", "gitleaks"),
          ("no-prerelease-deps", "no-prerelease-deps", "no-prerelease-deps"), ("contract", "contract", "contract")]
# The old check workflows, kept with their job names, inputs and secrets until every caller has moved (expand first).
OLD = {
    "pins.yml": ("pins", ["runner"], ["AGENTS_APP_CLIENT_ID", "AGENTS_APP_PRIVATE_KEY"], "pins"),
    "secret-scan.yml": ("gitleaks", ["runner"], [], "gitleaks"),
    "contract.yml": ("contract", ["runner", "role", "base_ref"], ["AGENTS_APP_CLIENT_ID", "AGENTS_APP_PRIVATE_KEY"], "contract"),
    "no-prerelease-deps.yml": ("no-prerelease-deps", ["runner"], [], "no-prerelease-deps"),
}


def load(name):
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def steps(name, job):
    return load(name)["jobs"][job]["steps"]


def by_id(step_list, step_id):
    return next(s for s in step_list if s.get("id") == step_id)


def results_step(step_list):
    return next(s for s in step_list if str(s.get("name", "")).startswith("Results"))


EXPR_TOKEN = re.compile(r"\s*(?:(&&|\|\||==|!=|!|\(|\))|'([^']*)'|(always\(\)|success\(\))|([A-Za-z_][\w.-]*))")


def evaluate(expr, ctx, condition=True):
    """Evaluates the expression subset these steps use (always(), contexts, 'strings', ==, !=, !, &&, ||, parentheses)
    the way GitHub does: && and || return an operand, a missing context value is ''; a step condition with no status
    function gets an implicit success(), false once an earlier step failed (ctx["__failed__"])."""
    body = expr.strip()
    implicit = condition and not re.search(r"\b(always|success|failure|cancelled)\(\)", body)
    assert body.startswith("${{") and body.endswith("}}"), expr
    body, out, pos = body[3:-2], [], 0
    while body[pos:].strip():
        m = EXPR_TOKEN.match(body, pos)
        assert m, body[pos:]
        op, text, always, name = m.groups()
        if op:
            out.append({"&&": " and ", "||": " or ", "!": " not "}.get(op, f" {op} "))
        elif text is not None:
            out.append(repr(text))
        elif always:
            out.append(" True " if always == "always()" else repr(not ctx.get("__failed__")))
        elif name in ("true", "false"):
            out.append(" True " if name == "true" else " False ")
        else:
            out.append(repr(ctx.get(name, "")))
        pos = m.end()
    value = eval("".join(out), {"__builtins__": {}})  # noqa: S307 - test-only, the tokens above are all it can see
    return value if not (implicit and ctx.get("__failed__")) else False


FAST = steps("fast-checks.yml", "fast")
LINT = steps("workflow-lint.yml", "lint")
RESULTS_RUN = results_step(FAST)["run"]


def results(lines, summary="ok"):
    """Runs the results step's script as Actions does (bash -e); summary: 'ok', or 'broken' for an unwritable file."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d, "summary") if summary == "ok" else Path(d, "missing-dir", "summary")
        env = {"PATH": os.environ["PATH"], "RESULTS": "\n".join(lines) + "\n", "GITHUB_STEP_SUMMARY": str(path)}
        out = subprocess.run(["bash", "-e", "-c", RESULTS_RUN], env=env, capture_output=True, text=True, timeout=30)
        return out, path.read_text() if path.exists() else None


def version(file, repo, pins_text, run_sha="e" * 40):
    """Runs the version step of `file` in a fake caller checkout whose workflow holds `pins_text`."""
    step = by_id(steps(file, OLD[file][0] if file in OLD else "fast"), "version")
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / ".github/workflows").mkdir(parents=True)
        (Path(d) / ".github/workflows/checks.yml").write_text(pins_text)
        output = Path(d, "output")
        env = {"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(output), **{k: str(v) for k, v in step["env"].items()}}
        env.update({"REPO": repo, "RUN_SHA": run_sha})
        out = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=d, env=env, capture_output=True, text=True, timeout=30)
        got = dict(line.split("=", 1) for line in output.read_text().split()) if output.exists() else {}
        return out, got.get("sha")


class Results(unittest.TestCase):
    """The last step: every check's result in the summary; the job fails when any check failed (R-S2i-1)."""

    def test_all_green_passes_and_lists_every_check(self):
        out, summary = results(["setup|true|success,success,success", "pins|true|success", "secrets|true|success",
                                "no-prerelease-deps|false|skipped", "contract|true|success"])
        self.assertEqual(out.returncode, 0, out.stdout)
        for line in ("| pins | success |", "| secrets | success |", "| no-prerelease-deps | not requested |",
                     "| contract | success |"):
            self.assertIn(line, summary)
            self.assertIn(line, out.stdout)  # the log holds the table too

    def test_an_early_failure_fails_the_job_and_the_later_checks_still_report(self):
        out, summary = results(["setup|true|success,success,success", "pins|true|failure", "secrets|true|success",
                                "contract|true|success"])
        self.assertEqual(out.returncode, 1)
        self.assertIn("| pins | failed |", summary)
        self.assertIn("| secrets | success |", summary)
        self.assertIn("| contract | success |", summary)
        self.assertIn("::error::A check failed or did not run", out.stdout)

    def test_a_failure_in_any_step_of_a_check_fails_it(self):
        out, summary = results(["setup|true|success", "service-list|true|success,failure,skipped,skipped"])
        self.assertEqual(out.returncode, 1)
        self.assertIn("| service-list | failed |", summary)

    def test_a_requested_check_that_did_not_run_fails_the_job(self):
        # Setup failed, so the check steps were skipped: never green by omission.
        out, summary = results(["setup|true|success,failure,skipped", "pins|true|skipped"])
        self.assertEqual(out.returncode, 1)
        self.assertIn("| setup | failed |", summary)
        self.assertIn("| pins | not run (skipped) |", summary)
        out, _ = results(["pins|true|cancelled"])
        self.assertEqual(out.returncode, 1)
        out, _ = results(["pins|true|"])
        self.assertEqual(out.returncode, 1)

    def test_a_tree_only_check_skipped_on_a_promotion_passes_and_says_why(self):
        out, summary = results(["setup|true|success", "pins|true|success", "secrets|excused|skipped"])
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertIn("| secrets | skipped: a dev -> release promotion pull request", summary)

    def test_an_excused_check_that_failed_still_fails(self):
        out, _ = results(["setup|true|success", "secrets|excused|failure"])
        self.assertEqual(out.returncode, 1)

    def test_no_check_requested_fails(self):
        out, summary = results(["setup|true|success", "pins|false|skipped", "secrets|false|skipped"])
        self.assertEqual(out.returncode, 1)
        self.assertIn("no check was requested", summary)

    def test_an_unknown_wanted_value_counts_as_wanted(self):
        out, _ = results(["setup|true|success", "pins||skipped"])
        self.assertEqual(out.returncode, 1)

    def test_an_unwritable_summary_still_fails_on_a_failed_check_and_passes_when_green(self):
        out, summary = results(["setup|true|success", "pins|true|failure"], summary="broken")
        self.assertEqual(out.returncode, 1)
        self.assertIsNone(summary)
        self.assertIn("::warning::Could not write the job summary", out.stdout)
        self.assertIn("| pins | failed |", out.stdout)
        out, _ = results(["setup|true|success", "pins|true|success"], summary="broken")
        self.assertEqual(out.returncode, 0, out.stdout)

    def test_the_lint_job_runs_the_same_script(self):
        self.assertEqual(results_step(LINT)["run"], RESULTS_RUN)

    def test_the_script_reads_no_expression_output_or_secret(self):
        self.assertNotIn("${{", RESULTS_RUN)
        for step in (results_step(FAST), results_step(LINT)):
            self.assertEqual(step["if"], "${{ always() }}")
            self.assertNotRegex(str(step["env"]), SECRET_CONTEXT)
            self.assertNotIn(".outputs.", str(step["env"]))


class FastJob(unittest.TestCase):
    """fast-checks.yml: one job, boolean inputs, each check a step that runs whatever failed before (R-S2i-1, R-S2i-2)."""

    def test_one_job_named_fast_with_boolean_check_inputs_off_by_default(self):
        doc = load("fast-checks.yml")
        self.assertEqual(list(doc["jobs"]), ["fast"])
        inputs = doc[True]["workflow_call"]["inputs"]
        for name, _, _ in CHECKS:
            self.assertEqual((inputs[name]["type"], inputs[name]["default"]), ("boolean", False), name)
        self.assertEqual(sorted(doc[True]["workflow_call"]["secrets"]), ["AGENTS_APP_CLIENT_ID", "AGENTS_APP_PRIVATE_KEY"])
        self.assertFalse(any(s["required"] for s in doc[True]["workflow_call"]["secrets"].values()))
        self.assertTrue(all(not v.get("required") for v in inputs.values()))  # expand: no caller has to change

    def test_no_concurrency_and_no_trigger_filter_in_the_called_workflow(self):
        # A called workflow's group would share the caller's name and cancel or block it; a paths: filter never reports.
        doc = load("fast-checks.yml")
        self.assertNotIn("concurrency", doc)
        self.assertNotIn("concurrency", doc["jobs"]["fast"])
        self.assertEqual(list(doc[True]), ["workflow_call"])

    def test_each_check_runs_after_an_earlier_failure_when_requested_and_set_up(self):
        for name, step_id, action in CHECKS:
            step = by_id(FAST, step_id)
            self.assertEqual(step["uses"], f"./_shared/actions/{action}", name)
            cond = step["if"]
            self.assertTrue(cond.startswith(f"${{{{ always() && inputs.{name} && steps.shared.outcome == 'success'"), cond)
            self.assertNotIn("failure()", cond)
            self.assertNotIn("success()", cond)

    def test_the_secret_scan_skips_itself_only_on_a_promotion_pull_request(self):
        self.assertEqual(by_id(FAST, "secrets")["if"],
                         f"${{{{ always() && inputs.secrets && steps.shared.outcome == 'success' && !{PROMOTION} }}}}")
        for name, step_id, _ in CHECKS:
            if name != "secrets":
                self.assertNotIn("base_ref", by_id(FAST, step_id)["if"], name)  # release-specific checks always run
        lines = results_step(FAST)["env"]["RESULTS"]
        self.assertIn(f"secrets|${{{{ inputs.secrets && ({PROMOTION} && 'excused' || 'true') || 'false' }}}}|", lines)

    def test_the_results_name_every_check_and_the_setup(self):
        lines = results_step(FAST)["env"]["RESULTS"].splitlines()
        self.assertEqual(lines[0], "setup|true|${{ steps.repo.outcome }},${{ steps.version.outcome }},${{ steps.shared.outcome }}")
        for name, step_id, _ in CHECKS:
            self.assertTrue(any(line.startswith(f"{name}|") and line.endswith(f"|${{{{ steps.{step_id}.outcome }}}}")
                                for line in lines), name)
        self.assertEqual(len(lines), 1 + len(CHECKS))

    def test_order_setup_checks_results_then_the_job_usage_step(self):
        ids = [s.get("id") for s in FAST]
        self.assertEqual(ids[:3], ["repo", "version", "shared"])
        self.assertEqual(ids[3:7], ["pins", "secrets", "no-prerelease-deps", "contract"])  # previews before peers
        self.assertTrue(FAST[7]["name"].startswith("Results"))
        self.assertTrue(FAST[8]["name"].startswith("Job usage"))
        self.assertEqual(len(FAST), 9)
        self.assertEqual(FAST[0]["with"]["fetch-depth"], 0)  # gitleaks scans the pull request's commits

    def test_the_contract_step_gets_its_inputs_and_the_app_secrets(self):
        self.assertEqual(by_id(FAST, "contract")["with"], {
            "role": "${{ inputs.role }}", "base_ref": "${{ inputs.base_ref }}",
            "app-client-id": "${{ secrets.AGENTS_APP_CLIENT_ID }}", "app-private-key": "${{ secrets.AGENTS_APP_PRIVATE_KEY }}"})
        self.assertEqual(by_id(FAST, "pins")["with"], {
            "app-client-id": "${{ secrets.AGENTS_APP_CLIENT_ID }}", "app-private-key": "${{ secrets.AGENTS_APP_PRIVATE_KEY }}"})


class SecretsStepCondition(unittest.TestCase):
    """R-S2i-6 (docs#293 r1): the secrets step runs after an earlier failed step, and skips only on a promotion."""

    def ctx(self, event="pull_request", base="dev", head="feature/x", pins="failure", shared="success"):
        return {"inputs.secrets": True, "steps.shared.outcome": shared, "steps.pins.outcome": pins,
                "github.event_name": event, "github.base_ref": base, "github.head_ref": head,
                "__failed__": pins == "failure"}

    def runs(self, **kw):
        return bool(evaluate(by_id(FAST, "secrets")["if"], self.ctx(**kw)))

    def want(self, **kw):
        line = next(l for l in results_step(FAST)["env"]["RESULTS"].splitlines() if l.startswith("secrets|"))
        return evaluate(line.rsplit("|", 1)[0].split("|", 1)[1], self.ctx(**kw), condition=False)

    def test_it_runs_after_an_earlier_failed_step(self):
        self.assertTrue(self.runs(pins="failure"))
        self.assertEqual(self.want(pins="failure"), "true")

    def test_a_promotion_pull_request_skips_it_and_the_job_still_reports_the_skip(self):
        self.assertFalse(self.runs(base="release", head="dev"))
        self.assertEqual(self.want(base="release", head="dev"), "excused")
        out, summary = results(["setup|true|success", "pins|true|success", f"secrets|{self.want(base='release', head='dev')}|skipped"])
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertIn("| secrets | skipped: a dev -> release promotion pull request", summary)

    def test_a_hotfix_into_release_and_a_push_still_run_it(self):
        self.assertTrue(self.runs(base="release", head="hotfix/x"))
        self.assertTrue(self.runs(event="push", base="", head=""))
        self.assertEqual(self.want(event="push", base="", head=""), "true")

    def test_not_requested_or_not_set_up_means_not_run(self):
        ctx = self.ctx()
        ctx["inputs.secrets"] = False
        self.assertFalse(evaluate(by_id(FAST, "secrets")["if"], ctx))
        self.assertEqual(evaluate(next(l for l in results_step(FAST)["env"]["RESULTS"].splitlines()
                                       if l.startswith("secrets|")).rsplit("|", 1)[0].split("|", 1)[1], ctx, False), "false")
        self.assertFalse(self.runs(shared="failure"))

    def test_every_other_check_runs_on_a_promotion(self):
        for name, step_id, _ in CHECKS:
            if name == "secrets":
                continue
            ctx = {**self.ctx(base="release", head="dev"), f"inputs.{name}": True}
            self.assertTrue(evaluate(by_id(FAST, step_id)["if"], ctx), name)


class Expand(unittest.TestCase):
    """The old workflows keep their job names, inputs and secrets and run the same composite actions (R-S2i-1)."""

    def test_old_names_inputs_and_secrets_are_unchanged(self):
        for file, (job, inputs, secrets, _) in OLD.items():
            call = load(file)[True]["workflow_call"]
            self.assertEqual(list(load(file)["jobs"]), [job], file)
            self.assertEqual(list(call["inputs"]), inputs, file)
            self.assertEqual(sorted(call.get("secrets") or {}), secrets, file)

    def test_each_old_workflow_runs_its_action_from_the_shared_checkout_at_its_pin(self):
        for file, (job, _, _, action) in OLD.items():
            st = steps(file, job)
            shared = by_id(st, "shared")
            self.assertEqual(shared["with"], {"repository": "OnePeek/.github", "ref": "${{ steps.version.outputs.sha }}",
                                              "path": "_shared", "persist-credentials": False}, file)
            uses = [s.get("uses") for s in st]
            self.assertEqual(uses.count(f"./_shared/actions/{action}"), 1, file)
            self.assertLess(st.index(shared), uses.index(f"./_shared/actions/{action}"), file)
            self.assertEqual(st[0]["uses"].split("@")[0], "actions/checkout", file)  # the caller first: the pin is read there

    def test_the_version_step_is_one_text_in_every_workflow(self):
        texts = {}
        for file in [*OLD, "fast-checks.yml"]:
            st = steps(file, OLD[file][0] if file in OLD else "fast")
            step = by_id(st, "version")
            self.assertEqual(step["env"]["FILE"], file)
            texts[file] = (step["run"], {k: v for k, v in step["env"].items() if k != "FILE"},
                           step["name"].replace(file, "<file>"))
        self.assertEqual(len(set(map(str, texts.values()))), 1, texts)

    def test_secret_scan_keeps_the_full_history(self):
        self.assertEqual(steps("secret-scan.yml", "gitleaks")[0]["with"]["fetch-depth"], 0)


class Version(unittest.TestCase):
    """The shared checkout is this workflow's own repository at the caller's pin; a caller without a pin fails."""

    PIN = "a" * 40

    def test_the_callers_pin_is_the_version(self):
        text = f"    uses: OnePeek/.github/.github/workflows/fast-checks.yml@{self.PIN} # v1.25.0\n"
        out, sha = version("fast-checks.yml", "OnePeek/backend-service", text)
        self.assertEqual((out.returncode, sha), (0, self.PIN), out.stdout + out.stderr)

    def test_the_pin_is_read_in_any_letter_case(self):
        text = f"    uses: onepeek/.GITHUB/.github/workflows/pins.yml@{self.PIN}\n"
        self.assertEqual(version("pins.yml", "OnePeek/onepeek-web", text)[1], self.PIN)

    def test_another_workflows_pin_is_not_this_ones(self):
        text = f"    uses: OnePeek/.github/.github/workflows/pins.yml@{self.PIN}\n"
        out, sha = version("secret-scan.yml", "OnePeek/onepeek-web", text)
        self.assertEqual(out.returncode, 1)
        self.assertIn("No pin of OnePeek/.github/.github/workflows/secret-scan.yml", out.stdout)
        self.assertIsNone(sha)

    def test_a_local_call_inside_this_repo_uses_the_runs_commit(self):
        out, sha = version("fast-checks.yml", "OnePeek/.github", "    uses: ./.github/workflows/fast-checks.yml\n")
        self.assertEqual((out.returncode, sha), (0, "e" * 40))

    def test_a_short_or_moving_ref_is_no_pin(self):
        out, _ = version("contract.yml", "OnePeek/onepeek-infra", "    uses: OnePeek/.github/.github/workflows/contract.yml@dev\n")
        self.assertEqual(out.returncode, 1)


class Actions(unittest.TestCase):
    """The composite actions: inputs only (a composite action cannot read secrets), bash steps, no inline expressions."""

    def test_every_action_is_composite_with_bash_steps_and_no_secret_context(self):
        names = sorted(p.parent.name for p in ACTIONS.glob("*/action.yml"))
        self.assertEqual(names, ["contract", "gitleaks", "no-prerelease-deps", "pins"])
        for path in ACTIONS.glob("*/action.yml"):
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertEqual(doc["runs"]["using"], "composite", path)
            self.assertNotRegex(path.read_text(encoding="utf-8"), SECRET_CONTEXT, path)
            for step in doc["runs"]["steps"]:
                if "run" in step:
                    self.assertEqual(step.get("shell"), "bash", (path, step.get("name")))
                    self.assertNotIn("${{", step["run"], (path, step.get("name")))  # values come in through env
                else:
                    self.assertRegex(step["uses"], r"@[0-9a-f]{40}$", (path, step.get("name")))

    def test_the_scripts_are_the_actions_own_repository(self):
        pins = yaml.safe_load((ACTIONS / "pins/action.yml").read_text())["runs"]["steps"]
        self.assertTrue(all("_shared" not in str(s) for s in pins))
        self.assertIn('"${GITHUB_ACTION_PATH}/../.."', pins[0]["run"])
        self.assertIn('--shared "${shared}"', pins[0]["run"])

    def test_the_pins_app_token_runs_only_when_the_docs_rule_needs_it(self):
        pins = yaml.safe_load((ACTIONS / "pins/action.yml").read_text())["runs"]["steps"]
        for step in pins[2:4]:
            self.assertEqual(step["if"], "${{ steps.docs.outputs.app == 'true' }}", step.get("name"))
        self.assertEqual(pins[-1]["env"]["GH_TOKEN"], "${{ steps.app.outputs.token }}")


class Docs(unittest.TestCase):
    """The docs-needed step of the pins action: the App is needed only when the rule runs and docs-version.txt exists."""

    def run_docs(self, needed, with_file):
        step = yaml.safe_load((ACTIONS / "pins/action.yml").read_text())["runs"]["steps"][1]
        with tempfile.TemporaryDirectory() as d:
            action = Path(d, "act/actions/pins")
            action.mkdir(parents=True)
            (Path(d, "act/scripts")).mkdir()
            Path(d, "act/scripts/check_pins.py").write_text(f"print('{needed}')\n")
            work = Path(d, "work")
            work.mkdir()
            if with_file:
                (work / "docs-version.txt").write_text("x\n")
            out_file = Path(d, "out")
            env = {"PATH": os.environ["PATH"], "GITHUB_ACTION_PATH": str(action), "GITHUB_OUTPUT": str(out_file)}
            out = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=work, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(out.returncode, 0, out.stderr)
            return dict(line.split("=", 1) for line in out_file.read_text().split())

    def test_app_only_when_needed_and_the_file_exists(self):
        self.assertEqual(self.run_docs("true", True)["app"], "true")
        self.assertEqual(self.run_docs("true", False)["app"], "false")
        self.assertEqual(self.run_docs("false", True)["app"], "false")


class WorkflowLint(unittest.TestCase):
    """This repo's own check: depends-on, the merged `lint` job, and `fast` (the shared merged job, called locally)."""

    def test_concurrency_cancels_only_pull_request_runs(self):  # R-S2i-3
        self.assertEqual(load("workflow-lint.yml")["concurrency"], {
            "group": "${{ github.workflow }}-${{ github.ref }}",
            "cancel-in-progress": "${{ github.event_name == 'pull_request' }}"})

    def test_three_jobs_and_the_tiny_ones_are_steps_of_lint(self):
        jobs = load("workflow-lint.yml")["jobs"]
        self.assertEqual(list(jobs), ["depends-on", "lint", "fast"])
        self.assertEqual(jobs["fast"]["uses"], "./.github/workflows/fast-checks.yml")
        self.assertEqual(jobs["fast"]["with"], {"no-prerelease-deps": True})
        uses = [s.get("uses") for s in LINT]
        self.assertIn("./actions/gitleaks", uses)
        self.assertIn("./actions/pins", uses)

    def test_every_lint_step_after_the_checkout_runs_after_an_earlier_failure(self):
        for step in LINT[1:]:
            cond = step.get("if", "")
            if step.get("name", "").startswith("Results"):
                continue
            self.assertTrue(cond.startswith("${{ always()"), step.get("name"))
            self.assertNotIn("success()", cond, step.get("name"))

    def test_the_results_cover_every_lint_step(self):
        lines = results_step(LINT)["env"]["RESULTS"]
        ids = [s["id"] for s in LINT if s.get("id")]
        for step_id in ids:
            self.assertIn(f"${{{{ steps.{step_id}.outcome }}}}", lines, step_id)
        for name in ("actionlint", "secrets", "zizmor", "runner-labels", "pins", "service-list"):
            self.assertTrue(re.search(rf"^{re.escape(name)}\|true\|", lines, re.M), name)

    def test_zizmor_reads_the_composite_actions_too(self):
        self.assertIn("zizmor --no-progress --min-severity medium .github/workflows actions", by_id(LINT, "zizmor")["run"])


if __name__ == "__main__":
    unittest.main()
