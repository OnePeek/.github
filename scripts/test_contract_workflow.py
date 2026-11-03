"""Tests of .github/workflows/contract.yml (decision 153; delivery-pipelines §6.1 row "contract between repos").
Run: python -m unittest discover -s scripts -p 'test_*.py'"""

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/contract.yml"
DOC = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
STEPS = DOC["jobs"]["contract"]["steps"]


def step(name: str) -> dict:
    return next(s for s in STEPS if s.get("name", "").startswith(name))


def condition(s: dict, role: str) -> bool:
    """Evaluates the step's `if:` for a role (the only forms used: none, role == 'x', role != 'x')."""
    expr = str(s.get("if", "")).strip()
    if not expr:
        return True
    match = re.fullmatch(r"\$\{\{ inputs\.role (==|!=) '(\w+)' \}\}", expr)
    assert match, expr
    return (role == match.group(2)) == (match.group(1) == "==")


def run(name: str, env: dict[str, str]) -> tuple[subprocess.CompletedProcess[str], str]:
    with tempfile.TemporaryDirectory() as d:
        output = Path(d, "output")
        out = subprocess.run(["bash", "-eo", "pipefail", "-c", step(name)["run"]], capture_output=True, text=True,
                             env={**os.environ, **env, "GITHUB_OUTPUT": str(output)})  # fmt: skip
        return out, output.read_text() if output.exists() else ""


def checkouts(role: str) -> dict[str, str]:
    """{path: repository or 'this pull request'} for the checkouts a role runs."""
    found = {}
    for s in STEPS:
        if str(s.get("uses", "")).startswith("actions/checkout@") and condition(s, role):
            path = s["with"]["path"].replace("${{ inputs.role }}", role)
            found[path] = str(s["with"].get("repository", "this pull request")).split("/")[-1]
    return found


FAKE_GH = """#!/bin/bash
# Stands in for gh api: prints $FAKE_PULLS_<repo, - as _> (a JSON list of {number, head:{sha}}) through the --jq filter.
repo=$(sed -E 's#repos/[^/]+/([^/]+)/pulls.*#\\1#' <<<"$2" | tr - _)
var="FAKE_PULLS_${repo}"
jq -c "$4" <<<"${!var:-[]}"
"""


def pulls(*numbers: int) -> str:
    return json.dumps([{"number": n, "head": {"sha": f"sha{n}"}} for n in numbers])


def resolve(role: str, base: str, **by_repo: str) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    with tempfile.TemporaryDirectory() as d:
        gh = Path(d, "gh")
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
        env = {"ROLE": role, "BASE_REF": base, "OWNER": "OnePeek", "GH_TOKEN": "t", "PATH": f"{d}:{os.environ['PATH']}"}
        env.update({f"FAKE_PULLS_{k}": v for k, v in by_repo.items()})
        out, output = run("The other repos' ref", env)
        return out, dict(line.split("=", 1) for line in output.split())


class OtherRef(unittest.TestCase):
    def test_a_pull_request_into_dev_takes_the_other_repos_at_dev(self):
        out, refs = resolve("infra", "dev", backend_service=pulls(7), onepeek_web=pulls(8))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(refs, {"backend": "dev", "web": "dev"})

    def test_a_release_pull_request_takes_each_other_repo_at_its_release_candidate_else_dev_never_release(self):
        out, refs = resolve("infra", "release", backend_service=pulls(7))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(refs, {"backend": "sha7", "web": "dev"})
        out, refs = resolve("web", "release")
        self.assertEqual(refs, {"backend": "dev", "infra": "dev"})

    def test_more_than_one_open_release_pull_request_fails_naming_them(self):
        out, _ = resolve("infra", "release", backend_service=pulls(7, 9))
        self.assertEqual(out.returncode, 1)
        self.assertIn("backend-service has 2 open pull requests into release (#7, #9)", out.stdout)

    def test_any_other_base_or_role_refuses(self):
        self.assertEqual(resolve("backend", "main")[0].returncode, 1)
        self.assertEqual(resolve("docs", "dev")[0].returncode, 1)

    def test_the_lookup_uses_the_read_token_and_never_inlines_event_inputs(self):
        s = step("The other repos' ref")
        self.assertEqual(s["env"]["GH_TOKEN"], "${{ secrets.ORG_READ_TOKEN }}")
        self.assertNotIn("${{", s["run"])

    def test_every_other_repo_is_checked_out_at_its_own_ref_with_the_read_token(self):
        refs = {"onepeek-infra": "infra", "backend-service": "backend", "onepeek-web": "web"}
        for s in STEPS:
            if "repository" in s.get("with", {}):
                repo = s["with"]["repository"].split("/")[-1]
                self.assertEqual(s["with"]["ref"], "${{ steps.ref.outputs." + refs[repo] + " }}")
                self.assertEqual(s["with"]["token"], "${{ secrets.ORG_READ_TOKEN }}")
                self.assertFalse(s["with"]["persist-credentials"])


class Sides(unittest.TestCase):
    def test_each_role_checks_out_itself_at_the_pull_request_and_what_it_is_checked_against(self):
        self.assertEqual(checkouts("backend"), {"_backend": "this pull request", "_infra": "onepeek-infra"})
        self.assertEqual(checkouts("web"), {"_web": "this pull request", "_infra": "onepeek-infra"})
        self.assertEqual(checkouts("infra"), {"_infra": "this pull request", "_backend": "backend-service", "_web": "onepeek-web"})

    def test_the_checks_run_infras_one_contract_script_with_each_roles_sides(self):
        script = step("The contract between repos")["run"]
        self.assertIn('python _infra/scripts/contract.py --infra _infra "${sides[@]}"', script)
        self.assertIn("backend) sides=(--backend _backend) ;;", script)
        self.assertIn("web) sides=(--web _web) ;;", script)
        self.assertIn("infra) sides=(--backend _backend --web _web) ;;", script)
        self.assertIn("AWS_SHARED_CREDENTIALS_FILE=/dev/null AWS_CONFIG_FILE=/dev/null", script)

    def test_the_backend_is_synced_and_node_set_up_only_where_used(self):
        self.assertEqual([r for r in ("backend", "web", "infra") if condition(step("The backend's own environment"), r)], ["backend", "infra"])
        node = next(s for s in STEPS if str(s.get("uses", "")).startswith("actions/setup-node@"))
        self.assertEqual([r for r in ("backend", "web", "infra") if condition(node, r)], ["web", "infra"])
        self.assertEqual(node["with"]["node-version-file"], "_web/.node-version")


if __name__ == "__main__":
    unittest.main()
