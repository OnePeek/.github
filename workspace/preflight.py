#!/usr/bin/env python3
"""Preflight before every push and release (onepeek-docs architecture/lld/infra/foundation-playbook.md §8a P4).

    python3 .github/workspace/preflight.py <repo dir> [--release] [--depends-on OnePeek/<repo>#<n> ...]

0. Declared dependencies (§8a P1, lesson L26): the `Depends on: OnePeek/<repo>#<n>` lines of the branch's open pull
   request, or, before it has one, each `--depends-on`. While one is unmerged preflight refuses the push (keep the
   branch local, or open its pull request as a draft: a draft's push is a warning, its CI waits in `depends-on`).
1. Local CI (`local_ci.py <repo>`): preflight refuses to go on unless it ends `LOCAL CI OK`.
2. The pin checks (§8a P2, S4): every OnePeek/.github pin is the commit of one `v*` tag.
3. Tool versions: the laptop's Terraform, Python and Node match every tracked `.terraform-version`, `.python-version`
   and `.node-version`; uv matches CI's pin (a mismatch fails); tflint does too (a mismatch is a warning, §8a P3).
4. With `--release`, the prerequisites of the steps that run only on a release pull request:
   - docs-version.txt is an ancestor of onepeek-docs main (the pins.yml rule for release pull requests);
   - tokens: the agents' App's granted permissions against every `create-github-app-token` permission the repo's
     workflows and the shared ones they call request, and its installed repositories against the repositories those
     steps name ("unverified" without org-admin read); repository secrets cannot be read from a laptop ("unverified");
   - the other repos' release state, for each repo the contract or the release lock reads: more than one open
     dev -> release pull request fails, and so does none while `release` is stale: the second parent of the newest
     merge commit reachable from `release` (the last dev commit merged into it; a hotfix squashed on top does not
     count), or `release` itself when it is on `dev` (a fast-forward), is not an ancestor of `dev`; no merge commit
     and `release` not on `dev` also fails ("no release merge found").

The docs pin's wait (docs-version.txt names a commit of an open docs pull request, the normal state of a change set)
prints as a NOTE; with `--release` it fails, and a pin in no open docs pull request always fails.

Each line is PASS, FAIL, WARN, NOTE or UNVERIFIED; the last line is `PREFLIGHT OK` or `PREFLIGHT FAILED`. Exit 1 on any FAIL.
Needs `gh` signed in for the pin tags and `--release`.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys

import yaml

HERE = pathlib.Path(__file__).resolve().parent
SHARED = HERE.parent  # this .github checkout
ORG = "OnePeek"
APP_SLUG = "onepeek-agents"  # §8a S6
CONTRACT_REPOS = ("backend-service", "onepeek-web", "onepeek-infra")  # contract.yml's three sides
LOCK_REPOS = ("backend-service", "onepeek-web", "onepeek-infra", "mcp-athena")  # the repos that take the lock (§5.4)
LOCK_WORKFLOWS = ("release-lock.yml", "release-ecs.yml", "operate.yml")
LEVEL = {"read": 1, "write": 2, "admin": 3}


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


check_pins = _load("check_pins", SHARED / "scripts" / "check_pins.py")
depends_on = _load("check_depends_on", SHARED / "scripts" / "check_depends_on.py")
local_ci = _load("local_ci", HERE / "local_ci.py")


class Report:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def add(self, status: str, text: str) -> None:
        self.lines.append((status, text))
        print(f"{status:<10}  {text}", flush=True)

    def failed(self) -> int:
        return sum(1 for s, _ in self.lines if s == "FAIL")


def sh(cmd: list[str], cwd: pathlib.Path | None = None) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    except FileNotFoundError:
        return 127, f"{cmd[0]} is not installed"
    return r.returncode, (r.stdout if r.returncode == 0 else (r.stderr or r.stdout)).strip()


def gh_json(path: str) -> tuple[bool, object]:
    """`gh api <path>` as JSON: (True, data) or (False, the error text)."""
    rc, out = sh(["gh", "api", path])
    if rc:
        return False, out[:300]
    try:
        return True, json.loads(out or "null")
    except json.JSONDecodeError:
        return False, out[:300]


# 0. Declared dependencies -------------------------------------------------------------------------------------------
def dependency_check(repo: pathlib.Path, name: str, given: list[str], rep: Report, api=gh_json) -> bool:
    """False (refuse) while a declared dependency is unmerged and the branch's pull request is not a draft."""
    _, branch = sh(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"])
    ok, prs = api(f"repos/{ORG}/{name}/pulls?state=open&head={ORG}:{branch}")
    if not ok:
        rep.add("FAIL", f"depends on: cannot read {name}'s open pull requests for {branch}: {prs}")
        return False
    pr = prs[0] if prs else None
    body = (pr.get("body") or "") if pr else ""
    deps = depends_on.parse(body + "\n" + "\n".join(f"Depends on: {g}" for g in given))
    if not deps:
        rep.add("PASS", "depends on: no declared dependencies")
        return True

    def state(r: str, n: int) -> tuple[str, bool]:
        ok, data = api(f"repos/{ORG}/{r}/pulls/{n}")
        if not ok:
            raise SystemExit(f"PREFLIGHT FAILED: cannot read {r}#{n}: {data}")
        return data["state"], bool(data.get("merged"))

    waiting = depends_on.waiting(deps, state)
    if not waiting:
        rep.add("PASS", "depends on: " + ", ".join(f"{r}#{n}" for r, n in deps) + " merged")
        return True
    if pr and pr.get("draft"):
        rep.add("WARN", f"depends on: {'; '.join(waiting)}; #{pr['number']} is a draft, so its CI waits in depends-on")
        return True
    rep.add("FAIL", f"depends on: {'; '.join(waiting)}. Refusing the push: keep the branch local, or open its pull "
                    "request as a draft")
    return False


# 1. Local CI ------------------------------------------------------------------------------------------------------
def run_local_ci(repo: pathlib.Path) -> tuple[bool, str]:
    """Runs local CI with its output shown; ok only when it exits 0 and its last line is `LOCAL CI OK ...`."""
    r = subprocess.run([sys.executable, str(HERE / "local_ci.py"), str(repo)], capture_output=True, text=True)
    out = r.stdout + r.stderr
    print(out, end="" if out.endswith("\n") else "\n", flush=True)
    last = next((ln for ln in reversed(out.splitlines()) if ln.strip()), "")
    return r.returncode == 0 and last.startswith("LOCAL CI OK"), last


# 2. Pins ----------------------------------------------------------------------------------------------------------
def pin_checks(repo: pathlib.Path, rep: Report, tags=None) -> None:
    found = check_pins.pins(repo)
    if not found:
        rep.add("PASS", "pins: no OnePeek/.github pins")
        return
    problems = check_pins.pin_problems(found, tags if tags is not None else check_pins.remote_tags())
    for p in problems:
        rep.add("FAIL", f"pins: {p}")
    if not problems:
        rep.add("PASS", f"pins: {len(found)} OnePeek/.github pin(s), one tagged version ({found[0][3][:7]})")


# 3. Tool versions -------------------------------------------------------------------------------------------------
def version_files(repo: pathlib.Path) -> list[pathlib.Path]:
    rc, out = sh(["git", "-C", str(repo), "ls-files", "--", ".terraform-version", "**/.terraform-version",
                  ".python-version", "**/.python-version", ".node-version", "**/.node-version"])
    return [repo / p for p in sorted(set(out.splitlines()))] if rc == 0 else []


def matches(installed: str, wanted: str) -> bool:
    """`3.13` accepts 3.13.x; `1.16.6` only itself."""
    return installed == wanted or installed.startswith(wanted + ".")


def installed_version(kind: str, wanted: str, cwd: pathlib.Path) -> str | None:
    """The version the laptop uses in cwd, or None when the tool is missing."""
    if kind == "terraform":
        rc, out = sh(["terraform", "version", "-json"], cwd)
        return json.loads(out).get("terraform_version") if rc == 0 else None
    if kind == "python":
        rc, path = sh(["uv", "python", "find", "--no-config", wanted], cwd)  # what uv runs for the repo's pin
        if rc:
            return None
        rc, out = sh([path.splitlines()[-1], "--version"], cwd)
        return out.split()[-1] if rc == 0 else None
    rc, out = sh(["node", "--version"], cwd)
    return out.lstrip("v") if rc == 0 else None


FIX = {"terraform": "tenv tf install {v}", "python": "uv python install {v}", "node": "install Node {v} (fnm/nvm/mise)"}


def tool_checks(repo: pathlib.Path, rep: Report, version=installed_version) -> None:
    for f in version_files(repo):
        kind = f.name.strip(".").split("-")[0]
        wanted = f.read_text(encoding="utf-8").strip().lstrip("v")
        got = version(kind, wanted, f.parent)
        where = f.relative_to(repo)
        if got and matches(got, wanted):
            rep.add("PASS", f"tools: {kind} {got} matches {where} ({wanted})")
        else:
            rep.add("FAIL", f"tools: {kind} {got or 'is missing'}, {where} pins {wanted}; {FIX[kind].format(v=wanted)}")
    uv = local_ci.uv_problem()
    rep.add("FAIL" if uv else "PASS", f"tools: {uv or 'uv ' + local_ci.UV_VERSION + ' (CI pin)'}")
    if uses_tflint(repo):
        why = local_ci.tflint_problem()
        rep.add("WARN" if why else "PASS", f"tools: {why + '; local CI skips tflint' if why else 'tflint ' + local_ci.TFLINT_VERSION + ' (CI pin)'}")


def uses_tflint(repo: pathlib.Path) -> bool:
    return any(re.search(r"tflint|terraform-checks\.yml", f.read_text(encoding="utf-8"))
               for f in (repo / ".github/workflows").glob("*.y*ml"))


# 4. Release prerequisites -----------------------------------------------------------------------------------------
def shared_text(path: str, sha: str) -> str:
    """A shared workflow at the ref the caller pins (fetched when this checkout lacks it), else the local copy."""
    for attempt in range(2 if sha else 0):
        rc, out = sh(["git", "-C", str(SHARED), "show", f"{sha}:{path}"])
        if rc == 0:
            return out
        if attempt == 0:
            sh(["git", "-C", str(SHARED), "fetch", "-q", "origin", sha])
    local = SHARED / path
    return local.read_text(encoding="utf-8") if local.exists() else ""


def workflows(repo: pathlib.Path) -> list[tuple[str, str]]:
    """(label, text) for the repo's workflows and every shared workflow they call, at the pinned commits."""
    out, seen = [], set()
    for f in sorted((repo / ".github/workflows").glob("*.y*ml")):
        text = f.read_text(encoding="utf-8")
        out.append((f.name, text))
        for m in check_pins.USES.finditer(text):  # every OnePeek/.github call, any letter case and any ref (P2)
            path, sha = m.group("path") or "", m.group("ref") or ""
            if path.startswith(".github/workflows/") and (path, sha) not in seen:
                seen.add((path, sha))
                out.append((f"{pathlib.Path(path).name}@{sha[:7] if check_pins.SHA.fullmatch(sha) else sha}",
                            shared_text(path, sha)))
    return out


def app_requests(texts: list[tuple[str, str]], own: str) -> list[dict]:
    """Every create-github-app-token step: where, the permissions it asks for, the repositories it names."""
    reqs = []
    for label, text in texts:
        doc = yaml.safe_load(text) or {}
        for job_id, job in (doc.get("jobs") or {}).items():
            for st in (job or {}).get("steps") or []:
                if not str(st.get("uses") or "").startswith("actions/create-github-app-token"):
                    continue
                w = st.get("with") or {}
                perms = {k.removeprefix("permission-").replace("-", "_"): str(v)
                         for k, v in w.items() if k.startswith("permission-")}
                repos = str(w.get("repositories") or own)
                reqs.append({"where": f"{label}:{job_id}", "permissions": perms,
                             "repositories": None if "${{" in repos else [r.strip() for r in re.split(r"[,\n]", repos) if r.strip()]})
    return reqs


def secret_names(texts: list[tuple[str, str]]) -> list[str]:
    return sorted({m for _, t in texts for m in re.findall(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)", t)} - {"GITHUB_TOKEN"})


def installation(api=gh_json) -> tuple[dict | None, list[str] | None, str]:
    """(granted permissions, installed repo names or None for all, why unverified). Needs org-admin read."""
    ok, data = api(f"orgs/{ORG}/installations?per_page=100")
    if not ok:
        return None, None, f"cannot list the organisation's App installations (needs org-admin read): {data}"
    inst = next((i for i in data.get("installations", []) if i.get("app_slug") == APP_SLUG), None)
    if not inst:
        return {}, [], f"{APP_SLUG} is not installed on {ORG}"
    if inst.get("repository_selection") == "all":
        return inst.get("permissions") or {}, None, ""
    ok, repos = api(f"user/installations/{inst['id']}/repositories?per_page=100")
    if not ok:
        return inst.get("permissions") or {}, None, f"cannot list the repositories {APP_SLUG} is installed on: {repos}"
    return inst.get("permissions") or {}, [r["name"] for r in repos.get("repositories", [])], ""


def token_checks(texts: list[tuple[str, str]], own: str, rep: Report, api=gh_json) -> None:
    reqs = app_requests(texts, own)
    granted, installed, why = installation(api)
    if granted is None:
        ok, app = api(f"apps/{APP_SLUG}")
        granted = (app.get("permissions") or {}) if ok else None
        if granted is None:
            rep.add("UNVERIFIED", f"tokens: cannot read {APP_SLUG}'s permissions: {app}")
    if why:
        rep.add("UNVERIFIED", f"tokens: {why}")
    for r in reqs:
        if granted is not None:
            short = [f"{p}: {lvl} (has {granted.get(p, 'none')})" for p, lvl in sorted(r["permissions"].items())
                     if LEVEL.get(str(granted.get(p)), 0) < LEVEL.get(lvl, 9)]
            if short:
                rep.add("FAIL", f"tokens: {r['where']} asks {APP_SLUG} for {', '.join(short)}")
            else:
                asked = ", ".join(f"{p}: {lvl}" for p, lvl in sorted(r["permissions"].items())) or "its default permissions"
                rep.add("PASS", f"tokens: {r['where']} asks for {asked}; granted")
        if r["repositories"] is None:
            rep.add("UNVERIFIED", f"tokens: {r['where']} names its repositories with an expression")
        elif installed is not None:
            missing = sorted(set(r["repositories"]) - set(installed))
            rep.add("FAIL" if missing else "PASS",
                    f"tokens: {r['where']} " + (f"names {', '.join(missing)}, where {APP_SLUG} is not installed" if missing
                                                 else f"names {', '.join(r['repositories'])}; all installed"))
    names = secret_names(texts)
    if names:
        rep.add("UNVERIFIED", f"secrets: {', '.join(names)} cannot be read from a laptop; check they are set in {own}")


def release_repos(repo_name: str, texts: list[tuple[str, str]]) -> list[str]:
    """The repos whose release state this repo's release reads: the contract's other sides, the lock's other repos."""
    out: set[str] = set()
    joined = "\n".join(t for _, t in texts).lower()  # a pin in any letter case counts (P2)
    if "workflows/contract.yml@" in joined:
        out |= set(CONTRACT_REPOS)
    if any(f"workflows/{w}@" in joined for w in LOCK_WORKFLOWS):
        out |= set(LOCK_REPOS)
    out.discard(repo_name)
    return sorted(out)


def ancestor_of_dev(repo: str, commit: str, api) -> tuple[bool | None, str]:
    """(True when `commit` is an ancestor of dev or dev itself, None when the compare fails, its status or error)."""
    ok, cmp = api(f"repos/{ORG}/{repo}/compare/{commit}...dev")
    if not ok:
        return None, str(cmp)
    return cmp.get("status") in ("ahead", "identical"), str(cmp.get("status"))


def newest_release_merge(repo: str, api) -> tuple[bool, dict | None | str]:
    """The newest merge commit reachable from `release` (walking its history newest first): (True, commit or None when
    there is none) or (False, the error)."""
    page = 1
    while True:
        ok, commits = api(f"repos/{ORG}/{repo}/commits?sha=release&per_page=100&page={page}")
        if not ok:
            return False, str(commits)
        for c in commits or []:
            if len(c.get("parents") or []) > 1:
                return True, c
        if len(commits or []) < 100:
            return True, None
        page += 1


def release_state(repo: str, rep: Report, api=gh_json) -> None:
    """At most one open dev -> release pull request; with none, `release` must not be stale (§8a P4): the last dev
    commit merged into it (the second parent of the newest merge commit reachable from `release`, so a hotfix squashed
    on top does not count; or `release` itself when it is on dev, a fast-forward) is an ancestor of dev."""
    ok, prs = api(f"repos/{ORG}/{repo}/pulls?state=open&base=release&head={ORG}:dev")
    if not ok:
        rep.add("FAIL", f"release state: cannot read {repo}'s open pull requests into release: {prs}")
        return
    if len(prs) > 1:
        rep.add("FAIL", f"release state: {repo} has {len(prs)} open dev -> release pull requests; close all but one")
        return
    if len(prs) == 1:
        rep.add("PASS", f"release state: {repo} release candidate is #{prs[0]['number']} ({prs[0]['head']['sha'][:7]})")
        return
    on_dev, why = ancestor_of_dev(repo, "release", api)
    if on_dev is None:
        rep.add("FAIL", f"release state: cannot compare {repo} release with dev (no release branch?): {why}")
        return
    if on_dev:
        rep.add("PASS", f"release state: {repo} has no open release pull request; release is on dev (fast-forward)")
        return
    ok, merge = newest_release_merge(repo, api)
    if not ok:
        rep.add("FAIL", f"release state: cannot read {repo}'s release history: {merge}")
        return
    if merge is None:
        rep.add("FAIL", f"release state: {repo} has no open release pull request, release is not on dev and no release "
                        "merge found: the check would compare against code dev no longer builds on; rebuild release from dev")
        return
    merged = merge["parents"][1]["sha"]
    on_dev, why = ancestor_of_dev(repo, merged, api)
    if on_dev is None:
        rep.add("FAIL", f"release state: cannot compare {repo}'s last release merge ({merged[:7]}) with dev: {why}")
    elif on_dev:
        rep.add("PASS", f"release state: {repo} has no open release pull request; its last release merge "
                        f"({merge['sha'][:7]}) brought in {merged[:7]}, which is on dev")
    else:
        rep.add("FAIL", f"release state: {repo} has no open release pull request and release is stale: its last release "
                        f"merge ({merge['sha'][:7]}) brought in {merged[:7]}, which is not an ancestor of dev ({why}); "
                        "the check would compare against code dev no longer builds on; rebuild release from dev")


def docs_wait_check(repo: pathlib.Path, rep: Report, strict: bool, status=None, prs=None) -> None:
    """The implicit docs dependency (lesson L27 a), as depends-on.yml reports it: a pin in an open docs pull request is a
    NOTE locally (the normal state of a change set), a FAIL with `strict` (--release); a pin in no open docs pull
    request always fails. `status` and `prs` are the API reads, replaceable in tests."""
    _, pin = check_pins.docs_pin(repo)
    if not pin:
        return
    token = os.environ.get("GH_TOKEN") or sh(["gh", "auth", "token"])[1]
    status = status or (lambda s: check_pins.compare_status(s, token))
    prs = prs or (lambda s: depends_on.open_docs_prs(s, token))
    problems, notes = depends_on.docs_waiting(pin, status, prs, strict)
    for p in problems:
        rep.add("FAIL", f"docs wait: {p}")
    for n in notes:
        rep.add("NOTE", f"docs wait: {n}")


def docs_check(repo: pathlib.Path, rep: Report) -> None:
    token = os.environ.get("GH_TOKEN") or sh(["gh", "auth", "token"])[1]
    has, _ = check_pins.docs_pin(repo)
    problems = check_pins.docs_problems(repo, token)
    for p in problems:
        rep.add("FAIL", f"docs pin: {p}")
    if not problems:
        rep.add("PASS", "docs pin: docs-version.txt is an ancestor of onepeek-docs main" if has else "docs pin: no docs-version.txt")


def repo_name(repo: pathlib.Path) -> str:
    _, url = sh(["git", "-C", str(repo), "remote", "get-url", "origin"])
    return url.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1] or repo.name


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("repo")
    ap.add_argument("--release", action="store_true", help="also check the prerequisites of release-only steps")
    ap.add_argument("--depends-on", action="append", default=[], metavar="OnePeek/<repo>#<n>",
                    help="a dependency of a branch with no pull request yet (repeatable)")
    a = ap.parse_args(argv)
    repo = pathlib.Path(a.repo).resolve()
    if not (repo / ".github/workflows").is_dir():
        print(f"{repo}: no .github/workflows", file=sys.stderr)
        return 2
    rep = Report()
    if not dependency_check(repo, repo_name(repo), a.depends_on, rep):
        print("PREFLIGHT FAILED (refused: a declared dependency is unmerged)")
        return 1
    ok, last = run_local_ci(repo)
    if not ok:
        rep.add("FAIL", f"local CI did not end LOCAL CI OK (last line: {last!r}); fix it first")
        print("PREFLIGHT FAILED (refused: local CI is not OK)")
        return 1
    rep.add("PASS", "local CI: LOCAL CI OK")
    pin_checks(repo, rep)
    docs_wait_check(repo, rep, strict=a.release)
    tool_checks(repo, rep)
    if a.release:
        name = repo_name(repo)
        texts = workflows(repo)
        docs_check(repo, rep)
        token_checks(texts, name, rep)
        for other in release_repos(name, texts):
            release_state(other, rep)
    n = rep.failed()
    print("PREFLIGHT OK" if n == 0 else f"PREFLIGHT FAILED ({n} failed)")
    return 1 if n else 0


if __name__ == "__main__":
    sys.exit(main())
