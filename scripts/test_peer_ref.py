"""Tests of scripts/peer_ref.py, actions/peer-ref and the rule that no workflow computes another repo's ref itself
(onepeek-docs architecture/lld/infra/delivery-pipelines.md §6.5). Run: python -m unittest discover -s scripts -p 'test_*.py'"""

import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/peer_ref.py"
ACTION = yaml.safe_load((ROOT / "actions/peer-ref/action.yml").read_text(encoding="utf-8"))
B, W, I = "b" * 40, "c" * 40, "d" * 40
PEERS = ("onepeek-infra", "backend-service", "onepeek-web", "mcp-athena")


def fake_gh(d: str, body: str) -> None:
    stub = Path(d, "gh")
    stub.write_text(f'#!/bin/bash\necho "gh $*" >> "{d}/calls"\n{body}\n')
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)


def answers(**by_repo: str) -> str:
    """A gh body answering each repo's promotion pull request list (empty string: none open)."""
    cases = f"*repos/OnePeek/*/commits/dev*) echo {'e' * 40};;\n*repos/OnePeek/*/commits/release*) echo {'f' * 40};;\n" + "".join(f'*repos/OnePeek/{r.replace("_", "-")}/pulls?state=open\\&base=release\\&head=OnePeek:dev*) {a};;\n' for r, a in by_repo.items())
    return f'case "$*" in\n{cases}*) echo "unexpected: $*" >&2; exit 9;;\nesac'


def peer(base, head, repos, gh="exit 9", owner="OnePeek", *extra):
    with tempfile.TemporaryDirectory() as d:
        fake_gh(d, gh)
        out = subprocess.run([sys.executable, str(SCRIPT), "--base", base, "--head", head, "--owner", owner, "--repos", repos, *extra],
                             capture_output=True, text=True, env={**os.environ, "PATH": f"{d}:{os.environ['PATH']}", "GH_TOKEN": "t"})
        calls = Path(d, "calls").read_text() if Path(d, "calls").exists() else ""
        return out, calls


def refs(out) -> dict:
    return dict(line.split("=", 1) for line in out.stdout.split())


class Rule(unittest.TestCase):
    ALL = "onepeek-infra,backend-service,onepeek-web"
    OPEN = answers(onepeek_infra=f"echo {I}", backend_service=f"echo {B}", onepeek_web="true")

    def test_a_pull_request_into_dev_is_dev_and_never_asks_github(self):
        out, calls = peer("dev", "feature/x", self.ALL)
        self.assertEqual((out.returncode, out.stdout), (0, "onepeek-infra=dev\nbackend-service=dev\nonepeek-web=dev\n"))
        self.assertEqual(calls, "")

    def test_a_hotfix_into_dev_is_still_dev(self):
        self.assertEqual(refs(peer("dev", "hotfix/x", self.ALL)[0]), dict.fromkeys(self.ALL.split(","), "dev"))

    def test_a_hotfix_into_release_is_release_and_never_asks_github(self):
        out, calls = peer("release", "hotfix/x", self.ALL)
        self.assertEqual(refs(out), dict.fromkeys(self.ALL.split(","), "release"))
        self.assertEqual(calls, "")

    def test_a_promotion_is_the_head_of_each_repos_open_promotion_else_release(self):
        out, calls = peer("release", "dev", self.ALL, self.OPEN)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(refs(out), {"onepeek-infra": I, "backend-service": B, "onepeek-web": "release"})
        self.assertEqual(out.stdout.count("\n"), 3)
        self.assertEqual(calls.count("pulls?state=open&base=release&head=OnePeek:dev"), 3)

    def test_any_non_hotfix_head_into_release_is_a_promotion(self):
        self.assertEqual(refs(peer("release", "feature/hotfix/x", "onepeek-infra", self.OPEN)[0]), {"onepeek-infra": I})
        self.assertEqual(refs(peer("release", "hotfixes", "onepeek-infra", self.OPEN)[0]), {"onepeek-infra": I})

    def test_commits_resolves_every_ref_to_its_commit(self):
        out, calls = peer("release", "dev", self.ALL, self.OPEN, "OnePeek", "--commits")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.splitlines(), [f"onepeek-infra={I} {I}", f"backend-service={B} {B}", f"onepeek-web=release {'f' * 40}"])
        self.assertNotIn("commits/" + I, calls)  # a SHA is its own commit: no read
        self.assertEqual(peer("dev", "x", "onepeek-web", self.OPEN, "OnePeek", "--commits")[0].stdout, f"onepeek-web=dev {'e' * 40}\n")

    def test_commits_reads_exactly_the_documented_endpoints(self):  # §6.5 Interface: the pulls list, then commits/<branch ref>
        _, calls = peer("release", "dev", "onepeek-web,onepeek-infra", self.OPEN, "OnePeek", "--commits")
        paths = [c.split()[2] for c in calls.splitlines()]
        self.assertEqual(paths, ["repos/OnePeek/onepeek-web/pulls?state=open&base=release&head=OnePeek:dev",
                                 "repos/OnePeek/onepeek-infra/pulls?state=open&base=release&head=OnePeek:dev",
                                 "repos/OnePeek/onepeek-web/commits/release"])
        for line in peer("release", "dev", "onepeek-web", self.OPEN, "OnePeek", "--commits")[0].stdout.splitlines():
            self.assertRegex(line, r"^onepeek-web=release [0-9a-f]{40}$")

    def test_a_commit_read_that_cannot_start_fails_closed(self):  # gh missing or not executable: OSError, refused
        sys.path.insert(0, str(SCRIPT.parent))
        try:
            import peer_ref
        finally:
            sys.path.pop(0)
        with mock.patch.object(peer_ref.subprocess, "run", side_effect=FileNotFoundError("gh")):
            with self.assertRaises(peer_ref.Refused):
                peer_ref.commit("OnePeek", "onepeek-web", "dev")
            with self.assertRaises(peer_ref.Refused):
                peer_ref.candidate("OnePeek", "onepeek-web")

    def test_commits_that_cannot_be_read_fail_closed(self):
        out, _ = peer("dev", "x", "onepeek-web", "exit 1", "OnePeek", "--commits")
        self.assertEqual((out.returncode, out.stdout), (1, ""))
        out, _ = peer("dev", "x", "onepeek-web", "echo main", "OnePeek", "--commits")
        self.assertEqual((out.returncode, out.stdout), (1, ""))

    def test_the_owner_is_the_one_asked(self):
        _, calls = peer("release", "dev", "onepeek-web", "echo", owner="Acme")
        self.assertIn("repos/Acme/onepeek-web/pulls", calls)

    def test_two_open_promotions_fail_closed(self):
        out, _ = peer("release", "dev", "onepeek-web", answers(onepeek_web=f"echo {B}; echo {W}"))
        self.assertEqual((out.returncode, out.stdout), (1, ""))
        self.assertIn("::error::", out.stderr)

    def test_an_unreadable_list_fails_closed_with_nothing_on_stdout(self):
        out, _ = peer("release", "dev", "onepeek-infra,onepeek-web", 'echo "HTTP 403" >&2; exit 1')
        self.assertEqual((out.returncode, out.stdout), (1, ""))
        self.assertIn("::error::Cannot read onepeek-infra's open pull requests", out.stderr)

    def test_a_head_that_is_not_a_full_sha_fails_closed(self):
        for bad in ("main", "abc123", "B" * 40, "b" * 39):
            out, _ = peer("release", "dev", "onepeek-web", answers(onepeek_web=f"echo {bad}"))
            self.assertEqual((out.returncode, out.stdout), (1, ""), bad)

    def test_a_base_other_than_dev_or_release_fails_closed(self):
        for base in ("main", "", "Dev"):
            out, calls = peer(base, "x", "onepeek-web")
            self.assertEqual((out.returncode, out.stdout, calls), (1, "", ""), base)
            self.assertIn("::error::base must be dev or release", out.stderr)

    def test_a_repo_that_is_not_a_peer_fails_closed(self):
        for repos in ("onepeek-docs", "onepeek-web,.github", "", "web"):
            self.assertEqual(peer("dev", "x", repos)[0].returncode, 1, repos)
        self.assertEqual(peer("dev", "x", "mcp-athena")[0].stdout, "mcp-athena=dev\n")

    def test_a_bad_owner_fails_closed(self):
        self.assertEqual(peer("dev", "x", "onepeek-web", owner="a/b")[0].returncode, 1)


class Action(unittest.TestCase):
    """actions/peer-ref/action.yml: the composite step's script, run against a fake gh."""
    STEP = ACTION["runs"]["steps"][0]

    def run_action(self, base, head, repos, gh="exit 9"):
        with tempfile.TemporaryDirectory() as d:
            fake_gh(d, gh)
            out, summary = Path(d, "out"), Path(d, "summary")
            env = {**os.environ, "PATH": f"{d}:{os.environ['PATH']}", "BASE": base, "HEAD": head, "REPOS": repos, "OWNER": "OnePeek",
                   "GH_TOKEN": "t", "ACTION_PATH": str(ROOT / "actions/peer-ref"), "GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(summary)}
            r = subprocess.run(["bash", "-e", "-c", self.STEP["run"]], capture_output=True, text=True, env=env)
            read = lambda p: p.read_text() if p.exists() else ""  # noqa: E731
            return r, dict(x.split("=", 1) for x in read(out).split()), read(summary).splitlines()

    def test_the_interface_is_base_head_repos_token_and_a_composite(self):
        self.assertEqual(set(ACTION["inputs"]), {"base", "head", "repos", "token"})
        self.assertEqual(ACTION["runs"]["using"], "composite")
        self.assertEqual(ACTION["runs"]["steps"][0]["shell"], "bash")

    def test_it_runs_the_script_at_its_own_pin_and_inlines_no_input(self):
        self.assertIn('"${ACTION_PATH}/../../scripts/peer_ref.py"', self.STEP["run"])
        self.assertEqual(self.STEP["env"]["ACTION_PATH"], "${{ github.action_path }}")
        self.assertNotIn("${{", self.STEP["run"])
        self.assertEqual(self.STEP["env"]["GH_TOKEN"], "${{ inputs.token }}")

    def test_it_writes_each_ref_under_the_short_name_and_records_the_peer(self):
        r, outputs, summary = self.run_action("release", "dev", "onepeek-infra,backend-service,onepeek-web,mcp-athena",
                                              answers(onepeek_infra=f"echo {I}", backend_service=f"echo {B}", onepeek_web="true", mcp_athena="true"))
        self.assertEqual(r.returncode, 0, r.stderr)
        f = "f" * 40
        self.assertEqual(outputs, {"infra": I, "backend": B, "web": f, "mcp": f})  # the commit, never a branch name (S2)
        self.assertEqual(summary, [f"peer: onepeek-infra@{I}", f"peer: backend-service@{B}", f"peer: onepeek-web@{f}", f"peer: mcp-athena@{f}"])
        self.assertEqual([ln for ln in r.stdout.splitlines() if ln.startswith("peer:")], summary)  # also in the log

    def test_a_dev_ref_is_output_as_its_commit(self):
        r, outputs, summary = self.run_action("dev", "feature/x", "onepeek-web", answers())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((outputs, summary), ({"web": "e" * 40}, ["peer: onepeek-web@" + "e" * 40]))

    def test_every_output_is_declared_and_maps_to_the_resolving_step(self):  # a composite action exposes only declared outputs
        self.assertEqual(self.STEP["id"], "resolve")
        self.assertEqual({k: v["value"] for k, v in ACTION["outputs"].items()},
                         {k: "${{ steps.resolve.outputs.%s }}" % k for k in ("infra", "backend", "web", "mcp")})

    def test_a_refusal_fails_the_step_and_writes_nothing(self):
        r, outputs, summary = self.run_action("release", "dev", "onepeek-web", 'exit 1')
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual((outputs, summary), ({}, []))


# --- the rule that no workflow computes another repo's ref itself (§6.5 "Tests") ---

# workflow-lint.yml's service-list job reads onepeek-infra's service list at `dev`: it is .github's own lint, not a check
# of a change against a peer's release (§6.5 Build rulings 3), so exactly this job is allowed a fixed ref.
FIXED_REF_OK = {("workflow-lint.yml", "service-list", "onepeek-infra", "dev")}
SHARED_SCRIPT = re.compile(r"(^|[\s;&|(])python3?\s+[\x22']?_shared/scripts/peer_ref\.py[\x22']?\s", re.M)


def violations(text: str, name: str = "x.yml") -> list[str]:
    """What a workflow does wrong under §6.5. Callers' fixtures go through the same function."""
    doc = yaml.safe_load(text)
    found = []
    for job_id, job in (doc.get("jobs") or {}).items():
        steps = job.get("steps") or []
        # A source of a peer's ref is only the peer-ref action or a run step calling the `_shared` checkout's script.
        sources = {s["id"] for s in steps if s.get("id") and (str(s.get("uses", "")).lower().startswith("onepeek/.github/actions/peer-ref@")
                                                              or SHARED_SCRIPT.search(str(s.get("run", ""))))}
        for s in steps:
            run = str(s.get("run", "")).lower()
            # Any read of the open pull requests into release, in any order or flag form (`?state=open&base=release`,
            # `-f state=open -f base=release`, `--base release --state open`).
            if re.search(r"\bbase\W{1,3}release\b", run) and re.search(r"\bstate\W{1,3}open\b", run):
                found.append(f"{name}:{job_id}: computes a peer's release candidate itself")
            if not str(s.get("uses", "")).lower().startswith("actions/checkout@"):
                continue
            w = s.get("with") or {}
            repo, ref = str(w.get("repository", "")).lower(), str(w.get("ref", ""))
            if any(repo.endswith("/" + p) for p in PEERS) and (name, job_id, repo.split("/")[-1], ref) not in FIXED_REF_OK and not any(f"steps.{i}.outputs." in ref for i in sources):
                found.append(f"{name}:{job_id}: checks out {repo} at '{ref}', not a peer-ref output")
            if repo == "onepeek/.github" and w.get("path") == "_shared" and ref != "${{ job.workflow_sha }}" and name == "contract.yml":
                found.append(f"{name}:{job_id}: _shared must be at ${{{{ job.workflow_sha }}}}, not '{ref}'")
    return found


class ActionlintIgnore(unittest.TestCase):
    """§6.5 Build rulings 1: actionlint does not know `job.workflow_sha` yet; exactly that message is ignored."""
    CONFIG = yaml.safe_load((ROOT / ".github/actionlint.yaml").read_text(encoding="utf-8"))

    def test_only_the_workflow_sha_message_is_ignored(self):
        patterns = [p for v in (self.CONFIG.get("paths") or {}).values() for p in v.get("ignore", [])]
        self.assertEqual(patterns, ['property "workflow_sha" is not defined in object type'])
        self.assertEqual(set(self.CONFIG), {"paths"})  # no global ignore, no other setting
        self.assertEqual(list(self.CONFIG["paths"]), [".github/workflows/contract.yml"])

    def test_the_config_is_where_actionlint_looks_from_the_repo_root(self):
        self.assertTrue((ROOT / ".github/actionlint.yaml").is_file())


class NoCopies(unittest.TestCase):
    def test_no_workflow_in_dot_github_computes_a_peers_ref(self):
        for f in sorted((ROOT / ".github/workflows").glob("*.y*ml")):
            self.assertEqual(violations(f.read_text(encoding="utf-8"), f.name), [], f.name)

    def test_contract_checks_the_peers_out_through_the_script_and_shared_at_the_workflow_sha(self):
        text = (ROOT / ".github/workflows/contract.yml").read_text(encoding="utf-8")
        self.assertIn("ref: ${{ job.workflow_sha }}", text)
        self.assertEqual(violations(text, "contract.yml"), [])
        # Mutation: _shared at a branch, or with no ref, fails.
        self.assertEqual(len(violations(text.replace("ref: ${{ job.workflow_sha }}", "ref: dev"), "contract.yml")), 1)
        self.assertEqual(len(violations(text.replace("          ref: ${{ job.workflow_sha }}\n", ""), "contract.yml")), 1)

    def test_a_peer_checkout_at_the_base_branch_fails(self):  # web e2e.yml today
        wf = """
jobs:
  e2e:
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7
        with:
          repository: OnePeek/backend-service
          ref: ${{ github.base_ref }}
"""
        self.assertEqual(len(violations(wf)), 1)

    def test_a_peer_checkout_at_a_peer_ref_output_passes(self):
        wf = """
jobs:
  e2e:
    steps:
      - id: peer
        uses: OnePeek/.github/actions/peer-ref@%s # v1.0.0
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7
        with:
          repository: ${{ github.repository_owner }}/backend-service
          ref: ${{ steps.peer.outputs.backend }}
""" % ("a" * 40)
        self.assertEqual(violations(wf), [])
        # Same output name but the step is not peer-ref: fails.
        self.assertEqual(len(violations(wf.replace("OnePeek/.github/actions/peer-ref@", "actions/other@"))), 1)

    def test_a_pulls_call_for_the_release_candidate_fails(self):
        wf = """
jobs:
  j:
    steps:
      - run: gh api "repos/${OWNER}/${repo}/pulls?state=open&base=release&head=${OWNER}:dev" --jq '.[].head.sha'
"""
        self.assertEqual(len(violations(wf)), 1)

    def test_every_form_of_the_open_pulls_into_release_read_fails(self):
        for run in ('gh api repos/o/r/pulls -X GET -f state=open -f base=release --jq ".[].head.sha"',
                    "gh api repos/o/r/pulls -X GET -f base=release -f state=open",
                    "gh api 'repos/o/r/pulls?base=release&state=open'",
                    "gh pr list -R o/r --base release --state open --json headRefOid",
                    "GH API REPOS/O/R/PULLS -F STATE=OPEN -F BASE=RELEASE"):
            self.assertEqual(len(violations("jobs:\n  j:\n    steps:\n      - run: %s\n" % run)), 1, run)
        self.assertEqual(violations("jobs:\n  j:\n    steps:\n      - run: gh pr list --base release --state merged\n"), [])

    PEER_CHECKOUT = """
jobs:
  e2e:
    steps:
      - id: peer
        %s
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7
        with:
          repository: %s
          ref: ${{ steps.peer.outputs.backend }}
"""

    def test_a_peer_named_in_any_letter_case_is_still_a_peer(self):
        for repo in ("OnePeek/Backend-Service", "ONEPEEK/BACKEND-SERVICE"):
            wf = self.PEER_CHECKOUT % ("run: echo backend=dev >> $GITHUB_OUTPUT", repo)
            self.assertEqual(len(violations(wf)), 1, repo)
        wf = (self.PEER_CHECKOUT % ("run: echo x", "OnePeek/Backend-Service")).replace("actions/checkout@", "Actions/Checkout@")
        self.assertEqual(len(violations(wf)), 1)

    def test_only_the_action_or_a_call_of_the_shared_script_is_a_source(self):
        ok = ("uses: OnePeek/.github/actions/peer-ref@" + "a" * 40, "run: python3 _shared/scripts/peer_ref.py --base x --commits",
              'run: lines=$(python3 "_shared/scripts/peer_ref.py" --base x)')
        for src in ok:
            self.assertEqual(violations(self.PEER_CHECKOUT % (src, "OnePeek/backend-service")), [], src)
        bad = ("run: echo peer_ref.py; echo backend=dev >> $GITHUB_OUTPUT", "run: python3 scripts/peer_ref.py --base x",
               "run: cat _shared/scripts/peer_ref.py", "run: echo python3 _shared/scripts/peer_ref.py.bak x",
               "uses: evil/actions/peer-ref@" + "a" * 40)
        for src in bad:
            self.assertEqual(len(violations(self.PEER_CHECKOUT % (src, "OnePeek/backend-service"))), 1, src)


if __name__ == "__main__":
    unittest.main()
