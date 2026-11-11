"""One fixture per workflow-lint rule of onepeek-docs architecture/lld/infra/ci-runners.md section 5.4."""
import tempfile
import textwrap
import unittest
from pathlib import Path

import check_runner_labels as lint

FAST_PUBLIC = "${{ inputs.runner == 'onepeek' && 'onepeek-arm64' || inputs.runner == 'canary' && 'onepeek-arm64-canary' || 'ubuntu-latest' }}"
ARM_PUBLIC = "${{ inputs.runner == 'onepeek' && 'onepeek-arm64' || inputs.runner == 'canary' && 'onepeek-arm64-canary' || 'ubuntu-latest' }}"
LIGHT_PUBLIC = "${{ inputs.runner == 'onepeek' && 'onepeek-arm64-small' || inputs.runner == 'canary' && 'onepeek-arm64-canary' || 'ubuntu-latest' }}"
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


class ExemptMatrix(unittest.TestCase):
    def test_an_exempt_workflow_may_name_its_labels_through_a_matrix(self):
        text = ("on: [workflow_dispatch]\njobs:\n  smoke:\n    strategy:\n      matrix:\n        runner: [onepeek-arm64-canary, onepeek-arm64]\n"
                "    runs-on: ${{ matrix.runner }}\n    timeout-minutes: 20\n    steps: [{run: echo}]\n")
        self.assertEqual(run("private", "OnePeek/onepeek-infra", "ci-runner-canary.yml", text), [])
        self.assertNotEqual(run("private", "OnePeek/onepeek-web", "ci-runner-canary.yml", text), [])
        bad = text.replace("onepeek-arm64]", "onepeek-arm64-od]")
        self.assertNotEqual(run("private", "OnePeek/onepeek-infra", "ci-runner-canary.yml", bad), [])


class Rule1Public(unittest.TestCase):
    def test_reusable_mapping_is_allowed(self):
        for m in (LIGHT_PUBLIC, FAST_PUBLIC, ARM_PUBLIC, STATEFUL_PUBLIC, PROD_GUARD):
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


class Rule1PublicPassthrough(unittest.TestCase):
    def test_free_passthrough_in_a_public_reusable_workflow_is_refused(self):
        for ro in ("${{ inputs.label }}", "${{ inputs.runner }}", "ubuntu-24.04", "[ubuntu-latest]"):
            errs = run("public", "OnePeek/.github", "a.yml", reusable(ro))
            self.assertTrue(any("rule 1" in e for e in errs), (ro, errs))

    def test_plain_hosted_label_is_allowed(self):
        self.assertEqual(run("public", "OnePeek/.github", "a.yml", reusable("ubuntu-latest")), [])

    def test_mapping_is_allowed_only_in_the_dot_github_repo(self):
        errs = run("public", "OnePeek/plugin-owl", "a.yml", reusable(FAST_PUBLIC))
        self.assertTrue(any("rule 1" in e for e in errs), errs)
        self.assertEqual(run("public", "OnePeek/plugin-owl", "a.yml", reusable("ubuntu-latest")), [])

    def test_a_public_repo_passes_no_runner_but_github(self):
        call = """\
            on: pull_request
            jobs:
              j:
                uses: OnePeek/.github/.github/workflows/secret-scan.yml@abc
                with:
                  runner: %s
            """
        for given, ok in (("github", True), ("onepeek", False), ("canary", False), ("${{ vars.RUNNER }}", False)):
            errs = run("public", "OnePeek/plugin-owl", "c.yml", call % given)
            self.assertEqual(errs == [], ok, (given, errs))


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


RELEASE_GUARD = "github.base_ref == 'release' && (vars.RUNNER == 'onepeek' || vars.RUNNER == 'canary') && 'onepeek-arm64-od' || "
LIGHT_PRIVATE = LIGHT_PUBLIC.replace("inputs.runner", "vars.RUNNER")
ARM_PRIVATE = ("${{ vars.RUNNER == 'onepeek' && 'onepeek-arm64' || vars.RUNNER == 'canary' && 'onepeek-arm64-canary' "
               "|| 'ubuntu-24.04-arm' }}")


def release(form):
    """A private standard form with the release guard in front (ci-runners.md section 5.3; sandbox.md section 7f item 5)."""
    return "${{ " + RELEASE_GUARD + form[4:]


class Rule2ReleaseForm(unittest.TestCase):
    """sandbox.md section 7f items 5 and 8 (R-E0-6): a pull_request workflow may run every job on onepeek-arm64-od when
    the base is release, in exactly this form, in front of a standard form."""

    def test_the_release_form_in_front_of_each_standard_form_is_allowed(self):
        for form in (LIGHT_PRIVATE, FAST_PRIVATE, ARM_PRIVATE, STATEFUL_PRIVATE):
            self.assertEqual(run("private", "OnePeek/onepeek-infra", "sandbox.yml", own(release(form))), [], form)

    def test_the_exact_text_of_infra_241_is_allowed(self):
        # onepeek-infra#241 head ab5220f, .github/workflows/sandbox.yml line 61.
        text = ("${{ github.base_ref == 'release' && (vars.RUNNER == 'onepeek' || vars.RUNNER == 'canary') && 'onepeek-arm64-od' "
                "|| vars.RUNNER == 'onepeek' && 'onepeek-arm64-small' || vars.RUNNER == 'canary' && 'onepeek-arm64-canary' "
                "|| 'ubuntu-latest' }}")
        self.assertEqual(run("private", "OnePeek/onepeek-infra", "sandbox.yml", own(text)), [])

    def test_the_release_form_keeps_the_class_limit_of_its_standard_form(self):
        self.assertEqual(run("private", "OnePeek/x", "c.yml", own(release(LIGHT_PRIVATE), "30")), [])
        errs = run("private", "OnePeek/x", "c.yml", own(release(LIGHT_PRIVATE), "31"))
        self.assertTrue(any("rule 3" in e for e in errs), errs)

    def test_a_trigger_list_or_mapping_with_pull_request_is_allowed(self):
        for trigger in ("[pull_request, workflow_dispatch]", "{pull_request: {branches: [dev, release]}, workflow_dispatch: {}}"):
            self.assertEqual(run("private", "OnePeek/x", "c.yml", own(release(FAST_PRIVATE), trigger=trigger)), [], trigger)

    def test_hostile_forms_are_refused(self):
        bad = {
            "another label": release(FAST_PRIVATE).replace("'onepeek-arm64-od' ||", "'onepeek-arm64' ||", 1),
            "the canary label": release(FAST_PRIVATE).replace("'onepeek-arm64-od' ||", "'onepeek-arm64-canary' ||", 1),
            "a base other than release": release(FAST_PRIVATE).replace("'release'", "'dev'", 1),
            "a head ref": release(FAST_PRIVATE).replace("github.base_ref", "github.head_ref", 1),
            "no RUNNER guard": release(FAST_PRIVATE).replace("(vars.RUNNER == 'onepeek' || vars.RUNNER == 'canary') && ", ""),
            "only the onepeek guard": release(FAST_PRIVATE).replace(" || vars.RUNNER == 'canary')", ")", 1),
            "no standard tail": "${{ " + RELEASE_GUARD + "'ubuntu-latest' }}",
            "no tail at all": "${{ " + RELEASE_GUARD.rstrip(" |") + " }}",
            "a tail that is not a standard form": "${{ " + RELEASE_GUARD + "vars.RUNNER == 'onepeek' && 'self-hosted' || 'ubuntu-latest' }}",
            "the guard twice": "${{ " + RELEASE_GUARD + release(FAST_PRIVATE)[4:],
            "the guard after the tail": FAST_PRIVATE[:-3] + " || " + RELEASE_GUARD.rstrip(" |") + " }}",
        }
        for why, ro in bad.items():
            errs = run("private", "OnePeek/onepeek-infra", "sandbox.yml", own(ro))
            self.assertTrue(any("rule 2" in e for e in errs), (why, errs))

    def test_a_workflow_without_a_pull_request_trigger_is_refused(self):
        for trigger in ("push", "workflow_dispatch", "[push, workflow_dispatch]", "{workflow_call: {}}", "schedule"):
            errs = run("private", "OnePeek/onepeek-infra", "sandbox.yml", own(release(FAST_PRIVATE), trigger=trigger))
            self.assertTrue(any("rule 2" in e for e in errs), (trigger, errs))

    def test_a_public_repo_may_not_use_the_release_form(self):
        public = "${{ github.base_ref == 'release' && (inputs.runner == 'onepeek' || inputs.runner == 'canary') && 'onepeek-arm64-od' || " + FAST_PUBLIC[4:]
        for ro in (public, release(FAST_PRIVATE)):
            self.assertNotEqual(run("public", "OnePeek/.github", "a.yml", reusable(ro)), [], ro)
            self.assertNotEqual(run("public", "OnePeek/.github", "a.yml", own(ro)), [], ro)


class Rule3Timeouts(unittest.TestCase):
    def test_missing_or_large_timeout_is_refused(self):
        for m, limit in ((FAST_PRIVATE, 60), (STATEFUL_PRIVATE, 150)):
            self.assertEqual(run("private", "OnePeek/x", "c.yml", own(m, str(limit))), [])
            errs = run("private", "OnePeek/x", "c.yml", own(m, str(limit + 1)))
            self.assertTrue(any("rule 3" in e for e in errs), errs)
        errs = run("private", "OnePeek/x", "c.yml", own(FAST_PRIVATE).replace("            timeout-minutes: 5\n", ""))
        self.assertTrue(any("rule 3" in e for e in errs), errs)

    def test_light_limit_is_30(self):
        light = LIGHT_PUBLIC.replace("inputs.runner", "vars.RUNNER")
        self.assertEqual(run("private", "OnePeek/x", "c.yml", own(light, "30")), [])
        self.assertTrue(any("rule 3" in e for e in run("private", "OnePeek/x", "c.yml", own(light, "31"))))

    def test_sandbox_lock_is_the_named_exception(self):
        light = LIGHT_PUBLIC.replace("inputs.runner", "vars.RUNNER")
        lock = own(light, "85").replace("  j:", "  lock:")
        self.assertEqual(run("private", "OnePeek/onepeek-infra", "sandbox.yml", lock), [])
        self.assertTrue(run("private", "OnePeek/onepeek-infra", "sandbox.yml", own(light, "86").replace("  j:", "  lock:")))
        # Only that job of that workflow in that repo.
        self.assertTrue(run("private", "OnePeek/onepeek-infra", "sandbox.yml", own(light, "85")))
        self.assertTrue(run("private", "OnePeek/onepeek-infra", "other.yml", lock))
        self.assertTrue(run("private", "OnePeek/backend-service", "sandbox.yml", lock))

    def test_the_release_lock_waits_are_named_exceptions(self):
        # foundation-playbook §5.4a "Waits that fit the runners": each waiting job of OnePeek/.github, by name.
        for name, job, limit in (("release-ecs.yml", "prepare", 135), ("release-ecs.yml", "finish", 60), ("operate.yml", "lock", 135),
                                 ("operate.yml", "unlock", 60), ("release-lock.yml", "lock", 135)):
            self.assertEqual(lint.TIMEOUT_EXCEPTIONS[("OnePeek/.github", name, job)], limit)
            text = reusable(STATEFUL_PUBLIC, timeout=str(limit)).replace("  j:", f"  {job}:")
            self.assertEqual(run("public", "OnePeek/.github", name, text), [], (name, job))
            over = reusable(STATEFUL_PUBLIC, timeout=str(limit + 1)).replace("  j:", f"  {job}:")
            self.assertTrue(any("rule 3" in e for e in run("public", "OnePeek/.github", name, over)), (name, job))
        # Any other job of these workflows keeps the class cap (150 for stateful).
        self.assertTrue(run("public", "OnePeek/.github", "release-ecs.yml", reusable(STATEFUL_PUBLIC, timeout="151").replace("  j:", "  verify:")))

    def test_an_expression_limit_is_read_as_its_larger_value(self):
        expr = "${{ (inputs.action == 'take' || inputs.action == 'freeze') && 135 || 10 }}"
        text = reusable(STATEFUL_PUBLIC, timeout=f'"{expr}"').replace("  j:", "  lock:")
        self.assertEqual(run("public", "OnePeek/.github", "release-lock.yml", text), [])
        self.assertTrue(run("public", "OnePeek/.github", "release-lock.yml", text.replace("135", "136")))
        self.assertTrue(run("public", "OnePeek/.github", "release-lock.yml", reusable(STATEFUL_PUBLIC, timeout='"${{ inputs.minutes }}"')))
        self.assertEqual(lint.timeout_minutes(expr), 135)
        self.assertIsNone(lint.timeout_minutes(True))

    def test_an_expression_that_could_hide_a_larger_limit_is_refused(self):
        # Only inputs.* comparisons, &&, ||, parentheses and bare integers: any other context, function or quoted value
        # could give a number the lint never sees (ci-runners.md §5.4 rule 3).
        for expr in ("${{ vars.T || 10 }}", "${{ inputs.a && '500' || 10 }}", "${{ inputs.a && fromJSON('500') || 10 }}",
                     "${{ format('{0}0', 50) }}", "${{ inputs.minutes || 10 }}", "${{ github.event.inputs.t == 'x' && 20 || 10 }}",
                     "${{ (inputs.a == 'x' && 20 || 10 }}", "${{ 10 20 }}", "${{ 1.5e3 }}", "${{ inputs.a == 'x' }}"):
            self.assertIsNone(lint.timeout_minutes(expr), expr)
            text = reusable(STATEFUL_PUBLIC, timeout=f'"{expr}"').replace("  j:", "  lock:")
            self.assertTrue(any("rule 3" in e for e in run("public", "OnePeek/.github", "release-lock.yml", text)), expr)

    def test_an_integer_inside_a_comparison_is_not_a_value_the_expression_gives(self):
        self.assertEqual(lint.timeout_minutes("${{ inputs.n == 500 && 20 || 10 }}"), 20)
        self.assertEqual(lint.timeout_minutes("${{ inputs.action != 'take' && 10 || (inputs.x == 'y' && 30 || 135) }}"), 135)
        self.assertEqual(lint.timeout_minutes("${{ 45 }}"), 45)

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


def caller(trigger, environment="preprod", flag=""):
    return f"""\
        on: {trigger}
        permissions: {{contents: read}}
        jobs:
          release:
            uses: OnePeek/.github/.github/workflows/release-ecs.yml@{"a" * 40}
            with:
              environment: {environment}
              {flag}
        """


class Rule5DeployFromDev(unittest.TestCase):
    """foundation-playbook §5.4: until launch `release` is preprod; deploying preprod from dev is off."""

    def rule5(self, text, kind="private"):
        return [e for e in run(kind, "OnePeek/backend-service", "release.yml", text) if "rule 5" in e]

    def test_a_push_to_dev_deploying_preprod_without_the_flag_is_refused(self):
        for trigger in ("{push: {branches: [dev]}}", "push", "{push: {branches: ['**']}}"):
            self.assertEqual(len(self.rule5(caller(trigger))), 1, trigger)

    def test_the_flag_false_is_still_refused(self):
        self.assertEqual(len(self.rule5(caller("{push: {branches: [dev]}}", flag="deploy-from-dev: false"))), 1)

    def test_the_flag_true_is_allowed(self):
        self.assertEqual(self.rule5(caller("{push: {branches: [dev]}}", flag="deploy-from-dev: true")), [])

    def test_a_push_to_release_is_allowed(self):
        self.assertEqual(self.rule5(caller("{push: {branches: [release]}}")), [])

    def test_another_environment_or_a_public_repo_is_not_this_rule(self):
        self.assertEqual(self.rule5(caller("{push: {branches: [dev]}}", environment="sandbox")), [])
        self.assertEqual(self.rule5(caller("{push: {branches: [dev]}}"), kind="public"), [])


class Rule6ImageDigests(unittest.TestCase):
    DIGEST = "public.ecr.aws/docker/library/postgres@sha256:" + "a" * 64

    def job(self, body):
        return f"on: [pull_request]\njobs:\n  t:\n    runs-on: ubuntu-latest\n{body}    steps: [{{run: echo}}]\n"

    def errs(self, body, kind="private"):
        return [e for e in run(kind, "OnePeek/x", "a.yml", self.job(body)) if "rule 6" in e]

    def test_a_service_image_by_digest_passes(self):
        self.assertEqual(self.errs(f"    services:\n      pg:\n        image: {self.DIGEST} # 17\n"), [])
        self.assertEqual(self.errs(f"    services:\n      pg:\n        image: \"{self.DIGEST}\" # postgres:17\n"), [])

    def test_a_service_image_by_tag_fails(self):
        self.assertTrue(self.errs("    services:\n      pg:\n        image: public.ecr.aws/docker/library/postgres:17\n"))

    def test_one_unpinned_service_among_pinned_ones_fails(self):
        body = f"    services:\n      pg:\n        image: {self.DIGEST} # 17\n      mongo:\n        image: mongo:7\n"
        self.assertEqual(len(self.errs(body)), 1)

    def test_a_digest_without_the_tag_comment_fails(self):
        # onepeek-infra's ci-runner-canary.yml form: `name:17@sha256:...` and no comment; the bump cannot move it.
        for line in (f"image: {self.DIGEST}", f"image: {self.DIGEST.replace('@', ':17@')}", f"image: {self.DIGEST} #"):
            errs = self.errs(f"    services:\n      pg:\n        {line}\n")
            self.assertEqual(len(errs), 1, line)
            self.assertIn("tag in a comment", errs[0])

    def test_a_digest_in_a_flow_mapping_fails(self):
        self.assertTrue(self.errs(f"    container: {{image: {self.DIGEST}}} # 17\n"))

    def test_a_tag_in_the_name_with_the_comment_passes(self):
        self.assertEqual(self.errs(f"    services:\n      pg:\n        image: {self.DIGEST.replace('@', ':17@')} # 17\n"), [])

    def test_an_image_from_the_caller_passes(self):
        self.assertEqual(self.errs("    container: ${{ inputs.image }}\n"), [])
        self.assertEqual(self.errs("    container:\n      image: ${{ matrix.c }}\n"), [])
        self.assertEqual(self.errs("    services:\n      db:\n        image: ${{ inputs.db-image }}\n"), [])

    def test_other_expressions_still_fail(self):
        self.assertTrue(self.errs("    container: ${{ vars.IMAGE }}\n"))
        self.assertTrue(self.errs("    container: node:${{ inputs.v }}\n"))

    def test_the_lint_and_the_bump_share_one_line_form(self):
        import bump_ci_images as bump
        self.assertEqual(lint.PIN_LINE.pattern, bump.LINE.pattern)

    def test_a_container_mapping_or_string_by_tag_fails(self):
        self.assertTrue(self.errs("    container:\n      image: node:22\n"))
        self.assertTrue(self.errs("    container: node:22\n"))

    def test_a_container_by_digest_passes(self):
        self.assertEqual(self.errs(f"    container: {self.DIGEST} # 17\n"), [])
        self.assertEqual(self.errs(f"    container:\n      image: {self.DIGEST} # 17\n"), [])

    def test_a_badly_written_line_fails_even_when_the_same_pin_is_written_well_elsewhere(self):
        good = f"        image: {self.DIGEST} # 17\n"
        bad = f"        image: {self.DIGEST}\n"
        body = f"    services:\n      a:\n{good}      b:\n{bad}"
        self.assertTrue(self.errs(body))
        self.assertTrue(self.errs(f"    services:\n      b:\n{bad}      a:\n{good}"))
        self.assertEqual(self.errs(f"    services:\n      a:\n{good}      b:\n{good}"), [])

    def test_a_short_or_malformed_digest_fails(self):
        self.assertTrue(self.errs("    container: node@sha256:abc\n"))
        self.assertTrue(self.errs("    container: node@sha256:" + "a" * 63 + "\n"))

    def test_it_applies_in_public_repos_too(self):
        self.assertTrue(self.errs("    container: node:22\n", kind="public"))

    def test_a_job_with_no_container_is_untouched(self):
        self.assertEqual(self.errs(""), [])


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
                if ro and "onepeek-" in ro:
                    self.assertIn("inputs.environment == 'prod' && 'ubuntu-latest' ||", ro, (name, job_id))


if __name__ == "__main__":
    unittest.main()
