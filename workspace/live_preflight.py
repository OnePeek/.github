#!/usr/bin/env python3
"""Live preflight for onepeek-infra changes (onepeek-docs architecture/lld/infra/foundation-playbook.md §8a P9).

    python3 .github/workspace/live_preflight.py <infra dir> [--profile P] [--plan-json F ...] [--plan F]
        [--apply-role NAME] [--env sandbox] [--base origin/dev] [--memory IMAGE=MB ...] [--seconds 20]
        [--only iam,memory,edge]

The checks that only a live sandbox run used to find, run on the laptop in minutes. Read-only on AWS, nothing applied.

iam     Needs `--profile` and a plan: `--plan-json` (the output of `terraform show -json <plan>`, repeatable, for
        example one of envs/sandbox and one of bootstrap) or `--plan` (a plan file, shown with `terraform show -json`
        from `--plan-dir`, default envs/sandbox). A real plan needs the backend's credentials, so this tool never
        plans itself. For every IAM role in the plan, its policies (inline, attached in the plan, or AWS-managed and
        read live) and its permissions boundary go to `aws iam simulate-custom-policy` with:
          - each Allow statement's own actions on its own resources (wildcard actions are not simulated);
          - the dependent actions of DEPENDENT (an AWS call that runs other calls with the caller's rights);
          - for SNS actions on a topic whose policy the plan knows, that topic policy as the resource policy.
        With `--apply-role`, the role's live policies and boundary (read with iam get-role / get-role-policy /
        get-policy-version) are simulated against the calls APPLY_ACTIONS says Terraform makes to create the plan's
        resources. Every simulation gets `aws:RequestedRegion`: the resource ARN's region, else the config region
        (config/<env>.yaml must name one). An implicit deny that depends on a condition key the simulation was not
        given is not judged (named); an explicit deny always fails. A role whose policies or boundary cannot be read,
        or are known only after apply, fails. A policy Terraform knows only after apply cannot be simulated (counted
        in the line); NotAction and NotResource statements are not simulated (named in the line).
        simulate-custom-policy does not evaluate SCPs: an SCP deny (the region SCP, the launch guard) is not checked.
memory  For each images/<name> with a Dockerfile whose files changed against `--base`: `docker build`, then
        `docker run` with `--memory` (and `--memory-swap`) at the task definition's hard limit for `--seconds`, with a
        stub environment and no network. The limit is read from the plan's aws_ecs_task_definition (the container's
        `memory`, else the task's) when a plan is given; with no plan, from the module defaults (MEMORY_LIMITS), said
        in the line; `--memory <name>=<MB>` overrides. FAIL when `docker inspect` shows `.State.OOMKilled` or exit
        code 137 (a suspected OOM kill). SKIP when Docker is not running or the limit is unknown.
edge    When scripts/smoke_env.py changed: its own `http_status` against https://<config domain>/api/status (the smoke
        check's URL and User-Agent); FAIL on 403.

One PASS, FAIL or SKIP line per check (and NOTE lines), then `LIVE PREFLIGHT OK` or `LIVE PREFLIGHT FAILED`. Exit 1 on
any FAIL. preflight.py --live shows WARN, not PASS, when any check was skipped, and lists them.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import re
import subprocess
import sys
import time
from collections.abc import Callable

import yaml

CHECKS = ("iam", "memory", "edge")
SMOKE_ENV = "scripts/smoke_env.py"
STUB_CALLER = "user/onepeek-live-preflight"  # simulate-custom-policy needs a user ARN as caller for a resource policy

# Calls AWS runs with the caller's rights after the call it was given. `if` names a plan fact that must hold.
DEPENDENT = (
    {"when": "servicediscovery:RegisterInstance", "if": "health_checked_service",
     "needs": ("route53:CreateHealthCheck", "route53:GetHealthCheck", "route53:UpdateHealthCheck", "ec2:DescribeInstances"),
     "why": "Cloud Map creates the instance's Route 53 health check on a health-checked service"},
    {"when": "servicediscovery:DeregisterInstance", "if": "health_checked_service",
     "needs": ("route53:GetHealthCheck", "route53:UpdateHealthCheck", "route53:DeleteHealthCheck", "ec2:DescribeInstances"),
     "why": "Cloud Map deletes the instance's Route 53 health check on a health-checked service"},
)

# The call Terraform makes to create a resource of a type, and where its target's name is in the plan.
APPLY_ACTIONS = {
    "aws_iam_role": ("iam:CreateRole", "arn:{p}:iam::{a}:role/{name}", "name"),
    "aws_iam_role_policy": ("iam:PutRolePolicy", "arn:{p}:iam::{a}:role/{name}", "role"),
    "aws_iam_role_policy_attachment": ("iam:AttachRolePolicy", "arn:{p}:iam::{a}:role/{name}", "role"),
    "aws_iam_policy": ("iam:CreatePolicy", "arn:{p}:iam::{a}:policy/{name}", "name"),
    "aws_sns_topic": ("sns:CreateTopic", "arn:{p}:sns:{r}:{a}:{name}", "name"),
    "aws_sns_topic_subscription": ("sns:Subscribe", "{name}", "topic_arn"),
}

# With no plan only: images/<name> -> (file, variable), the module default of the hard memory limit (MB) of the
# container its task definition runs. A variable of None reads the `memory` of the file's aws_ecs_task_definition.
MEMORY_LIMITS = {
    "gateway": ("modules/aws/discovery-gateway/variables.tf", "gateway_memory_mb"),
    "pgbouncer": ("modules/aws/postgres/variables.tf", "pgbouncer_memory_mb"),
    "skeleton": ("modules/aws/runtime/variables.tf", "skeleton_memory_mb"),
    "egress-proxy": ("modules/aws/egress-proxy/main.tf", None),
}
STUB_ENV = {"ONEPEEK_ENV": "sandbox", "AWS_REGION": "ap-south-1", "AWS_DEFAULT_REGION": "ap-south-1",
            "AWS_ACCESS_KEY_ID": "stub", "AWS_SECRET_ACCESS_KEY": "stub", "AWS_EC2_METADATA_DISABLED": "true"}


class Report:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def add(self, status: str, text: str) -> None:
        self.lines.append((status, text))
        print(f"{status:<5} {text}", flush=True)

    def failed(self) -> int:
        return sum(1 for s, _ in self.lines if s == "FAIL")


def sh(cmd: list[str], cwd: pathlib.Path | None = None, timeout: float | None = None) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, f"{cmd[0]} is not installed"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]} timed out"
    return r.returncode, (r.stdout if r.returncode == 0 else (r.stderr or r.stdout)).strip()


def cli(profile: str, region: str) -> Callable[[list[str]], tuple[bool, object]]:
    """The AWS CLI with the profile, as JSON: (True, data) or (False, the error). Only read-only calls are made."""
    def call(args: list[str]) -> tuple[bool, object]:
        rc, out = sh(["aws", *args, "--profile", profile, "--region", region, "--output", "json", "--no-cli-pager"])
        if rc:
            return False, out[:400]
        try:
            return True, json.loads(out or "null")
        except json.JSONDecodeError:
            return False, out[:400]
    return call


# Changed files ----------------------------------------------------------------------------------------------------
def changed_files(repo: pathlib.Path, base: str) -> tuple[list[str] | None, str]:
    """Files changed against base (committed, staged, unstaged, untracked), or (None, the error)."""
    rc, committed = sh(["git", "-C", str(repo), "diff", "--name-only", f"{base}...HEAD"])
    if rc:
        return None, committed
    _, local = sh(["git", "-C", str(repo), "diff", "--name-only", "HEAD"])
    _, untracked = sh(["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard"])
    return sorted({f for f in (committed + "\n" + local + "\n" + untracked).splitlines() if f.strip()}), ""


# iam: the plan ------------------------------------------------------------------------------------------------------
def strip_index(address: str) -> str:
    return re.sub(r"\[[^\]]*\]", "", address)


def planned(plan: dict) -> list[dict]:
    """Every planned resource, with `module` (its module address) and `key` (its address without indexes)."""
    out: list[dict] = []

    def walk(mod: dict) -> None:
        for r in mod.get("resources") or []:
            if r.get("mode", "managed") == "managed":
                prefix = r["address"][: r["address"].rfind(r["type"] + ".")].rstrip(".")
                out.append({**r, "module": strip_index(prefix), "key": strip_index(r["address"])})
        for c in mod.get("child_modules") or []:
            walk(c)

    walk((plan.get("planned_values") or {}).get("root_module") or {})
    return out


def configuration(plan: dict) -> dict[str, dict]:
    """Resource address without indexes -> its configuration expressions."""
    out: dict[str, dict] = {}

    def walk(mod: dict, prefix: str) -> None:
        for r in mod.get("resources") or []:
            out[f"{prefix}{r['address']}"] = r.get("expressions") or {}
        for name, call in (mod.get("module_calls") or {}).items():
            walk(call.get("module") or {}, f"{prefix}module.{name}.")

    walk((plan.get("configuration") or {}).get("root_module") or {}, "")
    return out


def ref(expressions: dict, attr: str, module: str, rtype: str) -> str | None:
    """The key of the `rtype` resource an attribute references inside its own module, if any."""
    for r in (expressions.get(attr) or {}).get("references") or []:
        m = re.match(rf"({rtype}\.[\w-]+)", strip_index(r))
        if m:
            return f"{module}.{m.group(1)}" if module else m.group(1)
    return None


def statements(doc: str | dict) -> list[dict]:
    d = json.loads(doc) if isinstance(doc, str) else doc
    st = d.get("Statement") or []
    return [st] if isinstance(st, dict) else list(st)


def as_list(v) -> list[str]:
    return [v] if isinstance(v, str) else list(v or [])


def health_checked_service(resources: list[dict]) -> bool:
    return any(r["type"] == "aws_service_discovery_service" and ((r.get("values") or {}).get("health_check_custom_config")
                                                                 or (r.get("values") or {}).get("health_check_config"))
               for r in resources)


def managed_policy(arn: str, aws, cache: dict) -> str | None:
    """A managed policy's default version, read live (read-only)."""
    if arn not in cache:
        ok, pol = aws(["iam", "get-policy", "--policy-arn", arn])
        doc = None
        if ok:
            ok, ver = aws(["iam", "get-policy-version", "--policy-arn", arn, "--version-id", pol["Policy"]["DefaultVersionId"]])
            if ok:
                d = ver["PolicyVersion"]["Document"]
                doc = d if isinstance(d, str) else json.dumps(d)
        cache[arn] = doc
    return cache[arn]


def roles_in_plan(plan: dict, aws, cache: dict) -> list[dict]:
    """Each planned role: name, arn, its known policy documents, how many are known only after apply, its boundary."""
    res, conf = planned(plan), configuration(plan)
    by_key = {r["key"]: r for r in res}
    roles: dict[str, dict] = {}
    for r in res:
        if r["type"] != "aws_iam_role":
            continue
        v = r.get("values") or {}
        roles.setdefault(r["key"], {"name": v.get("name") or r["key"], "arn": v.get("arn"), "policies": [],
                                    "unknown": 0, "boundary": None, "boundary_unknown": False})
        role = roles[r["key"]]
        role["policies"] += [ip["policy"] for ip in v.get("inline_policy") or [] if ip.get("policy")]
        b = v.get("permissions_boundary")
        bkey = ref(conf.get(r["key"], {}), "permissions_boundary", r["module"], "aws_iam_policy")
        if bkey and by_key.get(bkey, {}).get("values", {}).get("policy"):
            role["boundary"] = by_key[bkey]["values"]["policy"]
        elif b:
            role["boundary"] = managed_policy(b, aws, cache)
            role["boundary_unknown"] = role["boundary"] is None
        elif bkey or "permissions_boundary" in (conf.get(r["key"], {})):
            role["boundary_unknown"] = True
    by_name = {role["name"]: k for k, role in roles.items()}

    def owner(r: dict) -> dict | None:
        k = ref(conf.get(r["key"], {}), "role", r["module"], "aws_iam_role")
        k = k or by_name.get((r.get("values") or {}).get("role"))
        return roles.get(k) if k else None

    for r in res:
        role = owner(r) if r["type"] in ("aws_iam_role_policy", "aws_iam_role_policy_attachment") else None
        if role is None:
            continue
        v = r.get("values") or {}
        if r["type"] == "aws_iam_role_policy":
            doc = v.get("policy")
        else:
            pkey = ref(conf.get(r["key"], {}), "policy_arn", r["module"], "aws_iam_policy")
            doc = (by_key.get(pkey, {}).get("values") or {}).get("policy") if pkey else None
            if doc is None and v.get("policy_arn"):
                doc = managed_policy(v["policy_arn"], aws, cache)
        if doc:
            role["policies"].append(doc)
        else:
            role["unknown"] += 1
    return list(roles.values())


def topic_policies(plan: dict) -> dict[str, str]:
    """Topic ARN -> its topic policy, where the plan knows both."""
    return {v["arn"]: v["policy"] for r in planned(plan) if r["type"] == "aws_sns_topic_policy"
            for v in [r.get("values") or {}] if v.get("arn") and v.get("policy")}


def pairs(role: dict, plan_facts: dict, skipped: list[str] | None = None) -> list[tuple[tuple[str, ...], tuple[str, ...], str]]:
    """(actions, resources, why) to simulate: each Allow statement's own, then the dependent actions they imply.
    An Allow with NotAction or NotResource cannot be listed as pairs: it is named in `skipped`, not simulated."""
    out, granted = [], set()
    for doc in role["policies"]:
        for st in statements(doc):
            if st.get("Effect") != "Allow":
                continue
            if "NotAction" in st or "NotResource" in st:
                if skipped is not None:
                    skipped.append(f"statement {st.get('Sid') or '(no Sid)'} ({'NotAction' if 'NotAction' in st else 'NotResource'})")
                continue
            acts = tuple(a for a in as_list(st.get("Action")) if "*" not in a)
            if acts:
                out.append((acts, tuple(as_list(st.get("Resource")) or ["*"]), f"statement {st.get('Sid') or '(no Sid)'}"))
                granted |= set(acts)
    for dep in DEPENDENT:
        if dep["when"] in granted and plan_facts.get(dep["if"]):
            out.append((dep["needs"], ("*",), f"{dep['when']}: {dep['why']}"))
    return out


def simulate(aws, policies: list[str], actions, resources, boundary: str | None = None,
             resource_policy: str | None = None, caller: str | None = None,
             context: list[dict] | None = None) -> tuple[bool, object]:
    args = ["iam", "simulate-custom-policy", "--policy-input-list", *policies,
            "--action-names", *actions, "--resource-arns", *resources]
    if boundary:
        args += ["--permissions-boundary-policy-input-list", boundary]
    if resource_policy:
        args += ["--resource-policy", resource_policy, "--caller-arn", caller]
    if context:
        args += ["--context-entries", json.dumps(context)]
    ok, data = aws(args)
    return (True, data.get("EvaluationResults") or []) if ok else (False, data)


def judge(results: list[dict], why: str, denied: list[str], unjudged: list[str]) -> None:
    """Only an implicit deny that lacks a condition key is not judged; an explicit deny is always denied."""
    for e in results:
        if e.get("EvalDecision") == "allowed":
            continue
        line = f"{e['EvalActionName']} on {e.get('EvalResourceName', '*')} ({e['EvalDecision']}; {why})"
        (unjudged if e.get("EvalDecision") == "implicitDeny" and e.get("MissingContextValues") else denied).append(line)


def region_of(arn: str, region: str) -> str:
    """The region an ARN names, else the config region (a global resource, `*`)."""
    parts = arn.split(":")
    return parts[3] if len(parts) > 4 and parts[0] == "arn" and parts[3] else region


def context(region: str, principal: str | None = None) -> list[dict]:
    ctx = [{"ContextKeyName": "aws:RequestedRegion", "ContextKeyValues": [region], "ContextKeyType": "string"}]
    if principal:
        ctx.append({"ContextKeyName": "aws:PrincipalArn", "ContextKeyValues": [principal], "ContextKeyType": "string"})
    return ctx


def account_of(arn: str | None) -> tuple[str, str] | None:
    m = re.match(r"arn:([\w-]+):iam::(\d{12}):", arn or "")
    return (m.group(1), m.group(2)) if m else None


def role_check(role: dict, plan_facts: dict, topics: dict[str, str], region: str, aws, rep: Report) -> None:
    label = f"iam: role {role['name']}"
    if not role["policies"]:
        rep.add("SKIP", f"{label}: no policy known at plan ({role['unknown']} known only after apply)")
        return
    if role["boundary_unknown"]:
        rep.add("FAIL", f"{label}: its permissions boundary is known only after apply or cannot be read, so its "
                        "rights cannot be judged")
        return
    denied, unjudged, errors, skipped = [], [], [], []
    acct = account_of(role.get("arn"))
    for acts, resources, why in pairs(role, plan_facts, skipped):
        sns_res = [r for r in resources if r in topics] if any(a.startswith("sns:") for a in acts) else []
        by_region: dict[str, list[str]] = {}
        for r in resources:
            if r not in sns_res:
                by_region.setdefault(region_of(r, region), []).append(r)
        for reg, plain in by_region.items():
            ok, res = simulate(aws, role["policies"], acts, plain, role["boundary"], context=context(reg))
            if ok:
                judge(res, why, denied, unjudged)
            else:
                errors.append(f"{why}: {res}")
        for topic in sns_res:
            ctx = context(region_of(topic, region), role["arn"])
            caller = f"arn:{acct[0]}:iam::{acct[1]}:{STUB_CALLER}" if acct else f"arn:aws:iam::{topic.split(':')[4]}:{STUB_CALLER}"
            ok, res = simulate(aws, role["policies"], acts, [topic], role["boundary"], topics[topic], caller, ctx)
            if ok:
                judge(res, why + ", with the topic policy", denied, unjudged)
            else:
                errors.append(f"{why}: {res}")
    tail = (f"; {role['unknown']} policy(ies) known only after apply" if role["unknown"] else "") + \
           (f"; not simulated: {'; '.join(skipped)}" if skipped else "") + \
           (f"; not judged (a condition key the simulation lacks): {'; '.join(unjudged)}" if unjudged else "")
    if denied or errors:
        rep.add("FAIL", f"{label}: denied: {'; '.join(denied + errors)}{tail}")
    else:
        rep.add("PASS", f"{label}: every granted and dependent action is allowed{tail}")


def apply_role_check(plans: list[dict], role_name: str, region: str, aws, rep: Report, cache: dict) -> None:
    """The live role that applies the plan, against the calls that create the plan's resources."""
    label = f"iam: apply role {role_name}"
    ok, got = aws(["iam", "get-role", "--role-name", role_name])
    if not ok:
        rep.add("FAIL", f"{label}: cannot read it: {got}")
        return
    role = got["Role"]
    part, acct = account_of(role["Arn"])
    # Every policy must be read: one left out (a deny guard such as ci-stop-guard) would give a false PASS.
    policies: list[str] = []
    unreadable: list[str] = []
    ok, names = aws(["iam", "list-role-policies", "--role-name", role_name])
    if not ok:
        unreadable.append(f"list-role-policies: {names}")
    for n in (names or {}).get("PolicyNames", []) if ok else []:
        ok2, p = aws(["iam", "get-role-policy", "--role-name", role_name, "--policy-name", n])
        if ok2:
            d = p["PolicyDocument"]
            policies.append(d if isinstance(d, str) else json.dumps(d))
        else:
            unreadable.append(f"inline policy {n}: {p}")
    ok, att = aws(["iam", "list-attached-role-policies", "--role-name", role_name])
    if not ok:
        unreadable.append(f"list-attached-role-policies: {att}")
    for a in (att or {}).get("AttachedPolicies", []) if ok else []:
        doc = managed_policy(a["PolicyArn"], aws, cache)
        if doc:
            policies.append(doc)
        else:
            unreadable.append(f"attached policy {a['PolicyArn']}")
    barn = (role.get("PermissionsBoundary") or {}).get("PermissionsBoundaryArn")
    boundary = managed_policy(barn, aws, cache) if barn else None
    if barn and boundary is None:
        unreadable.append(f"boundary {barn}")
    if unreadable or not policies:
        rep.add("FAIL", f"{label}: cannot read its policies or boundary: {'; '.join(unreadable) or 'no policy'}")
        return
    denied, unjudged, unknown = [], [], 0
    for plan in plans:
        conf = configuration(plan)
        changes = [c for c in plan.get("resource_changes") or [] if "create" in (c.get("change") or {}).get("actions", [])]
        after = {strip_index(c["address"]): (c["change"].get("after") or {}) for c in plan.get("resource_changes") or []}
        tpol = {}
        for c in plan.get("resource_changes") or []:
            if c["type"] == "aws_sns_topic_policy":
                mod = strip_index(c.get("module_address", ""))
                k = ref(conf.get(strip_index(c["address"]), {}), "arn", mod, "aws_sns_topic")
                tpol[k] = (c["change"].get("after") or {}).get("policy")
        for c in changes:
            spec = APPLY_ACTIONS.get(c["type"])
            if not spec:
                continue
            action, tmpl, attr = spec
            a = c["change"].get("after") or {}
            target, rp = a.get(attr), None
            if c["type"] == "aws_sns_topic_subscription":
                mod = strip_index(c.get("module_address", ""))
                tkey = ref(conf.get(strip_index(c["address"]), {}), "topic_arn", mod, "aws_sns_topic")
                if not target and tkey and after.get(tkey, {}).get("name"):
                    treg = after[tkey].get("region") or region  # the topic's own region (a us-east-1 alerts topic)
                    target = f"arn:{part}:sns:{treg}:{acct}:{after[tkey]['name']}"
                rp = tpol.get(tkey)
                if tkey in tpol and rp is None:
                    unjudged.append(f"{action} on {target}: its topic policy is known only after apply")
            if not target:
                unknown += 1
                continue
            arn = tmpl.format(p=part, a=acct, r=a.get("region") or region, name=target)  # a topic in its own region
            ok, res = simulate(aws, policies, [action], [arn], boundary, rp,
                               f"arn:{part}:iam::{acct}:{STUB_CALLER}" if rp else None,
                               context(region_of(arn, region), role["Arn"]))
            if ok:
                judge(res, f"creates {c['address']}", denied, unjudged)
            else:
                denied.append(f"{action} on {arn}: {res}")
    tail = (f"; {unknown} target(s) known only after apply" if unknown else "") + \
           (f"; not judged: {'; '.join(unjudged)}" if unjudged else "")
    rep.add("FAIL" if denied else "PASS",
            f"{label}: " + (f"denied: {'; '.join(denied)}" if denied else "may make every create call in the plan") + tail)


def load_plans(repo: pathlib.Path, plan_json: list[str], plan: str | None, plan_dir: str) -> tuple[list[dict], str]:
    plans = []
    for p in plan_json:
        plans.append(json.loads(pathlib.Path(p).read_text(encoding="utf-8")))
    if plan:
        rc, out = sh(["terraform", f"-chdir={repo / plan_dir}", "show", "-json", str(pathlib.Path(plan).resolve())])
        if rc:
            return [], f"terraform show -json {plan}: {out[:300]}"
        plans.append(json.loads(out))
    return plans, ""


SCP_NOTE = ("iam: simulate-custom-policy does not evaluate SCPs: an SCP deny (the region SCP, the launch guard) is "
            "not checked here")


def iam_check(plans: list[dict], apply_role: str | None, region: str, aws, rep: Report) -> None:
    cache: dict = {}
    rep.add("NOTE", SCP_NOTE)
    for plan in plans:
        res = planned(plan)
        facts = {"health_checked_service": health_checked_service(res)}
        topics = topic_policies(plan)
        roles = roles_in_plan(plan, aws, cache)
        if not roles:
            rep.add("SKIP", "iam: the plan has no IAM role")
        for role in roles:
            role_check(role, facts, topics, region, aws, rep)
    if apply_role:
        apply_role_check(plans, apply_role, region, aws, rep, cache)


# memory -----------------------------------------------------------------------------------------------------------
def plan_memory_limit(plans: list[dict], image: str) -> tuple[int | None, str]:
    """The hard limit (MB) of the planned container that runs images/<image>: matched by container name or by the
    image's repository name; the container's `memory`, else its task definition's `memory`."""
    found = []
    for plan in plans:
        for r in planned(plan):
            if r["type"] != "aws_ecs_task_definition":
                continue
            v = r.get("values") or {}
            try:
                containers = json.loads(v.get("container_definitions") or "[]")
            except (TypeError, json.JSONDecodeError):
                continue
            for c in containers:
                repo_name = re.split(r"[:@]", str(c.get("image") or "").rsplit("/", 1)[-1])[0]
                if image not in (c.get("name"), repo_name):
                    continue
                mb = c.get("memory") or v.get("memory")
                if mb is not None and str(mb).isdigit():
                    found.append((int(mb), f"plan {r['address']} container {c.get('name')}"))
    if not found:
        return None, f"no task definition in the plan runs images/{image}; pass --memory {image}=<MB>"
    return min(found)


def memory_limit(repo: pathlib.Path, image: str, overrides: dict[str, int],
                 plans: list[dict] | None = None) -> tuple[int | None, str]:
    if image in overrides:
        return overrides[image], "--memory"
    if plans:
        return plan_memory_limit(plans, image)
    if image not in MEMORY_LIMITS:
        return None, f"no task definition limit is known for images/{image}; pass --memory {image}=<MB>"
    path, var = MEMORY_LIMITS[image]
    f = repo / path
    text = f.read_text(encoding="utf-8") if f.exists() else ""
    if var:
        block = re.search(rf'variable "{var}" \{{(.*?)\n\}}', text, re.S)
        m = re.search(r"default\s*=\s*(\d+)", block.group(1)) if block else None
        where = f"no plan: module default, {path} {var}"
    else:
        block = re.search(r'resource "aws_ecs_task_definition"[^{]*\{(.*?)\n\}', text, re.S)
        m = re.search(r'\bmemory\s*=\s*"?(\d+)"?', block.group(1)) if block else None
        where = f"no plan: module default, {path} task definition memory"
    return (int(m.group(1)), where) if m else (None, f"cannot read the limit from {where}")


def docker_running() -> tuple[bool, str]:
    rc, out = sh(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=20)
    return rc == 0, out


def run_under_limit(repo: pathlib.Path, image: str, mb: int, seconds: float,
                    run=sh, clock=time.monotonic, sleep=time.sleep) -> tuple[str, str]:
    """(status, detail): build images/<image>, run it under the limit, FAIL when it is OOM-killed."""
    tag = f"onepeek-live-preflight/{image}:local"
    rc, out = run(["docker", "build", "-q", "-t", tag, str(repo / "images" / image)])
    if rc:
        return "FAIL", f"docker build failed: {out[-300:]}"
    env = [x for k, v in STUB_ENV.items() for x in ("-e", f"{k}={v}")]
    rc, cid = run(["docker", "run", "-d", "--network", "none", "--memory", f"{mb}m", "--memory-swap", f"{mb}m", *env, tag])
    if rc:
        return "FAIL", f"docker run failed: {cid[-300:]}"
    cid = cid.strip().splitlines()[-1]
    try:
        end = clock() + seconds
        while True:
            rc, out = run(["docker", "inspect", "--format", "{{json .State}}", cid])
            if rc:
                return "FAIL", f"docker inspect failed: {out[-300:]}"
            state = json.loads(out)
            if state.get("OOMKilled"):
                return "FAIL", f"OOM-killed under its {mb} MB limit (exit {state.get('ExitCode')})"
            if not state.get("Running") and state.get("ExitCode") == 137:
                # Some cgroup v2 setups kill without setting OOMKilled: 137 (SIGKILL) under a limit is a suspected OOM.
                return "FAIL", f"exit 137 under its {mb} MB limit: a suspected OOM kill"
            if not state.get("Running"):
                return "PASS", f"exited {state.get('ExitCode')} under {mb} MB without an OOM kill (stub environment)"
            if clock() >= end:
                return "PASS", f"ran {seconds:g} s under {mb} MB without an OOM kill"
            sleep(1)
    finally:
        run(["docker", "rm", "-f", cid])


def memory_check(repo: pathlib.Path, changed: list[str], overrides: dict[str, int], seconds: float, rep: Report,
                 running=docker_running, runner=run_under_limit, plans: list[dict] | None = None) -> None:
    images = sorted({f.split("/")[1] for f in changed if f.startswith("images/") and f.count("/") >= 2
                     and (repo / "images" / f.split("/")[1] / "Dockerfile").exists()})
    if not images:
        rep.add("SKIP", "memory: no image with a Dockerfile changed")
        return
    up, why = running()
    if not up:
        rep.add("SKIP", f"memory: Docker is not running ({why[:200]}); images not checked: {', '.join(images)}")
        return
    for image in images:
        mb, where = memory_limit(repo, image, overrides, plans)
        if mb is None:
            rep.add("SKIP", f"memory: images/{image}: {where}")
            continue
        status, detail = runner(repo, image, mb, seconds)
        rep.add(status, f"memory: images/{image} ({where}): {detail}")


# edge -------------------------------------------------------------------------------------------------------------
def edge_check(repo: pathlib.Path, changed: list[str], env: str, rep: Report, status_of=None) -> None:
    if SMOKE_ENV not in changed:
        rep.add("SKIP", f"edge: {SMOKE_ENV} did not change")
        return
    path = repo / "config" / f"{env}.yaml"
    config = (yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None) or {}
    if not config.get("domain"):
        rep.add("FAIL", f"edge: config/{env}.yaml is missing or names no domain")
        return
    url = f"https://{config['domain']}/api/status"
    if status_of is None:
        spec = importlib.util.spec_from_file_location("smoke_env", repo / SMOKE_ENV)
        mod = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(repo / "scripts"))
        spec.loader.exec_module(mod)
        status_of = mod.http_status
    try:
        code = status_of(url)
    except Exception as e:  # noqa: BLE001 - an unreachable edge is reported, not raised
        rep.add("SKIP", f"edge: {url} unreachable from this laptop: {e}")
        return
    rep.add("FAIL" if code == 403 else "PASS",
            f"edge: {url} answered {code} to smoke_env's http_status" + (" (refused at the edge)" if code == 403 else ""))


# main -------------------------------------------------------------------------------------------------------------
def parse_memory(items: list[str]) -> dict[str, int]:
    out = {}
    for it in items:
        name, _, mb = it.partition("=")
        if not name or not mb.isdigit():
            raise SystemExit(f"--memory {it}: expected <image>=<MB>")
        out[name] = int(mb)
    return out


def main(argv: list[str] | None = None, aws_factory=cli) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("repo", help="the onepeek-infra worktree")
    ap.add_argument("--profile", help="the AWS profile for the read-only IAM calls")
    ap.add_argument("--plan-json", action="append", default=[], help="terraform show -json output (repeatable)")
    ap.add_argument("--plan", help="a plan file (terraform plan -out), shown from --plan-dir")
    ap.add_argument("--plan-dir", default="envs/sandbox")
    ap.add_argument("--apply-role", help="the IAM role name that applies the plan (its live policies are read)")
    ap.add_argument("--env", default="sandbox", help="config/<env>.yaml gives the region and the edge's domain")
    ap.add_argument("--base", default="origin/dev", help="changed files are against this ref")
    ap.add_argument("--memory", action="append", default=[], metavar="IMAGE=MB")
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--only", default=",".join(CHECKS))
    a = ap.parse_args(argv)
    repo = pathlib.Path(a.repo).resolve()
    only = [c for c in a.only.split(",") if c]
    if set(only) - set(CHECKS):
        print(f"--only: unknown check(s) {', '.join(sorted(set(only) - set(CHECKS)))}", file=sys.stderr)
        return 2
    rep = Report()
    changed, err = changed_files(repo, a.base)
    if changed is None:
        rep.add("FAIL", f"changed files: cannot diff against {a.base}: {err[:200]}")
        changed = []
    config_file = repo / "config" / f"{a.env}.yaml"
    config = (yaml.safe_load(config_file.read_text(encoding="utf-8")) if config_file.exists() else None) or {}
    region = config.get("region")  # never a fallback: the region decides which region statements apply
    plans: list[dict] = []
    if (a.plan_json or a.plan) and ("iam" in only or "memory" in only):
        plans, err = load_plans(repo, a.plan_json, a.plan, a.plan_dir)
        if err:
            rep.add("FAIL", f"plan: {err}")
    if "iam" in only:
        if not (a.plan_json or a.plan):
            rep.add("SKIP", "iam: no plan; pass --plan-json <terraform show -json output> or --plan <plan file>")
        elif not a.profile:
            rep.add("SKIP", "iam: no --profile for the read-only simulation")
        elif not region:
            rep.add("FAIL", f"iam: config/{a.env}.yaml is missing or names no region (aws:RequestedRegion needs it)")
        elif plans:
            iam_check(plans, a.apply_role, region, aws_factory(a.profile, region), rep)
    if "memory" in only:
        memory_check(repo, changed, parse_memory(a.memory), a.seconds, rep, plans=plans)
    if "edge" in only:
        edge_check(repo, changed, a.env, rep)
    n = rep.failed()
    print("LIVE PREFLIGHT OK" if n == 0 else f"LIVE PREFLIGHT FAILED ({n} failed)")
    return 1 if n else 0


if __name__ == "__main__":
    sys.exit(main())
