"""Tests of .github/workflows/contract.yml (decision 153; delivery-pipelines §6.1 row "contract between repos").
Run: python -m unittest discover -s scripts -p 'test_*.py'"""

import os
import re
import stat
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


def run(name: str, env: dict[str, str], gh: str = "echo gh must not be called >&2; exit 9") -> tuple[subprocess.CompletedProcess[str], str]:
    """Runs a step with a stub `gh` (its calls are appended to <tmp>/calls; the body sees "$*")."""
    with tempfile.TemporaryDirectory() as d:
        output = Path(d, "output")
        stub = Path(d, "gh")
        stub.write_text(f'#!/bin/bash\necho "gh $*" >> "{d}/calls"\n{gh}\n')
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        out = subprocess.run(["bash", "-eo", "pipefail", "-c", step(name)["run"]], capture_output=True, text=True,
                             env={**os.environ, **env, "OWNER": "OnePeek", "GITHUB_OUTPUT": str(output), "PATH": f"{d}:{os.environ['PATH']}"})  # fmt: skip
        calls = Path(d, "calls")
        out.calls = calls.read_text() if calls.exists() else ""  # type: ignore[attr-defined]
        return out, output.read_text() if output.exists() else ""


def checkouts(role: str) -> dict[str, str]:
    """{path: repository or 'this pull request'} for the checkouts a role runs."""
    found = {}
    for s in STEPS:
        if str(s.get("uses", "")).startswith("actions/checkout@") and condition(s, role):
            path = s["with"]["path"].replace("${{ inputs.role }}", role)
            found[path] = str(s["with"].get("repository", "this pull request")).split("/")[-1]
    return found


def resolve(role: str, base: str, head: str = "feature/x", gh: str | None = None) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    out, output = run("The other repos' ref", {"ROLE": role, "BASE_REF": base, "HEAD_REF": head}, *([gh] if gh else []))
    return out, dict(line.split("=", 1) for line in output.split())


SHA_BACKEND, SHA_WEB, SHA_INFRA = "b" * 40, "c" * 40, "d" * 40
# Open dev -> release pull requests: backend and infra have one, web has none.
OPEN_RELEASE_PRS = f"""case "$*" in
*repos/OnePeek/backend-service/pulls?state=open\\&base=release\\&head=OnePeek:dev*) echo {SHA_BACKEND};;
*repos/OnePeek/onepeek-infra/pulls?state=open\\&base=release\\&head=OnePeek:dev*) echo {SHA_INFRA};;
*repos/OnePeek/onepeek-web/pulls?state=open\\&base=release\\&head=OnePeek:dev*) ;;
*) echo "unexpected: $*" >&2; exit 9;;
esac"""


class OtherRef(unittest.TestCase):
    def test_dev_for_a_pull_request_into_dev(self):
        out, refs = resolve("infra", "dev")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(refs, {"backend": "dev", "web": "dev"})

    def test_a_release_pull_request_checks_each_other_repo_at_the_head_of_its_open_release_pull_request(self):
        # delivery-pipelines §6.1: the release candidate is the exact commit that repo's promotion ships, never a moving dev.
        out, refs = resolve("web", "release", "dev", OPEN_RELEASE_PRS)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(refs, {"backend": SHA_BACKEND, "infra": SHA_INFRA})
        self.assertEqual(resolve("backend", "release", "dev", OPEN_RELEASE_PRS)[1], {"web": "release", "infra": SHA_INFRA})

    def test_a_repo_with_no_open_release_pull_request_is_checked_at_release_what_runs(self):
        out, refs = resolve("infra", "release", "dev", OPEN_RELEASE_PRS)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(refs, {"backend": SHA_BACKEND, "web": "release"})
        self.assertNotIn("dev", refs.values())

    def test_only_the_dev_head_counts_as_the_release_candidate(self):
        out, _ = resolve("infra", "release", "dev", OPEN_RELEASE_PRS)
        self.assertEqual(out.calls.count("base=release&head=OnePeek:dev"), 2, out.calls)  # type: ignore[attr-defined]

    def test_an_unreadable_pull_request_list_fails_closed(self):
        out, refs = resolve("infra", "release", "dev", 'echo "HTTP 403: Resource not accessible" >&2; exit 1')
        self.assertEqual(out.returncode, 1)
        self.assertIn("release candidate is unknown", out.stdout)
        self.assertEqual(refs, {})

    def test_anything_but_one_commit_sha_fails_closed(self):
        for answer in ("echo " + SHA_BACKEND + "; echo " + SHA_WEB, "echo main"):
            out, _ = resolve("infra", "release", "dev", answer)
            self.assertEqual(out.returncode, 1, answer)

    def test_pull_requests_into_dev_never_ask_github(self):
        out, refs = resolve("backend", "dev")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(refs, {"web": "dev", "infra": "dev"})
        self.assertEqual(out.calls, "")  # type: ignore[attr-defined]

    def test_a_hotfix_pull_request_into_release_checks_the_other_repos_at_release(self):
        self.assertEqual(resolve("infra", "release", "hotfix/fix-x")[1], {"backend": "release", "web": "release"})

    def test_a_hotfix_branch_into_dev_is_still_dev(self):
        self.assertEqual(resolve("infra", "dev", "hotfix/fix-x")[1], {"backend": "dev", "web": "dev"})

    def test_any_other_base_or_role_refuses(self):
        self.assertEqual(resolve("backend", "main")[0].returncode, 1)
        self.assertEqual(resolve("docs", "dev")[0].returncode, 1)

    def test_event_inputs_never_inline_into_the_script(self):
        self.assertNotIn("${{", step("The other repos' ref")["run"])

    def test_every_other_repo_is_checked_out_at_its_own_ref_with_the_app_token(self):
        refs = {"onepeek-infra": "infra", "backend-service": "backend", "onepeek-web": "web"}
        for s in STEPS:
            if "repository" in s.get("with", {}):
                repo = s["with"]["repository"].split("/")[-1]
                self.assertEqual(s["with"]["ref"], "${{ steps.ref.outputs." + refs[repo] + " }}")
                self.assertEqual(s["with"]["token"], "${{ steps.app.outputs.token }}")
                self.assertFalse(s["with"]["persist-credentials"])


class AppToken(unittest.TestCase):
    """Playbook §8a S6: the App token, scoped to the repos the job names; no other token (the old ORG_READ_TOKEN is retired)."""

    def test_the_app_token_names_the_three_repos_with_contents_and_pull_requests_read(self):
        app = next(s for s in STEPS if s.get("id") == "app")
        self.assertEqual(app["with"]["repositories"], "backend-service,onepeek-web,onepeek-infra")
        self.assertEqual((app["with"]["permission-contents"], app["with"]["permission-pull-requests"]), ("read", "read"))
        self.assertNotIn("if", app)  # the App is the only way: the token step always runs
        self.assertEqual(step("The other repos' ref")["env"]["GH_TOKEN"], "${{ steps.app.outputs.token }}")

    def test_the_secrets_are_the_two_app_ones_and_no_org_read_token(self):
        secrets = DOC["on"]["workflow_call"]["secrets"] if "on" in DOC else DOC[True]["workflow_call"]["secrets"]
        self.assertEqual({k: v["required"] for k, v in secrets.items()},
                         {"AGENTS_APP_CLIENT_ID": False, "AGENTS_APP_PRIVATE_KEY": False})

    def test_without_the_app_secrets_it_fails_and_no_other_token_is_read(self):
        out, _ = run("The agents' App secrets are passed", {"HAS_APP": "false"})
        self.assertEqual(out.returncode, 1)
        out, _ = run("The agents' App secrets are passed", {"HAS_APP": "true"})
        self.assertEqual(out.returncode, 0)
        self.assertNotIn("ORG_READ_TOKEN", WORKFLOW.read_text(encoding="utf-8"))


class ServiceListJob(unittest.TestCase):
    """workflow-lint.yml service-list reads onepeek-infra with the App token (S6); its last step runs locally (P5)."""

    def test_infra_is_read_with_the_app_token_and_the_drift_step_needs_no_secret(self):
        steps = yaml.safe_load((WORKFLOW.parent / "workflow-lint.yml").read_text(encoding="utf-8"))["jobs"]["service-list"]["steps"]
        app = next(s for s in steps if s.get("id") == "app")
        self.assertEqual((app["with"]["repositories"], app["with"]["permission-contents"]), ("onepeek-infra", "read"))
        infra = next(s for s in steps if (s.get("with") or {}).get("path") == "_infra")
        self.assertEqual(infra["with"]["token"], "${{ steps.app.outputs.token }}")
        last = steps[-1]
        self.assertNotIn("secrets.", str(last))
        self.assertIn("_infra/config/services.preprod.yaml", last["run"])


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
