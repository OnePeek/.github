"""One fixture per workflow-lint rule of onepeek-docs architecture/lld/infra/ci-runners.md section 5.4."""
import tempfile
import textwrap
import unittest
from pathlib import Path

import check_runner_labels as lint

FAST_PUBLIC = "${{ inputs.runner == 'onepeek' && 'onepeek-arm64' || inputs.runner == 'canary' && 'onepeek-arm64-canary' || 'ubuntu-latest' }}"
ARM_PUBLIC = "${{ inputs.runner == 'onepeek' && 'onepeek-arm64' || inputs.runner == 'canary' && 'onepeek-arm64-canary' || 'ubuntu-24.04-arm' }}"
STATEFUL_PUBLIC = "${{ inputs.runner == 'onepeek' && 'onepeek-arm64-od' || inputs.runner == 'canary' && 'onepeek-arm64-od' || 'ubuntu-latest' }}"
FAST_PRIVATE = FAST_PUBLIC.replace("inputs.runner", "vars.RUNNER")
STATEFUL_PRIVATE = STATEFUL_PUBLIC.replace("inputs.runner", "vars.RUNNER")
PROD_GUARD = "${{ inputs.environment == 'prod' && 'ubuntu-latest' || " + FAST_PUBLIC[4:]


def run(kind, repo, name, text, listed=()):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / name
        p.write_text(textwrap.dedent(text))
        return lint.check_file(p, kind, repo, set(listed))


def reusable(runs_on, timeout="5", extra=""):
    return f"""\
        on:
          workflow_call:
            inputs:
              runner: {{type: string, default: github}}
        jobs:
          j:
            runs-on: {runs_on}
            timeout-minutes: {timeout}
            steps: [{{run: echo}}]
        {extra}
        """


def own(runs_on, timeout="5", trigger="pull_request"):
    return f"""\
        on: {trigger}
        jobs:
          j:
            runs-on: {runs_on}
            timeout-minutes: {timeout}
            steps: [{{run: echo}}]
        """


class Rule1Public(unittest.TestCase):
    def test_reusable_mapping_is_allowed(self):
        for m in (FAST_PUBLIC, ARM_PUBLIC, STATEFUL_PUBLIC, PROD_GUARD):
            self.assertEqual(run("public", "OnePeek/.github", "a.yml", reusable(m)), [], m)

    def test_non_reusable_workflow_naming_onepeek_label_is_refused(self):
        errs = run("public", "OnePeek/plugin-owl", "ci.yml", own("onepeek-arm64"))
        self.assertTrue(any("rule 1" in e for e in errs), errs)

    def test_non_reusable_workflow_with_the_mapping_is_refused(self):
        errs = run("public", "OnePeek/.github", "ci.yml", own(FAST_PUBLIC))
        self.assertTrue(any("rule 1" in e for e in errs), errs)

    def test_self_hosted_is_refused_in_a_non_reusable_workflow(self):
        errs = run("public", "OnePeek/plugin-owl", "ci.yml", own("self-hosted"))
        self.assertTrue(any("rule 1" in e for e in errs), errs)

    def test_reusable_with_a_label_outside_the_exact_mapping_is_refused(self):
        errs = run("public", "OnePeek/.github", "a.yml", reusable("onepeek-arm64"))
        self.assertTrue(any("rule 1" in e for e in errs), errs)
        errs = run("public", "OnePeek/.github", "a.yml", reusable("${{ inputs.runner || 'onepeek-arm64-od' }}"))
        self.assertTrue(any("rule 1" in e for e in errs), errs)

    def test_reusable_naming_a_label_outside_runs_on_is_refused(self):
        errs = run("public", "OnePeek/.github", "a.yml", reusable(FAST_PUBLIC, extra="# x"), [])
        self.assertEqual(errs, [])
        text = reusable(FAST_PUBLIC).replace("steps: [{run: echo}]", "steps: [{run: echo onepeek-arm64}]")
        self.assertTrue(any("rule 1" in e for e in run("public", "OnePeek/.github", "a.yml", text)))

    def test_public_workflow_reading_vars_runner_is_refused(self):
        errs = run("public", "OnePeek/.github", "a.yml", reusable(FAST_PRIVATE))
        self.assertTrue(any("rule 1" in e for e in errs), errs)
        text = own("ubuntu-latest").replace("steps: [{run: echo}]", "steps: [{run: echo, if: vars.RUNNER == 'x'}]")
        self.assertTrue(any("vars.RUNNER" in e for e in run("public", "OnePeek/.github", "a.yml", text)))


class Rule2Private(unittest.TestCase):
    def test_forms_of_section_5_3_are_allowed(self):
        for m in (FAST_PRIVATE, STATEFUL_PRIVATE, "ubuntu-latest"):
            self.assertEqual(run("private", "OnePeek/backend-service", "c.yml", own(m)), [], m)

    def test_other_labels_are_refused(self):
        for m in ("self-hosted", "ghr-arm64", "onepeek-arm64", "ubuntu-24.04", "${{ matrix.os }}", "[self-hosted, linux]"):
            errs = run("private", "OnePeek/backend-service", "c.yml", own(m))
            self.assertTrue(any("rule 2" in e for e in errs), (m, errs))

    def test_listed_job_must_be_github_only(self):
        listed = [("OnePeek/onepeek-infra", "promote.yml", "j")]
        self.assertEqual(run("private", "OnePeek/onepeek-infra", "promote.yml", own("ubuntu-latest"), listed), [])
        errs = run("private", "OnePeek/onepeek-infra", "promote.yml", own(FAST_PRIVATE), listed)
        self.assertTrue(any("github-only-jobs" in e for e in errs), errs)

    def test_listed_job_in_another_repo_is_not_affected(self):
        listed = [("OnePeek/onepeek-infra", "promote.yml", "j")]
        self.assertEqual(run("private", "OnePeek/backend-service", "promote.yml", own(FAST_PRIVATE), listed), [])

    def test_canary_and_image_workflows_of_infra_are_exempt_by_name(self):
        for name in ("ci-runner-canary.yml", "ci-runner-image.yml"):
            for label in ("onepeek-arm64", "onepeek-arm64-canary"):
                self.assertEqual(run("private", "OnePeek/onepeek-infra", name, own(label)), [], (name, label))

    def test_exemption_is_by_repo_and_file_name_only(self):
        for repo, name in (("OnePeek/backend-service", "ci-runner-canary.yml"), ("OnePeek/onepeek-infra", "checks.yml")):
            errs = run("private", repo, name, own("onepeek-arm64"))
            self.assertTrue(any("rule 2" in e for e in errs), (repo, name, errs))

    def test_the_shipped_list_parses(self):
        path = Path(__file__).resolve().parents[1] / ".github" / "workflow-lint" / "github-only-jobs.yaml"
        self.assertIsInstance(lint.load_listed(path), set)


class Rule3Timeouts(unittest.TestCase):
    def test_missing_or_large_timeout_is_refused(self):
        for m, limit in ((FAST_PRIVATE, 60), (STATEFUL_PRIVATE, 150)):
            self.assertEqual(run("private", "OnePeek/x", "c.yml", own(m, str(limit))), [])
            errs = run("private", "OnePeek/x", "c.yml", own(m, str(limit + 1)))
            self.assertTrue(any("rule 3" in e for e in errs), errs)
        errs = run("private", "OnePeek/x", "c.yml", own(FAST_PRIVATE).replace("            timeout-minutes: 5\n", ""))
        self.assertTrue(any("rule 3" in e for e in errs), errs)

    def test_stateful_allows_more_than_fast(self):
        self.assertEqual(run("private", "OnePeek/x", "c.yml", own(STATEFUL_PRIVATE, "120")), [])
        self.assertTrue(run("private", "OnePeek/x", "c.yml", own(FAST_PRIVATE, "120")))

    def test_public_reusable_job_needs_a_timeout(self):
        errs = run("public", "OnePeek/.github", "a.yml", reusable(FAST_PUBLIC, timeout="61"))
        self.assertTrue(any("rule 3" in e for e in errs), errs)

    def test_github_only_job_needs_no_limit(self):
        self.assertEqual(run("private", "OnePeek/x", "c.yml", own("ubuntu-latest", "300")), [])


class Rule4Triggers(unittest.TestCase):
    def test_pull_request_target_is_refused_everywhere(self):
        for kind, repo in (("public", "OnePeek/.github"), ("private", "OnePeek/x")):
            errs = run(kind, repo, "c.yml", own("ubuntu-latest", trigger="pull_request_target"))
            self.assertTrue(any("rule 4" in e for e in errs), errs)

    def test_workflow_run_job_on_onepeek_is_refused(self):
        errs = run("private", "OnePeek/x", "c.yml", own(FAST_PRIVATE, trigger="workflow_run"))
        self.assertTrue(any("rule 4" in e for e in errs), errs)

    def test_workflow_run_on_hosted_runner_is_allowed(self):
        self.assertEqual(run("private", "OnePeek/x", "c.yml", own("ubuntu-latest", trigger="workflow_run")), [])


class SharedWorkflows(unittest.TestCase):
    def test_this_repos_workflows_pass_the_public_rules(self):
        root = Path(__file__).resolve().parents[1]
        rc = lint.main(["--kind", "public", "--repo", "OnePeek/.github", "--list",
                        str(root / ".github" / "workflow-lint" / "github-only-jobs.yaml"),
                        str(root / ".github" / "workflows")])
        self.assertEqual(rc, 0)

    def test_every_reusable_workflow_has_the_runner_input_defaulting_to_github(self):
        import yaml
        for p in sorted((Path(__file__).resolve().parents[1] / ".github" / "workflows").glob("*.yml")):
            doc = yaml.safe_load(p.read_text())
            call = (doc.get("on") or doc.get(True) or {}).get("workflow_call")
            if call is None:
                continue
            self.assertEqual(call["inputs"]["runner"]["default"], "github", p.name)
            self.assertEqual(call["inputs"]["runner"]["type"], "string", p.name)

    def test_prod_environment_never_reaches_a_self_hosted_label(self):
        import yaml
        for name in ("release-ecs.yml", "deploy-ecs.yml", "operate.yml"):
            doc = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github" / "workflows" / name).read_text())
            for job_id, job in doc["jobs"].items():
                ro = job.get("runs-on")
                if ro and "onepeek-" in ro and "version" != job_id:
                    self.assertIn("inputs.environment == 'prod' && 'ubuntu-latest' ||", ro, (name, job_id))


if __name__ == "__main__":
    unittest.main()
