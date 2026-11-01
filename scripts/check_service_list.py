"""Keep operate.yml in step with the services file (delivery-pipelines.md UC-DEL-002).

  check_service_list.py --services-file <services.<env>.yaml> [--write]

Fails if the `service` choice list in .github/workflows/operate.yml is not `all` plus the services file's
long-running services, in file order. `--write` regenerates the list. It also fails if the task-definition
render function differs between release-ecs.yml and operate.yml (one function, delivery-pipelines §5).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

ONE_OFF = {"migration", "operator", "function"}
LIST = re.compile(r"(# service-list begin\n\s*options: )\[[^\]]*\](\n\s*# service-list end)")
BLOCK = re.compile(r"# --- render-taskdef begin[^\n]*\n(.*?)# --- render-taskdef end ---", re.S)


def long_running(doc: dict) -> list[str]:
    return [n for n, s in doc["services"].items() if not set(s.get("triggers", [])) <= ONE_OFF]


def blocks(text: str) -> list[str]:
    return [re.sub(r"^\s+", "", b, flags=re.M) for b in BLOCK.findall(text)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--services-file", type=Path)
    ap.add_argument("--workflows", type=Path, default=Path(".github/workflows"))
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)
    operate = args.workflows / "operate.yml"
    text = operate.read_text(encoding="utf-8")
    problems: list[str] = []
    if args.services_file:
        wanted = "[" + ", ".join(["all", *long_running(yaml.safe_load(args.services_file.read_text(encoding="utf-8")))]) + "]"
        if not LIST.search(text):
            problems.append("operate.yml has no service-list markers")
        elif args.write:
            operate.write_text(LIST.sub(lambda m: m.group(1) + wanted + m.group(2), text), encoding="utf-8")
        elif LIST.search(text).group(0).split("options: ")[1].split("\n")[0] != wanted:
            problems.append(f"operate.yml service list has drifted from the services file; expected {wanted}")
    rel = blocks((args.workflows / "release-ecs.yml").read_text(encoding="utf-8"))
    ops = blocks(text)
    if not rel or not ops:
        problems.append("a render-taskdef block is missing")
    elif any(b != rel[0] for b in rel + ops):
        problems.append("the render-taskdef function differs between release-ecs.yml and operate.yml")
    for p in problems:
        print("::error::" + p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
