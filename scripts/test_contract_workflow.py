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


def resolve(role: str, base: str, head: str = "feature/x") -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    out, output = run("The other repos' ref", {"ROLE": role, "BASE_REF": base, "HEAD_REF": head})
    return out, dict(line.split("=", 1) for line in output.split())


class OtherRef(unittest.TestCase):
    def test_dev_for_a_pull_request_into_dev(self):
        out, refs = resolve("infra", "dev")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(refs, {"backend": "dev", "web": "dev"})

    def test_a_release_pull_request_checks_the_other_repos_at_dev_never_release(self):
        for role, others in (("infra", {"backend", "web"}), ("backend", {"web", "infra"}), ("web", {"backend", "infra"})):
            out, refs = resolve(role, "release", "dev")
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            self.assertEqual(refs, {o: "dev" for o in others})

    def test_a_hotfix_pull_request_into_release_checks_the_other_repos_at_release(self):
        self.assertEqual(resolve("infra", "release", "hotfix/fix-x")[1], {"backend": "release", "web": "release"})

    def test_a_hotfix_branch_into_dev_is_still_dev(self):
        self.assertEqual(resolve("infra", "dev", "hotfix/fix-x")[1], {"backend": "dev", "web": "dev"})

    def test_any_other_base_or_role_refuses(self):
        self.assertEqual(resolve("backend", "main")[0].returncode, 1)
        self.assertEqual(resolve("docs", "dev")[0].returncode, 1)

    def test_event_inputs_never_inline_into_the_script(self):
        self.assertNotIn("${{", step("The other repos' ref")["run"])

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
