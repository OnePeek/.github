"""Tests for check_release_skips.py (delivery-pipelines.md §8.1 "Slice 2 rulings" S1 to S7)."""
from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import pathlib
import shutil
import tempfile
import unittest

import yaml

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("check_release_skips", HERE / "check_release_skips.py")
crs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(crs)

SKIP = "!(github.event_name == 'pull_request' && github.base_ref == 'release' && github.head_ref == 'dev')"
CERT_IF = ("${{ always() && github.ref == 'refs/heads/dev' && "
           "(github.event_name == 'push' || github.event_name == 'workflow_dispatch') }}")
TREE_ONLY = ["secrets", "python", "architecture", "api-client"]


def backend_checks() -> dict:
    """A backend checks.yml that follows every rule."""
    jobs = {
        "depends-on": {"uses": "OnePeek/.github/.github/workflows/depends-on.yml@x"},
        "pins": {"needs": "depends-on", "uses": "x"},
        "release-gate": {"needs": "depends-on", "if": "github.event_name == 'pull_request' && github.base_ref == 'release'",
                         "uses": "x"},
        "contract": {"needs": "depends-on", "if": "github.event_name == 'pull_request'", "uses": "x"},
        "release-checks": {"needs": "depends-on", "runs-on": "ubuntu-latest", "steps": [{"run": "true"}]},
        "secrets": {"needs": "depends-on", "if": "${{ " + SKIP + " }}", "uses": "x"},
        "python": {"needs": "depends-on", "if": "${{ github.actor != 'x' && " + SKIP + " }}", "uses": "x"},
        "architecture": {"needs": "depends-on", "if": "${{ (a || b) &&   " + SKIP.replace(" && ", "  &&  ") + " }}",
                         "uses": "x"},
        "api-client": {"needs": "depends-on", "if": SKIP + " && github.event_name != 'schedule'", "uses": "x"},
        "certified": {"needs": list(TREE_ONLY), "if": CERT_IF, "runs-on": "ubuntu-latest", "steps": [{"run": "true"}]},
    }
    return {"name": "checks", "on": {"push": None}, "jobs": jobs}


def release_please(token: str | None = "${{ steps.app.outputs.token }}", mint_first: bool = True,
                   mint_uses: str = "actions/create-github-app-token@abc # v3") -> dict:
    mint = {"id": "app", "uses": mint_uses, "with": {"client-id": "x"}}
    rp = {"uses": "googleapis/release-please-action@45996ed # v5.0.0", "with": {"target-branch": "dev"}}
    if token is not None:
        rp["with"]["token"] = token
    steps = [mint, rp] if mint_first else [rp, mint]
    return {"name": "release-please", "jobs": {"release-please": {"runs-on": "ubuntu-latest", "steps": steps}}}


class Repo(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / ".github" / "workflows").mkdir(parents=True)
        self.checks = backend_checks()
        self.rp = release_please()

    def write(self, name: str, data) -> None:
        text = data if isinstance(data, str) else yaml.safe_dump(data, sort_keys=False)
        (self.root / ".github" / "workflows" / name).write_text(text)

    def errors(self, repo="OnePeek/backend-service") -> list[str]:
        self.write("checks.yml", self.checks)
        self.write("release-please.yml", self.rp)
        errors, _ = crs.check_repo(repo, self.root)
        return errors

    def assertFails(self, needle: str, repo="OnePeek/backend-service"):
        errors = self.errors(repo)
        self.assertTrue(any(needle in e for e in errors), errors)


class Passes(Repo):
    def test_a_caller_that_follows_every_rule_passes(self):
        self.assertEqual(self.errors(), [])

    def test_main_prints_ok_and_exits_0(self):
        self.write("checks.yml", self.checks)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = crs.main(["--repo", "OnePeek/backend-service", str(self.root)])
        self.assertEqual(code, 0)
        self.assertIn("OnePeek/backend-service OK", out.getvalue())

    def test_a_repo_without_a_release_branch_passes_with_a_notice(self):
        for repo in ("OnePeek/mcp-athena", "onepeek-design-system", "OnePeek/.github"):
            with self.subTest(repo=repo):
                errors, notice = crs.check_repo(repo, self.root / "missing")
                self.assertEqual(errors, [])
                self.assertIn("no release branch", notice)

    def test_web_and_infra_release_specific_lists(self):
        web = backend_checks()
        web["jobs"]["no-prerelease-deps"] = web["jobs"].pop("release-checks")
        self.checks = web
        self.assertEqual(self.errors("OnePeek/onepeek-web"), [])
        infra = backend_checks()
        del infra["jobs"]["release-checks"]
        self.checks = infra
        self.assertEqual(self.errors("onepeek-infra"), [])

    def test_a_repo_without_release_please_passes(self):
        self.write("checks.yml", self.checks)
        self.assertEqual(crs.check_repo("backend-service", self.root)[0], [])


class ChecksYml(Repo):
    def test_a_new_job_that_is_neither_fails(self):
        self.checks["jobs"]["lint"] = {"needs": "depends-on", "uses": "x"}
        self.assertFails("job `lint` is not release-specific")
        self.assertFails("job `lint` is tree-only but not in `certified.needs`")

    def test_a_tree_only_job_missing_from_certified_needs_fails(self):
        self.checks["jobs"]["certified"]["needs"].remove("python")
        self.assertFails("job `python` is tree-only but not in `certified.needs`")

    def test_a_release_specific_job_in_certified_needs_fails(self):
        self.checks["jobs"]["certified"]["needs"].append("contract")
        self.assertFails("holds jobs that are not tree-only: contract")

    def test_a_job_only_release_specific_in_another_repo_is_tree_only_here(self):
        # release-checks is backend's; in web it is neither, so it fails.
        web = backend_checks()
        web["jobs"]["no-prerelease-deps"] = {"needs": "depends-on", "uses": "x"}
        self.checks = web
        self.assertFails("job `release-checks` is not release-specific", repo="onepeek-web")

    def test_certified_listing_itself_fails(self):
        self.checks["jobs"]["certified"]["needs"].append("certified")
        self.assertFails("`certified` lists itself")

    def test_no_certified_job_fails(self):
        del self.checks["jobs"]["certified"]
        self.assertFails("no `certified` job")

    def test_certified_if_must_be_exact(self):
        for bad in ("${{ always() }}", "always()", None, True,
                    CERT_IF.replace("refs/heads/dev", "refs/heads/release"),
                    CERT_IF.replace(" || github.event_name == 'workflow_dispatch'", ""),
                    "${{ github.ref == 'refs/heads/dev' && (github.event_name == 'push' || github.event_name == 'workflow_dispatch') }}"):
            with self.subTest(bad=bad):
                self.checks = backend_checks()
                if bad is None:
                    del self.checks["jobs"]["certified"]["if"]
                else:
                    self.checks["jobs"]["certified"]["if"] = bad
                self.assertFails("`certified`'s `if` must be exactly")

    def test_certified_if_whitespace_and_braces_are_normalized(self):
        self.checks["jobs"]["certified"]["if"] = "  " + CERT_IF[4:-3].replace(" && ", "\n  &&  ") + "  "
        self.assertEqual(self.errors(), [])

    def test_the_skip_condition_must_be_a_top_level_and_operand(self):
        hostile = [
            "github.event_name != 'push'",                                   # today's condition
            "${{ github.x || " + SKIP + " }}",                                # an || makes it optional
            "${{ a || b && " + SKIP + " }}",                                 # && binds tighter than ||
            "${{ (a && " + SKIP + ") }}",                                    # inside parentheses, not top level
            "${{ !(" + SKIP + ") }}",                                        # negated
            "${{ '" + SKIP + "' }}",                                         # a string literal
            "${{ a == '" + SKIP.replace("'", "''") + "' && b }}",            # quoted inside a comparison
            SKIP.replace("'dev'", "'devx'"),                                 # not the canonical string
            SKIP.replace("!(", "! ("),                                       # spacing inside the condition
            "${{ " + SKIP + " && (a }}",                                     # unbalanced
            "${{ " + SKIP + " && 'a }}",                                     # unterminated string
            "${{ " + SKIP + ") && (a }}",                                    # closes before it opens
        ]
        for bad in hostile:
            with self.subTest(bad=bad):
                self.checks = backend_checks()
                self.checks["jobs"]["python"]["if"] = bad
                self.assertFails("job `python` is not release-specific and its `if` does not carry")

    def test_unreadable_checks_fail_closed(self):
        for text in ("jobs: [", "", "jobs: 3\n", "jobs:\n  a: 3\n"):
            with self.subTest(text=text):
                self.write("checks.yml", text)
                errors, _ = crs.check_repo("backend-service", self.root)
                self.assertTrue(errors)
        (self.root / ".github" / "workflows" / "checks.yml").unlink()
        errors, _ = crs.check_repo("backend-service", self.root)
        self.assertTrue(any("cannot read" in e for e in errors), errors)

    def test_bad_needs_shape_fails(self):
        self.checks["jobs"]["certified"]["needs"] = {"python": 1}
        self.assertFails("is not a job name or a list")


class ReleasePlease(Repo):
    def test_a_release_please_step_without_token_fails(self):
        self.rp = release_please(token=None)
        self.assertFails("release-please.yml: job `release-please`: the release-please step must pass")

    def test_the_token_must_come_from_an_earlier_app_token_step(self):
        cases = {
            "GITHUB_TOKEN": release_please(token="${{ secrets.GITHUB_TOKEN }}"),
            "github.token": release_please(token="${{ github.token }}"),
            "other step id": release_please(token="${{ steps.other.outputs.token }}"),
            "mint after": release_please(mint_first=False),
            "not the App action": release_please(mint_uses="tibdex/github-app-token@x"),
            "other output": release_please(token="${{ steps.app.outputs.app-slug }}"),
            "extra text": release_please(token="${{ steps.app.outputs.token }}x"),
        }
        for name, rp in cases.items():
            with self.subTest(name):
                self.rp = rp
                self.assertFails("the release-please step must pass")

    def test_every_workflow_file_is_checked(self):
        self.write("other.yaml", release_please(token=None))
        self.assertFails("other.yaml: job `release-please`")

    def test_an_unreadable_workflow_fails_closed(self):
        self.write("broken.yml", "jobs: [")
        self.assertFails("broken.yml: cannot read it")


class Main(Repo):
    def test_errors_exit_1(self):
        bad = copy.deepcopy(self.checks)
        del bad["jobs"]["certified"]
        self.write("checks.yml", bad)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = crs.main(["--repo", "backend-service", str(self.root)])
        self.assertEqual(code, 1)
        self.assertIn("::error::release skips:", out.getvalue())


if __name__ == "__main__":
    unittest.main()
