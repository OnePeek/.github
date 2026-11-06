#!/usr/bin/env python3
"""P19 hook: the cancel and dispatch rules (onepeek-docs architecture/lld/infra/foundation-playbook.md §1 P19 and §5.4a
"cancel-release.yml", option A; approved by the founder, decision 167, 2026-11-06).

A Claude Code PreToolUse hook on Bash: it runs only for an agent's tool calls, so every call it sees is an agent session.
It refuses any way to cancel a run or dispatch a workflow other than the wrapper `.github/workspace/cancel_release.py`
(which itself refuses prod there):
  - `gh workflow run` of cancel-release.yml, and any other `gh workflow run` (a dispatch);
  - `gh run cancel`;
  - `gh api` calls to `.../dispatches`, `.../runs/<id>/cancel` or `.../runs/<id>/force-cancel`.
Reads the hook's JSON on stdin; exit 2 with the reason on stderr blocks the call. P19's other rules (no `aws`, no
`terraform apply`, no cloud CLIs in sub-agents) are not built here yet (playbook §12 slice 0).
"""
from __future__ import annotations

import json
import re
import shlex
import sys

WRAPPER = ".github/workspace/cancel_release.py"
REFUSED = "P19: agents start a cancel only with `python3 .github/workspace/cancel_release.py <repo> <env> --reason <text>` (foundation-playbook §5.4a)"
OPERATORS = "&|;\n()`"   # a single & (background, &&), pipes, ;, newline, subshell and command-substitution brackets
API_PATH = re.compile(r"/(dispatches|runs/[^/\s]+/(force-)?cancel)\b")
# gh's global flags that take a value, so `gh -R <repo> run cancel 1` is still seen as `run cancel`.
# That includes `gh workflow run`'s own `-F/-f/-r` and their long forms: `gh workflow -F a=b run x.yml` still dispatches.
VALUE_FLAGS = ("-R", "--repo", "--hostname", "-F", "--field", "-f", "--raw-field", "-r", "--ref")
LEADERS = ("env", "command", "exec", "sudo", "nohup", "time", "{", "!", "then", "do", "else")


def subcommand(words: list[str]) -> list[str]:
    """gh's words after the program name, without flags and the values of the flags that take one."""
    out, skip = [], False
    for w in words[1:]:
        if skip:
            skip = False
        elif w in VALUE_FLAGS:
            skip = True
        elif not w.startswith("-"):
            out.append(w)
    return out or [""]


def split_commands(command: str) -> list[str]:
    """The text of a shell line cut at every operator that is not inside quotes (a command substitution inside double
    quotes still starts a command, so it is cut there too); quoted text such as `echo "a & b"` stays whole."""
    parts, cur, quote, i = [], [], None, 0
    while i < len(command):
        ch = command[i]
        if quote == "'":
            cur.append(ch)
            quote = None if ch == "'" else quote
        elif ch == "\\" and i + 1 < len(command):
            cur.append(command[i:i + 2])
            i += 1
        elif quote == '"':
            if ch == '"':
                quote = None
                cur.append(ch)
            elif ch == "`" or command.startswith("$(", i):
                parts.append("".join(cur))
                cur = []
                i += 1 if ch == "$" else 0   # skip the "(" of "$("
            else:
                cur.append(ch)
        elif ch in "'\"":
            quote = ch
            cur.append(ch)
        elif ch in OPERATORS:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return parts


def segments(command: str) -> list[list[str]]:
    """Each simple command of a shell line, split into words (unparseable text is kept as plain words)."""
    out = []
    for part in split_commands(command):
        part = part.strip()
        if not part:
            continue
        try:
            words = shlex.split(part)
        except ValueError:
            words = part.split()
        # Skip leading environment assignments and wrappers such as `env X=1 gh ...`.
        while words and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]) or words[0] in LEADERS):
            words = words[1:]
        if words:
            out.append(words)
    return out


def refusal(command: str) -> str | None:
    """Why this command is refused, or None."""
    for words in segments(command):
        name = words[0].rsplit("/", 1)[-1]
        if name in ("bash", "sh", "zsh") and "-c" in words:
            inner = refusal(" ".join(words[words.index("-c") + 1:]))
            if inner:
                return inner
        if name != "gh":
            continue
        rest = subcommand(words)
        if rest[:2] == ["workflow", "run"]:
            return f"{REFUSED}; `gh workflow run` dispatches a workflow."
        if rest[:2] == ["run", "cancel"]:
            return f"{REFUSED}; `gh run cancel` cancels a run."
        if rest[0] == "api" and any(API_PATH.search(w) for w in words[2:]):
            return f"{REFUSED}; this `gh api` call dispatches or cancels."
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    if event.get("tool_name") != "Bash":
        return 0
    command = str((event.get("tool_input") or {}).get("command", ""))
    why = refusal(command)
    if why:
        print(why, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
