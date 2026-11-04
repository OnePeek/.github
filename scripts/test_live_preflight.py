"""Tests for workspace/live_preflight.py (foundation-playbook §8a P9): IAM simulation, image memory, edge User-Agent.

AWS, Docker and HTTP are fakes: nothing here calls AWS, starts a container or opens a connection.
"""
from __future__ import annotations

import fnmatch
import importlib.util
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lp = _load("live_preflight", ROOT / "workspace" / "live_preflight.py")
pf = _load("preflight_for_live", ROOT / "workspace" / "preflight.py")

ACCT = "648457895309"
NODE_ARN = f"arn:aws:iam::{ACCT}:role/onepeek-sandbox-node"
SERVICE_ARN = f"arn:aws:servicediscovery:ap-south-1:{ACCT}:service/srv-1"
TOPIC_ARN = f"arn:aws:sns:ap-south-1:{ACCT}:onepeek-sandbox-events"


def doc(*statements) -> str:
    return json.dumps({"Version": "2012-10-17", "Statement": list(statements)})


def allow(actions, resources="*", sid="S"):
    return {"Sid": sid, "Effect": "Allow", "Action": actions, "Resource": resources}


REGISTER = allow(["servicediscovery:RegisterInstance", "servicediscovery:DeregisterInstance"], SERVICE_ARN, "Register")
HEALTH = allow(["route53:CreateHealthCheck", "route53:GetHealthCheck", "route53:UpdateHealthCheck",
                "route53:DeleteHealthCheck", "ec2:DescribeInstances"], "*", "Health")


# A small policy evaluator standing in for iam simulate-custom-policy -----------------------------------------------
def _matches(patterns, value) -> bool:
    return any(fnmatch.fnmatchcase(value, p) for p in lp.as_list(patterns))


def _cond_ok(st, ctx) -> tuple[bool, list[str]]:
    missing = []
    for op, keys in (st.get("Condition") or {}).items():
        for key, values in keys.items():
            if key not in ctx:
                missing.append(key)
                continue
            hit = any(v in lp.as_list(values) for v in ctx[key])
            if (op == "StringEquals" and not hit) or (op == "StringNotEquals" and hit):
                return False, []
    return not missing, missing


def _decide(docs, action, resource, ctx):
    allowed, missing = False, []
    for d in docs:
        for st in lp.statements(d):
            if not (_matches(st.get("Action", []), action) and _matches(st.get("Resource", "*"), resource)):
                continue
            ok, miss = _cond_ok(st, ctx)
            missing += miss
            if ok and st["Effect"] == "Deny":
                return "explicitDeny", missing
            if ok and st["Effect"] == "Allow":
                allowed = True
    return ("allowed" if allowed else "implicitDeny"), missing


class FakeAws:
    """Answers the read-only IAM calls the tool makes; records every call."""

    def __init__(self, managed=None, roles=None):
        self.calls, self.managed, self.roles = [], managed or {}, roles or {}

    def __call__(self, args):
        self.calls.append(args)
        op = args[1]
        val = lambda flag: args[args.index(flag) + 1]  # noqa: E731
        if op == "simulate-custom-policy":
            return True, {"EvaluationResults": self.simulate(args)}
        if op == "get-policy":
            arn = val("--policy-arn")
            return (True, {"Policy": {"DefaultVersionId": "v1"}}) if arn in self.managed else (False, "NoSuchEntity")
        if op == "get-policy-version":
            return True, {"PolicyVersion": {"Document": json.loads(self.managed[val("--policy-arn")])}}
        role = self.roles.get(val("--role-name"))
        if role is None:
            return False, "NoSuchEntity"
        if op == "get-role":
            r = {"Arn": role["arn"]}
            if role.get("boundary"):
                r["PermissionsBoundary"] = {"PermissionsBoundaryArn": role["boundary"]}
            return True, {"Role": r}
        if op == "list-role-policies":
            return True, {"PolicyNames": list(role["inline"])}
        if op == "get-role-policy":
            return True, {"PolicyDocument": json.loads(role["inline"][val("--policy-name")])}
        if op == "list-attached-role-policies":
            return True, {"AttachedPolicies": []}
        raise AssertionError(f"unexpected AWS call {args}")

    @staticmethod
    def _list(args, flag):
        i = args.index(flag) + 1
        out = []
        while i < len(args) and not args[i].startswith("--"):
            out.append(args[i])
            i += 1
        return out

    def simulate(self, args):
        policies = self._list(args, "--policy-input-list")
        boundary = self._list(args, "--permissions-boundary-policy-input-list") if "--permissions-boundary-policy-input-list" in args else None
        rp = args[args.index("--resource-policy") + 1] if "--resource-policy" in args else None
        ctx = {}
        if "--context-entries" in args:
            ctx = {e["ContextKeyName"]: e["ContextKeyValues"] for e in json.loads(args[args.index("--context-entries") + 1])}
        out = []
        for action in self._list(args, "--action-names"):
            for res in self._list(args, "--resource-arns"):
                d, missing = _decide(policies, action, res, ctx)
                if d == "allowed" and boundary is not None:
                    b, m2 = _decide(boundary, action, res, ctx)
                    d, missing = (d if b == "allowed" else "implicitDeny" if b == "implicitDeny" else b), missing + m2
                if rp is not None and d != "explicitDeny":
                    r, m3 = _decide([rp], action, res, ctx)
                    missing += m3
                    d = "explicitDeny" if r == "explicitDeny" else d
                e = {"EvalActionName": action, "EvalResourceName": res, "EvalDecision": d}
                if missing and d != "allowed":
                    e["MissingContextValues"] = missing
                out.append(e)
        return out


# Plan fixtures ----------------------------------------------------------------------------------------------------
def plan(resources, config=None, changes=None, module="module.stack.module.runtime"):
    return {
        "planned_values": {"root_module": {"child_modules": [{"address": module, "resources": [
            {"mode": "managed", "address": f"{module}.{r['type']}.{r['name']}{r.get('index', '')}", "type": r["type"],
             "name": r["name"], "values": r.get("values", {})} for r in resources]}]}},
        "configuration": {"root_module": {"module_calls": {"stack": {"module": {"module_calls": {"runtime": {"module": {
            "resources": [{"address": k, "expressions": v} for k, v in (config or {}).items()]}}}}}}}},
        "resource_changes": changes or [],
    }


def node_plan(policy, health=True, boundary=None, policy_known=True):
    res = [
        {"type": "aws_iam_role", "name": "node", "index": "[0]",
         "values": {"name": "onepeek-sandbox-node", "arn": NODE_ARN}},
        {"type": "aws_iam_role_policy", "name": "node_host_proxy", "index": "[0]",
         "values": {"role": "onepeek-sandbox-node", **({"policy": policy} if policy_known else {})}},
        {"type": "aws_service_discovery_service", "name": "egress_proxy",
         "values": {"name": "egress-proxy", **({"health_check_custom_config": [{}]} if health else {})}},
    ]
    config = {"aws_iam_role_policy.node_host_proxy": {"role": {"references": ["aws_iam_role.node[0].id", "aws_iam_role.node[0]", "aws_iam_role.node"]}}}
    if boundary is not None:
        res.append({"type": "aws_iam_policy", "name": "boundary", "values": {"name": "b", "policy": boundary}})
        config["aws_iam_role.node"] = {"permissions_boundary": {"references": ["aws_iam_policy.boundary.arn", "aws_iam_policy.boundary"]}}
    return plan(res, config)


def run_iam(plans, aws, apply_role=None):
    rep = lp.Report()
    with mock.patch("builtins.print"):
        lp.iam_check(plans, apply_role, "ap-south-1", aws, rep)
    return rep


class IamRoleTests(unittest.TestCase):
    def test_run3_register_instance_without_health_check_rights_fails(self):
        rep = run_iam([node_plan(doc(REGISTER))], FakeAws())
        self.assertEqual([s for s, _ in rep.lines], ["FAIL"])
        text = rep.lines[0][1]
        for a in ("route53:CreateHealthCheck", "route53:GetHealthCheck", "route53:UpdateHealthCheck", "ec2:DescribeInstances"):
            self.assertIn(a, text)
        self.assertIn("onepeek-sandbox-node", text)

    def test_run3_fixed_policy_passes(self):
        rep = run_iam([node_plan(doc(REGISTER, HEALTH))], FakeAws())
        self.assertEqual(rep.lines[0][0], "PASS", rep.lines)

    def test_no_health_checked_service_needs_no_health_check_rights(self):
        rep = run_iam([node_plan(doc(REGISTER), health=False)], FakeAws())
        self.assertEqual(rep.lines[0][0], "PASS", rep.lines)

    def test_boundary_in_the_plan_that_denies_a_granted_action_fails(self):
        boundary = doc(allow("*"), {"Effect": "Deny", "Action": "servicediscovery:RegisterInstance", "Resource": "*"})
        rep = run_iam([node_plan(doc(REGISTER, HEALTH), boundary=boundary)], FakeAws())
        self.assertEqual(rep.lines[0][0], "FAIL")
        self.assertIn("servicediscovery:RegisterInstance", rep.lines[0][1])
        self.assertIn("explicitDeny", rep.lines[0][1])

    def test_live_managed_boundary_is_read_and_applied(self):
        p = node_plan(doc(REGISTER, HEALTH))
        barn = f"arn:aws:iam::{ACCT}:policy/boundary"
        p["planned_values"]["root_module"]["child_modules"][0]["resources"][0]["values"]["permissions_boundary"] = barn
        aws = FakeAws(managed={barn: doc(allow(["servicediscovery:*"]))})
        rep = run_iam([p], aws)
        self.assertEqual(rep.lines[0][0], "FAIL")
        self.assertIn("route53:CreateHealthCheck", rep.lines[0][1])
        self.assertIn(["iam", "get-policy", "--policy-arn", barn], aws.calls)

    def test_policy_known_only_after_apply_is_skipped_not_passed(self):
        rep = run_iam([node_plan(doc(REGISTER), policy_known=False)], FakeAws())
        self.assertEqual(rep.lines[0][0], "SKIP")
        self.assertIn("1 known only after apply", rep.lines[0][1])

    def test_wildcard_actions_are_not_simulated(self):
        aws = FakeAws()
        run_iam([node_plan(doc(allow("s3:Get*")), health=False)], aws)
        self.assertFalse([c for c in aws.calls if c[1] == "simulate-custom-policy"])

    def test_a_missing_condition_key_is_not_judged_and_not_failed(self):
        st = {**allow("sqs:SendMessage", "*"), "Condition": {"StringEquals": {"aws:SourceVpc": ["vpc-1"]}}}
        rep = run_iam([node_plan(doc(st), health=False)], FakeAws())
        self.assertEqual(rep.lines[0][0], "PASS")
        self.assertIn("not judged", rep.lines[0][1])
        self.assertIn("sqs:SendMessage", rep.lines[0][1])

    def test_simulation_error_fails(self):
        rep = lp.Report()
        with mock.patch("builtins.print"):
            lp.iam_check([node_plan(doc(REGISTER, HEALTH))], None, "ap-south-1", lambda a: (False, "AccessDenied"), rep)
        self.assertEqual(rep.lines[0][0], "FAIL")
        self.assertIn("AccessDenied", rep.lines[0][1])

    def test_run1_topic_policy_denies_subscribe_to_the_role_it_does_not_name(self):
        topic_policy = doc({"Sid": "OnlyCiApplySubscribes", "Effect": "Deny", "Action": "sns:Subscribe", "Resource": TOPIC_ARN,
                            "Condition": {"StringNotEquals": {"aws:PrincipalArn": [f"arn:aws:iam::{ACCT}:role/onepeek-sandbox-ci-apply"]}}})
        p = node_plan(doc(allow("sns:Subscribe", TOPIC_ARN)), health=False)
        p["planned_values"]["root_module"]["child_modules"][0]["resources"].append(
            {"mode": "managed", "address": "module.stack.module.runtime.aws_sns_topic_policy.events", "type": "aws_sns_topic_policy",
             "name": "events", "values": {"arn": TOPIC_ARN, "policy": topic_policy}})
        aws = FakeAws()
        rep = run_iam([p], aws)
        self.assertEqual(rep.lines[0][0], "FAIL", rep.lines)
        self.assertIn("sns:Subscribe", rep.lines[0][1])
        self.assertIn("with the topic policy", rep.lines[0][1])
        sim = next(c for c in aws.calls if c[1] == "simulate-custom-policy" and "--resource-policy" in c)
        self.assertEqual(sim[sim.index("--caller-arn") + 1], f"arn:aws:iam::{ACCT}:user/onepeek-live-preflight")

    def test_no_role_in_the_plan_is_a_skip(self):
        rep = run_iam([plan([])], FakeAws())
        self.assertEqual(rep.lines, [("SKIP", "iam: the plan has no IAM role")])

    def test_only_read_only_iam_calls_are_made(self):
        aws = FakeAws()
        run_iam([node_plan(doc(REGISTER))], aws)
        for c in aws.calls:
            self.assertTrue(c[1].startswith(("simulate-", "get-", "list-")), c)


CI_SANDBOX = "onepeek-sandbox-ci-sandbox"
BOUNDARY_ARN = f"arn:aws:iam::{ACCT}:policy/onepeek-sandbox-ci-sandbox-boundary"


def apply_plan(after_type, after, module="module.stack.module.ci", config=None):
    p = plan([], config=config)
    p["resource_changes"] = [{"address": f"{module}.{after_type}.x", "module_address": module, "type": after_type,
                              "change": {"actions": ["create"], "after": after}}]
    return p


class ApplyRoleTests(unittest.TestCase):
    def aws(self, boundary):
        return FakeAws(managed={BOUNDARY_ARN: boundary},
                       roles={CI_SANDBOX: {"arn": f"arn:aws:iam::{ACCT}:role/{CI_SANDBOX}", "boundary": BOUNDARY_ARN,
                                           "inline": {"apply": doc(allow(["iam:*", "sns:*"]))}}})

    def test_run1_boundary_denies_iam_writes_on_a_bootstrap_role(self):
        boundary = doc(allow("*"), {"Effect": "Deny", "Action": "iam:PutRolePolicy",
                                    "Resource": f"arn:aws:iam::{ACCT}:role/onepeek-sandbox-ci-plan"})
        rep = run_iam([apply_plan("aws_iam_role_policy", {"role": "onepeek-sandbox-ci-plan", "name": "ci-plan-guard"})],
                      self.aws(boundary), apply_role=CI_SANDBOX)
        self.assertEqual(rep.lines[-1][0], "FAIL")
        self.assertIn("iam:PutRolePolicy", rep.lines[-1][1])
        self.assertIn("role/onepeek-sandbox-ci-plan", rep.lines[-1][1])

    def test_allowed_create_calls_pass(self):
        rep = run_iam([apply_plan("aws_iam_role", {"name": "onepeek-sandbox-worker"})], self.aws(doc(allow("*"))),
                      apply_role=CI_SANDBOX)
        self.assertEqual(rep.lines[-1][0], "PASS", rep.lines)

    def test_run1_subscription_against_a_known_topic_policy(self):
        tp = doc({"Effect": "Deny", "Action": "sns:Subscribe", "Resource": TOPIC_ARN,
                  "Condition": {"StringNotEquals": {"aws:PrincipalArn": [f"arn:aws:iam::{ACCT}:role/onepeek-sandbox-ci-apply"]}}})
        conf = {"aws_sns_topic_policy.events": {"arn": {"references": ["aws_sns_topic.events.arn", "aws_sns_topic.events"]}},
                "aws_sns_topic_subscription.x": {"topic_arn": {"references": ["aws_sns_topic.events.arn"]}}}
        p = plan([], config=conf)
        mod = "module.stack.module.runtime"
        p["resource_changes"] = [
            {"address": f"{mod}.aws_sns_topic.events", "module_address": mod, "type": "aws_sns_topic",
             "change": {"actions": ["no-op"], "after": {"name": "onepeek-sandbox-events", "arn": TOPIC_ARN}}},
            {"address": f"{mod}.aws_sns_topic_policy.events", "module_address": mod, "type": "aws_sns_topic_policy",
             "change": {"actions": ["no-op"], "after": {"arn": TOPIC_ARN, "policy": tp}}},
            {"address": f"{mod}.aws_sns_topic_subscription.x", "module_address": mod, "type": "aws_sns_topic_subscription",
             "change": {"actions": ["create"], "after": {"protocol": "sqs"}}},
        ]
        rep = run_iam([p], self.aws(doc(allow("*"))), apply_role=CI_SANDBOX)
        self.assertEqual(rep.lines[-1][0], "FAIL", rep.lines)
        self.assertIn(f"sns:Subscribe on {TOPIC_ARN}", rep.lines[-1][1])

    def test_target_known_only_after_apply_is_counted(self):
        rep = run_iam([apply_plan("aws_iam_role_policy", {"name": "x"})], self.aws(doc(allow("*"))), apply_role=CI_SANDBOX)
        self.assertIn("1 target(s) known only after apply", rep.lines[-1][1])

    def test_unreadable_role_fails(self):
        rep = run_iam([plan([])], FakeAws(), apply_role="nope")
        self.assertEqual(rep.lines[-1][0], "FAIL")


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self.tmp.name)
        for img in ("gateway", "egress-proxy", "proxy"):
            (self.repo / "images" / img).mkdir(parents=True)
            (self.repo / "images" / img / "Dockerfile").write_text("FROM scratch\n")
        (self.repo / "images/ci-runner").mkdir()
        gw = self.repo / "modules/aws/discovery-gateway"
        gw.mkdir(parents=True)
        (gw / "variables.tf").write_text('variable "cloudflared_memory_mb" {\n  default = 128\n}\n\n'
                                         'variable "gateway_memory_mb" {\n  type    = number\n  default = 64\n}\n')
        ep = self.repo / "modules/aws/egress-proxy"
        ep.mkdir(parents=True)
        (ep / "main.tf").write_text('resource "aws_ecs_task_definition" "fargate" {\n  cpu    = "256"\n  memory = "512" # 0.5 GB\n}\n')

    def tearDown(self):
        self.tmp.cleanup()

    def test_limit_from_the_module_variable_default(self):
        self.assertEqual(lp.memory_limit(self.repo, "gateway", {})[0], 64)

    def test_limit_from_the_task_definition(self):
        self.assertEqual(lp.memory_limit(self.repo, "egress-proxy", {})[0], 512)

    def test_override_wins_and_unknown_image_has_no_limit(self):
        self.assertEqual(lp.memory_limit(self.repo, "gateway", {"gateway": 128}), (128, "--memory"))
        mb, why = lp.memory_limit(self.repo, "proxy", {})
        self.assertIsNone(mb)
        self.assertIn("--memory proxy=<MB>", why)

    def check(self, changed, running=(True, "28"), runner=None):
        rep = lp.Report()
        seen = []

        def fake_runner(repo, image, mb, seconds):
            seen.append((image, mb))
            return "PASS", "ok"

        with mock.patch("builtins.print"):
            lp.memory_check(self.repo, changed, {}, 5, rep, running=lambda: running, runner=runner or fake_runner)
        return rep, seen

    def test_only_changed_images_with_a_dockerfile_run(self):
        rep, seen = self.check(["images/gateway/entrypoint.sh", "images/ci-runner/actions.txt", "images/mirrors.json",
                                "modules/aws/x.tf"])
        self.assertEqual(seen, [("gateway", 64)])
        self.assertEqual(rep.lines[0][0], "PASS")

    def test_docker_not_running_is_a_skip_with_the_reason(self):
        rep, seen = self.check(["images/gateway/Dockerfile"], running=(False, "Cannot connect to the Docker daemon"))
        self.assertEqual(seen, [])
        self.assertEqual(rep.lines[0][0], "SKIP")
        self.assertIn("Docker is not running", rep.lines[0][1])

    def test_image_without_a_known_limit_is_skipped(self):
        rep, seen = self.check(["images/proxy/Caddyfile"])
        self.assertEqual((seen, rep.lines[0][0]), ([], "SKIP"))

    def test_nothing_changed_is_a_skip(self):
        rep, _ = self.check(["scripts/x.py"])
        self.assertEqual(rep.lines[0][0], "SKIP")


class FakeDocker:
    """`docker` build/run/inspect/rm answers: states is the .State sequence inspect returns."""

    def __init__(self, states, build_rc=0):
        self.states, self.build_rc, self.calls = list(states), build_rc, []

    def __call__(self, cmd, cwd=None, timeout=None):
        self.calls.append(cmd)
        if cmd[1] == "build":
            return self.build_rc, "sha256:abc" if not self.build_rc else "build error"
        if cmd[1] == "run":
            return 0, "cid123\n"
        if cmd[1] == "inspect":
            return 0, json.dumps(self.states.pop(0) if len(self.states) > 1 else self.states[0])
        return 0, ""


class RunUnderLimitTests(unittest.TestCase):
    def run_it(self, docker, clock=None):
        t = iter(range(0, 1000))
        return lp.run_under_limit(pathlib.Path("/repo"), "gateway", 64, 3, run=docker,
                                  clock=clock or (lambda: next(t)), sleep=lambda _s: None)

    def test_run3_oom_killed_gateway_fails(self):
        d = FakeDocker([{"Running": True}, {"Running": False, "OOMKilled": True, "ExitCode": 137}])
        status, detail = self.run_it(d)
        self.assertEqual(status, "FAIL")
        self.assertIn("OOM-killed under its 64 MB limit", detail)
        run = next(c for c in d.calls if c[1] == "run")
        self.assertEqual(run[run.index("--memory") + 1], "64m")
        self.assertEqual(run[run.index("--memory-swap") + 1], "64m")
        self.assertEqual(run[run.index("--network") + 1], "none")
        self.assertIn(["docker", "rm", "-f", "cid123"], d.calls)

    def test_still_running_at_the_end_passes(self):
        d = FakeDocker([{"Running": True}])
        self.assertEqual(self.run_it(d)[0], "PASS")
        self.assertIn(["docker", "rm", "-f", "cid123"], d.calls)

    def test_exit_without_oom_passes(self):
        self.assertEqual(self.run_it(FakeDocker([{"Running": False, "OOMKilled": False, "ExitCode": 1}]))[0], "PASS")

    def test_build_failure_fails(self):
        status, detail = self.run_it(FakeDocker([{}], build_rc=1))
        self.assertEqual(status, "FAIL")
        self.assertIn("docker build failed", detail)


class EdgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self.tmp.name)
        (self.repo / "config").mkdir()
        (self.repo / "config/sandbox.yaml").write_text("domain: sandbox.onepeek.xyz\nregion: ap-south-1\n")

    def tearDown(self):
        self.tmp.cleanup()

    def edge(self, changed, status_of):
        rep = lp.Report()
        with mock.patch("builtins.print"):
            lp.edge_check(self.repo, changed, "sandbox", rep, status_of=status_of)
        return rep.lines

    def test_run3_403_at_the_edge_fails(self):
        seen = []
        lines = self.edge([lp.SMOKE_ENV], lambda url: seen.append(url) or 403)
        self.assertEqual(lines[0][0], "FAIL")
        self.assertEqual(seen, ["https://sandbox.onepeek.xyz/api/status"])

    def test_non_403_passes(self):
        self.assertEqual(self.edge([lp.SMOKE_ENV], lambda url: 503)[0][0], "PASS")

    def test_unchanged_smoke_env_is_a_skip(self):
        self.assertEqual(self.edge(["scripts/other.py"], lambda url: 403)[0][0], "SKIP")

    def test_unreachable_is_a_skip_with_the_reason(self):
        def boom(url):
            raise OSError("no route")
        lines = self.edge([lp.SMOKE_ENV], boom)
        self.assertEqual(lines[0][0], "SKIP")
        self.assertIn("no route", lines[0][1])

    def test_uses_the_repos_own_http_status(self):
        (self.repo / "scripts").mkdir()
        (self.repo / lp.SMOKE_ENV).write_text("SEEN = []\ndef http_status(url):\n    SEEN.append(url)\n    return 403\n")
        rep = lp.Report()
        with mock.patch("builtins.print"):
            lp.edge_check(self.repo, [lp.SMOKE_ENV], "sandbox", rep)
        self.assertEqual(rep.lines[0][0], "FAIL")


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self.tmp.name)
        git = lambda *a: subprocess.run(["git", "-C", str(self.repo), *a], check=True, capture_output=True)  # noqa: E731
        git("init", "-q", "-b", "dev")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        (self.repo / "config").mkdir()
        (self.repo / "config/sandbox.yaml").write_text("domain: sandbox.onepeek.xyz\nregion: ap-south-1\n")
        git("add", ".")
        git("commit", "-q", "-m", "base")
        git("checkout", "-q", "-b", "feat")
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts/new.py").write_text("x = 1\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_changed_files_include_untracked(self):
        files, _ = lp.changed_files(self.repo, "dev")
        self.assertEqual(files, ["scripts/new.py"])

    def test_bad_base_fails(self):
        out = []
        with mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(a[0])):
            rc = lp.main([str(self.repo), "--base", "no-such-ref"])
        self.assertEqual(rc, 1)
        self.assertTrue(out[-1].startswith("LIVE PREFLIGHT FAILED"))

    def test_nothing_to_check_is_ok_and_iam_without_a_plan_skips(self):
        out = []
        with mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(a[0])):
            rc = lp.main([str(self.repo), "--base", "dev"])
        self.assertEqual(rc, 0)
        self.assertEqual(out[-1], "LIVE PREFLIGHT OK")
        self.assertTrue(any(o.startswith("SKIP") and "iam: no plan" in o for o in out))

    def test_a_fail_exits_1(self):
        p = self.repo / "plan.json"
        p.write_text(json.dumps(node_plan(doc(REGISTER))))
        out = []
        with mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(a[0])):
            rc = lp.main([str(self.repo), "--base", "dev", "--profile", "sandbox", "--plan-json", str(p),
                          "--only", "iam"], aws_factory=lambda profile, region: FakeAws())
        self.assertEqual(rc, 1)
        self.assertEqual(out[-1], "LIVE PREFLIGHT FAILED (1 failed)")

    def test_cli_passes_the_profile_and_region(self):
        with mock.patch.object(lp, "sh", return_value=(0, "{}")) as sh:
            lp.cli("sandbox-ro", "ap-south-1")(["iam", "get-role", "--role-name", "r"])
        cmd = sh.call_args[0][0]
        self.assertEqual(cmd[:4], ["aws", "iam", "get-role", "--role-name"])
        self.assertEqual(cmd[cmd.index("--profile") + 1], "sandbox-ro")
        self.assertEqual(cmd[cmd.index("--region") + 1], "ap-south-1")


class PreflightLiveTests(unittest.TestCase):
    def test_live_args_forward_the_options(self):
        a = mock.Mock(profile="p", plan=None, apply_role="r", plan_json=["a.json", "b.json"])
        self.assertEqual(pf.live_args(a, pathlib.Path("/infra")),
                         ["/infra", "--profile", "p", "--apply-role", "r", "--plan-json", "a.json", "--plan-json", "b.json"])

    def run_live(self, rc, stdout):
        rep = pf.Report()
        done = subprocess.CompletedProcess([], rc, stdout, "")
        with mock.patch.object(pf.subprocess, "run", return_value=done), mock.patch("builtins.print"):
            pf.run_live(["/infra"], rep)
        return rep.lines

    def test_live_ok_passes(self):
        self.assertEqual(self.run_live(0, "PASS  x\nLIVE PREFLIGHT OK\n"), [("PASS", "live: LIVE PREFLIGHT OK")])

    def test_live_failed_fails(self):
        lines = self.run_live(1, "FAIL  x\nLIVE PREFLIGHT FAILED (1 failed)\n")
        self.assertEqual(lines[0][0], "FAIL")
        self.assertIn("LIVE PREFLIGHT FAILED", lines[0][1])


if __name__ == "__main__":
    unittest.main()
