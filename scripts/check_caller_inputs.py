#!/usr/bin/env python3
"""Every caller passes every required input (onepeek-docs architecture/lld/infra/delivery-pipelines.md §6.4a, F35).

    python3 check_caller_inputs.py <repo-dir> --shared <.github checkout>

For every job-level `uses: OnePeek/.github/.github/workflows/<file>@<sha>` in the repo's `.github/workflows/*.yml`, the
called workflow is read at that commit (`git show <sha>:.github/workflows/<file>` in the `.github` checkout), and for
every `uses: ./.github/workflows/<file>` from the repo's own tree. A call fails when its `with:` lacks an input the
callee marks `required: true` (even one with a `default:`), passes an input or a secret the callee does not declare, or
neither passes nor inherits (`secrets: inherit`) a secret the callee marks `required: true`. A commit, file or
workflow it cannot read is a failure, never a skip. Each failure is one line on stdout; the exit code is 1 on any.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

import yaml

SHARED = re.compile(r"onepeek/\.github/\.github/workflows/([^@/]+)(?:@(.*))?", re.I)
LOCAL = re.compile(r"\./\.github/workflows/([^@/]+)")
SHA = re.compile(r"[0-9a-f]{40}")
TAG_COMMENT = re.compile(r"#\s*(v\d+\.\d+\.\d+)(?![\w.])")


def git(shared: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(shared), *args], capture_output=True, text=True)


def load(text: str):
    """The parsed document and its node tree (for line numbers). yaml.YAMLError when it does not parse."""
    return yaml.safe_load(text), yaml.compose(text)


def declared(callee) -> tuple[dict, dict] | None:
    """(inputs, secrets) of the callee's `on.workflow_call`, or None when it is not a reusable workflow."""
    if not isinstance(callee, dict):
        return None
    on = callee.get("on", callee.get(True))  # YAML 1.1 reads the `on` key as True
    if on == "workflow_call" or (isinstance(on, list) and "workflow_call" in on):
        return {}, {}
    if not isinstance(on, dict) or "workflow_call" not in on:
        return None
    wc = on["workflow_call"] or {}
    if not isinstance(wc, dict):
        return None
    return wc.get("inputs") or {}, wc.get("secrets") or {}


def required(entry) -> bool:
    return isinstance(entry, dict) and entry.get("required") is True


def callee_text(uses: str, repo: pathlib.Path, shared: pathlib.Path, source_line: str) -> tuple[str, str | None, str | None]:
    """(label, text, reason): the callee's `<file>@<tag>` label and its text, or the failure reason."""
    if m := LOCAL.fullmatch(uses):
        label = f"./.github/workflows/{m.group(1)}"
        path = repo / ".github/workflows" / m.group(1)
        if not path.is_file():
            return label, None, "file not found"
        try:
            return label, path.read_text(encoding="utf-8"), None
        except (OSError, UnicodeDecodeError):
            return label, None, "cannot parse"
    m = SHARED.fullmatch(uses)
    name, ref = m.group(1), (m.group(2) or "")
    tag = TAG_COMMENT.search(source_line)  # the trailing `# vX.Y.Z` comment, else the first 12 hex of the commit
    label = f"{name}@{tag.group(1) if tag else (ref[:12] if SHA.fullmatch(ref) else ref)}"
    if not SHA.fullmatch(ref):
        return label, None, "ref is not a commit sha"
    if git(shared, "cat-file", "-e", f"{ref}^{{commit}}").returncode:
        return label, None, "pin commit not found"
    r = git(shared, "show", f"{ref}:.github/workflows/{name}")
    if r.returncode:
        return label, None, "file not found"
    return label, r.stdout, None


def node_items(node) -> dict:
    """key -> (key node, value node) of a mapping node; {} for anything else."""
    if not isinstance(node, yaml.MappingNode):
        return {}
    return {k.value: (k, v) for k, v in node.value if isinstance(k, yaml.ScalarNode)}


def check_call(caller: str, line: int, job_id: str, job: dict, uses: str, source_line: str, repo: pathlib.Path,
               shared: pathlib.Path) -> list[str]:
    label, text, reason = callee_text(uses, repo, shared, source_line)
    where = f"{caller}:{line}: {job_id} calls {label}"
    if reason:
        return [f"{where}: {reason}"]
    try:
        callee = yaml.safe_load(text)
    except yaml.YAMLError:
        return [f"{where}: cannot parse"]
    decl = declared(callee)
    if decl is None:
        return [f"{where}: not a reusable workflow"]
    inputs, secrets = decl
    given = job.get("with") or {}
    passed = job.get("secrets")
    if not isinstance(given, dict) or not (passed is None or passed == "inherit" or isinstance(passed, dict)):
        return [f"{where}: cannot parse"]
    out = [f"{where} without required input {k}" for k, v in inputs.items() if required(v) and k not in given]
    out += [f"{where} with unknown input {k}" for k in given if k not in inputs]
    if passed != "inherit":
        passed = passed or {}
        out += [f"{where} without required secret {k}" for k, v in secrets.items() if required(v) and k not in passed]
        out += [f"{where} with unknown secret {k}" for k in passed if k not in secrets]
    return out


def check(repo: pathlib.Path, shared: pathlib.Path) -> list[str]:
    failures = []
    for path in sorted((repo / ".github/workflows").glob("*.yml")):
        try:
            text = path.read_text(encoding="utf-8")
            doc, tree = load(text)
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
            mark = getattr(e, "problem_mark", None)
            failures.append(f"{path.name}:{mark.line + 1 if mark else 1}: cannot parse")
            continue
        lines = text.splitlines()
        jobs = (doc or {}).get("jobs") if isinstance(doc, dict) else None
        job_nodes = node_items(node_items(tree).get("jobs", (None, None))[1])
        for job_id, job in (jobs or {}).items() if isinstance(jobs, dict) else ():
            if not isinstance(job, dict) or not isinstance(job.get("uses"), str):
                continue
            uses = job["uses"].strip()
            if not (SHARED.fullmatch(uses) or LOCAL.fullmatch(uses)):
                continue
            key = node_items(job_nodes.get(str(job_id), (None, None))[1]).get("uses", (None, None))[0]
            line = key.start_mark.line + 1 if key is not None else 1
            failures += check_call(path.name, line, str(job_id), job, uses, lines[line - 1], repo, shared)
    return failures


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("repo")
    p.add_argument("--shared", required=True, help="a checkout of OnePeek/.github holding the pinned commits")
    a = p.parse_args(argv)
    if not pathlib.Path(a.repo).is_dir():
        print(f"{a.repo}: not a directory")  # fail closed: a wrong path never passes as "no calls"
        return 1
    failures =check(pathlib.Path(a.repo), pathlib.Path(a.shared))
    for f in failures:
        print(f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
