"""Tests of .github/workflows/contract.yml (decision 153; delivery-pipelines §6.1 row "contract between repos").
Run: python -m unittest discover -s scripts -p 'test_*.py'"""

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


class OtherBranch(unittest.TestCase):
    def test_dev_for_a_pull_request_into_dev_and_release_for_a_release_pull_request(self):
        for base in ("dev", "release"):
            out, output = run("The other repos' branch", {"ROLE": "backend", "BASE_REF": base})
            self.assertEqual(out.returncode, 0, out.stdout)
            self.assertEqual(output.strip(), f"other={base}")

    def test_any_other_base_or_role_refuses(self):
        self.assertEqual(run("The other repos' branch", {"ROLE": "backend", "BASE_REF": "main"})[0].returncode, 1)
        self.assertEqual(run("The other repos' branch", {"ROLE": "docs", "BASE_REF": "dev"})[0].returncode, 1)

    def test_every_other_repo_is_checked_out_at_that_branch_with_the_read_token(self):
        for s in STEPS:
            if "repository" in s.get("with", {}):
                self.assertEqual(s["with"]["ref"], "${{ steps.ref.outputs.other }}")
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
