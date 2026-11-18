"""Choose which `terraform test` suites a pull request must run (used by .github/workflows/terraform-checks.yml).

A suite is a directory with a `*.tftest.hcl` file: the folder above its `tests/` folder (any depth under tests/), else
the folder holding the file. Release pull requests (base `release`), pushes, schedules and manual runs run every suite.

On a pull request into `dev` a suite runs when a changed file is in its reach (INFRA-CI-SCOPE):
  - a file of the suite's own unit (a unit is a folder with a `*.tf` file, or a suite folder; a file belongs to the
    deepest unit holding it), or of a unit the suite reaches through local module sources (`source = "../x"`),
    transitively, so a changed module runs its own suite and every suite that uses it (a change under a used unit's
    own tests/ folder runs only that unit's suite);
  - a file the reached units' Terraform reads by path: a `${path.module}/..`, `${path.root}/..`, `${path.cwd}/..` or
    `../`, `./` path anywhere in their `.tf` / `.tftest.hcl` text (heredocs and comments too), an interpolation in it
    matching anything, a folder meaning everything under it; `${local.X}/rest` when local X is such a path (X itself then
    counts only when some use of it is not `${local.X}/...`); a referenced `.py` file adds the sibling modules it imports.
Every suite runs (fail closed) when the caller's own workflow file changed, when a Terraform-like file (`.tf`, `.hcl`,
`.tfvars`, `.terraform-version`, ...) or a `tests/` file changed outside every unit, when a local module source does not
resolve to a unit, when a path ends in `${...}/..`, when a Terraform read builds its path in a way this cannot resolve
to a literal (`format()`, `join()`, `abspath()`, `fileset()`, a `var.` or any other expression, or `path.module`,
`path.root`, `path.cwd` used other than as `${path.x}/`), or when the diff or any file cannot be read.

  python3 tf_test_selection.py --event pull_request --base-ref dev --diff-against origin/dev \
      --caller-workflow OWNER/REPO/.github/workflows/checks.yml@refs/pull/1/merge      # one suite per line
Prints nothing and exits 0 when no suite is selected; exits 1 when the repository has no suite at all. Why it chose
what it chose goes to stderr.
"""
from __future__ import annotations

import argparse
import posixpath
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

SKIP_PARTS = {".terraform", ".git", "node_modules"}
TF_FILES = (".tf", ".tf.json", ".tftest.hcl", ".tftest.json", ".tfmock.hcl", ".tfmock.json", ".tfvars", ".tfvars.json")
TF_LIKE = TF_FILES + (".hcl",)
TF_LIKE_NAMES = {".terraform-version", ".tflint.hcl", ".terraformrc", "terraform.rc"}
SOURCE = re.compile(r'\bsource\s*=\s*"(\.\.?/[^"]*)"')
LOCAL_PATH = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*"((?:\$\{path\.(?:module|root|cwd)\}|\.\.?)/[^"]*)"\s*$', re.M)
MARKERS = (("${path.module}/", "module"), ("${path.root}/", "cwd"), ("${path.cwd}/", "cwd"))
STOP = set("\"'()[],\n\r\t ")
HOLE = "\x00"
PATH_REF = re.compile(r"\bpath\.(?:module|root|cwd)\b")
READ_CALL = re.compile(r"\b(file|templatefile|fileexists|filebase64|filebase64sha256|filebase64sha512|filemd5|filesha1|"
                       r"filesha256|filesha512|fileset)\s*\(\s*")
BUILT = re.compile(r'[A-Za-z_]\w*\s*\(|"(?:[^"\\]|\\.)*?\$\{\s*[A-Za-z_]\w*\s*\(')  # a call as the path, or one inside the literal
PY_IMPORT = re.compile(r"^\s*(?:from\s+([A-Za-z_][\w]*)|import\s+([A-Za-z_][\w]*))", re.M)


class Unknown(Exception):
    """Something this selection cannot classify: every suite runs."""


def check_reads(name: str, text: str) -> None:
    """Unknown when a read's path is built in a way this selection cannot resolve to a literal path (full-line comments
    are not code and are skipped)."""
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(("#", "//")))
    rest = code
    for marker, _ in MARKERS:
        rest = rest.replace(marker, "")
    if PATH_REF.search(rest):
        raise Unknown(f"{name}: path.module/root/cwd is used other than as an interpolated path prefix")
    for m in READ_CALL.finditer(code):
        func = m.group(1)
        if func == "fileset" or BUILT.match(code, m.end()):
            raise Unknown(f"{name}: {func}() reads a path this cannot resolve to a literal")


def suite_dir(path: str) -> str:
    """The suite a *.tftest.hcl file belongs to."""
    p = PurePosixPath(path).parent
    parts = p.parts
    if "tests" in parts:
        p = PurePosixPath(*parts[: parts.index("tests")]) if parts.index("tests") else PurePosixPath(".")
    return p.as_posix()


def _files(root: Path, pattern: str) -> list[str]:
    return sorted(f.relative_to(root).as_posix() for f in root.rglob(pattern)
                  if not SKIP_PARTS.intersection(f.relative_to(root).parts) and f.is_file())


def find_suites(root: Path) -> list[str]:
    return sorted({suite_dir(f) for f in _files(root, "*.tftest.hcl")})


def under(path: str, directory: str) -> bool:
    return directory == "." or path == directory or path.startswith(directory.rstrip("/") + "/")


def owner(path: str, units: set[str]) -> str | None:
    """The deepest unit holding `path`, or None."""
    holding = [u for u in units if under(path, u)]
    return max(holding, key=lambda u: -1 if u == "." else len(u), default=None)


def tokens(text: str) -> list[tuple[str, str]]:
    """(kind, path) for every path-like token: kind module, cwd, rel or local:<NAME>."""
    found = []
    i = 0
    while i < len(text):
        kind, start = None, i
        for marker, k in MARKERS:
            if text.startswith(marker, i):
                kind, start = k, i + len(marker)
                break
        if kind is None:
            m = re.match(r"\$\{local\.([A-Za-z_][A-Za-z0-9_-]*)\}/", text[i:])
            if m:
                kind, start = "local:" + m.group(1), i + m.end()
            elif text.startswith(("../", "./"), i) and (i == 0 or text[i - 1] in "\"'=(, \t\n"):
                kind, start = "rel", i
        if kind is None:
            i += 1
            continue
        j, depth, out = start, 0, []
        while j < len(text):
            c = text[j]
            if depth == 0 and text.startswith("${", j):
                depth, j = 1, j + 2
                out.append(HOLE)
                continue
            if depth:
                depth += {"{": 1, "}": -1}.get(c, 0)
                j += 1
                continue
            if c in STOP or c == "}":
                break
            out.append(c)
            j += 1
        path = "".join(out)
        if kind == "rel" or path:
            found.append((kind, path))
        i = max(j, i + 1)
    return found


def resolve(base: str, path: str) -> str | None:
    """The repo-relative path of `path` from folder `base` (HOLE for an interpolation); None when it leaves the repo."""
    parts = PurePosixPath(path).parts
    for n, part in enumerate(parts[:-1]):
        if HOLE in part and parts[n + 1] == "..":
            raise Unknown(f"the path {path.replace(HOLE, '${...}')!r} climbs out of an interpolation")
    joined = posixpath.normpath(posixpath.join(base, path)) if path else posixpath.normpath(base)
    if joined == ".." or joined.startswith("../") or joined.startswith("/"):
        return None
    return joined


def regex(path: str) -> re.Pattern:
    """A path and everything under it; `.` is the whole repository."""
    if path == ".":
        return re.compile(r".*", re.S)
    return re.compile(".*".join(re.escape(p) for p in path.split(HOLE)) + r"(/.*)?", re.S)


def py_closure(root: Path, path: str) -> set[str]:
    """A referenced Python file and the sibling modules it imports, transitively; its whole folder when it edits sys.path."""
    seen, todo = set(), [path]
    while todo:
        p = todo.pop()
        if p in seen:
            continue
        seen.add(p)
        f = root / p
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8")
        folder = posixpath.dirname(p)
        if "sys.path" in text:
            seen.add(folder or ".")
            continue
        for m in PY_IMPORT.finditer(text):
            name = m.group(1) or m.group(2)
            for cand in (posixpath.join(folder, name + ".py"), posixpath.join(folder, name)):
                if (root / cand).exists():
                    todo.append(cand)
    return seen


class Graph:
    def __init__(self, root: Path):
        self.root = root
        self.suites = find_suites(root)
        tf = [f for ext in TF_FILES for f in _files(root, "*" + ext)]
        self.units = {posixpath.dirname(f) or "." for f in tf if f.endswith((".tf", ".tf.json"))} | set(self.suites)
        self.sources: dict[str, set[str]] = {u: set() for u in self.units}
        self.toks: dict[str, list[tuple[str, str, str]]] = {u: [] for u in self.units}
        for f in sorted(set(tf)):
            u = owner(f, self.units)
            if u is None:
                raise Unknown(f"{f} is in no Terraform folder")
            text = (root / f).read_text(encoding="utf-8")
            fdir = posixpath.dirname(f) or "."
            check_reads(f, text)
            srcs = SOURCE.findall(text)
            for src in srcs:
                cands = {resolve(fdir, src), resolve(u, src)}
                hit = [c for c in cands if c in self.units]
                if not hit:
                    raise Unknown(f"{f}: module source {src!r} is no Terraform folder here")
                self.sources[u].update(hit)
            defs = dict(LOCAL_PATH.findall(text))
            for kind, path in tokens(text):
                if not (kind == "rel" and path in srcs):  # a module source is an edge (reaches), not a read path
                    self.toks[u].append((kind, path, fdir))
            self.toks[u].extend(("def:" + name, value, fdir) for name, value in defs.items())
        self.closure = {s: self._closure(s) for s in self.suites}

    def _closure(self, s: str) -> set[str]:
        seen, todo = set(), [s]
        while todo:
            u = todo.pop()
            if u not in seen:
                seen.add(u)
                todo.extend(self.sources.get(u, ()))
        return seen

    def reaches(self, s: str, path: str) -> bool:
        """`path` is a file of a unit suite `s` uses; another unit's tests/ folder is that unit's own (a module's tests
        never run in the suites using it), unless a path in reach names it."""
        u = owner(path, self.units)
        if u not in self.closure[s]:
            return False
        return u == s or not under(path, posixpath.join(u, "tests"))

    def _defs(self, u: str) -> dict[str, tuple[str, str, str]]:
        """Local name -> (kind, path, file folder) for locals that are one path."""
        out = {}
        for kind, value, fdir in self.toks[u]:
            if kind.startswith("def:"):
                sub = tokens('"' + value + '"')
                if len(sub) == 1:
                    out[kind[4:]] = (sub[0][0], sub[0][1], fdir)
        return out

    def _only_prefix_uses(self, u: str, name: str) -> bool:
        """Every use of local.<name> in unit u is `${local.<name>}/...`."""
        uses = prefix = 0
        for f in _files(self.root / u if u != "." else self.root, "*.tf"):
            path = f if u == "." else posixpath.join(u, f)
            if owner(path, self.units) != u:
                continue
            text = (self.root / path).read_text(encoding="utf-8")
            uses += len(re.findall(r"\blocal\." + re.escape(name) + r"\b", text))
            prefix += len(re.findall(r"\$\{local\." + re.escape(name) + r"\}/", text))
        return prefix > 0 and uses == prefix

    def patterns(self, s: str) -> list[re.Pattern]:
        out = []
        for u in sorted(self.closure[s]):
            defs = self._defs(u)
            for kind, path, fdir in self.toks[u]:
                if kind.startswith("def:"):
                    continue
                if kind.startswith("local:"):
                    name = kind[6:]
                    if name not in defs:
                        continue  # not a path local: `${local.bucket_arn}/*` and the like
                    dkind, dpath, dfdir = defs[name]
                    kind, path, fdir = dkind, posixpath.join(dpath, path) if dpath else path, dfdir
                elif any(kind == k and path == v for n, (k, v, _) in defs.items()) and any(
                        path == v and self._only_prefix_uses(u, n) for n, (k, v, _) in defs.items() if k == kind):
                    continue  # a path local used only as `${local.X}/...`: its uses are the reach
                bases = {"module": [u], "cwd": [s], "rel": [fdir, u, s]}[kind]
                for base in bases:
                    full = resolve(base, path)
                    if full is None:
                        continue
                    out.append(regex(full))
                    if HOLE not in full and full.endswith(".py"):
                        out.extend(regex(p) for p in py_closure(self.root, full) if p != full)
        return out


def caller_path(workflow_ref: str) -> str:
    """OWNER/REPO/.github/workflows/x.yml@ref -> .github/workflows/x.yml; Unknown when it is not that shape."""
    m = re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(\.github/workflows/[^@]+)@.+", workflow_ref or "")
    if not m:
        raise Unknown(f"the caller workflow {workflow_ref!r} is not OWNER/REPO/.github/workflows/<file>@<ref>")
    return m.group(1)


def select(graph: Graph, changed: list[str], event: str, base_ref: str, caller: str = "") -> list[str]:
    """The suites to run. All of them unless this is a pull request into dev and every changed file is classified."""
    suites = sorted(graph.suites)
    if event != "pull_request" or base_ref != "dev":
        return suites
    try:
        workflow = caller_path(caller)
        for p in changed:
            name = posixpath.basename(p)
            if p == workflow:
                raise Unknown(f"{p}: the workflow that runs these tests changed")
            if owner(p, graph.units) is None and (name in TF_LIKE_NAMES or name.endswith(TF_LIKE) or "tests" in PurePosixPath(p).parts[:-1]):
                raise Unknown(f"{p}: a Terraform or test file outside every Terraform folder")
        chosen = []
        for s in suites:
            pats = graph.patterns(s)
            why = next((p for p in changed if graph.reaches(s, p) or any(r.fullmatch(p) for r in pats)), None)
            if why is not None:
                chosen.append(s)
                print(f"{s}: reached by {why}", file=sys.stderr)
        return chosen
    except Unknown as e:
        print(f"every suite runs: {e}", file=sys.stderr)
        return suites


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", required=True)
    ap.add_argument("--base-ref", default="")
    ap.add_argument("--diff-against", default="", help="git ref of the base branch, e.g. origin/dev")
    ap.add_argument("--caller-workflow", default="", help="github.workflow_ref of the calling workflow")
    ap.add_argument("--root", default=".")
    args = ap.parse_args(argv)
    root = Path(args.root)
    suites = find_suites(root)
    if not suites:
        print("no *.tftest.hcl found: this check would test nothing", file=sys.stderr)
        return 1
    try:
        graph = Graph(root)
    except (Unknown, OSError, UnicodeDecodeError) as e:
        print(f"every suite runs: the Terraform folders cannot be read ({e})", file=sys.stderr)
        print("\n".join(suites))
        return 0
    changed: list[str] = []
    if args.event == "pull_request" and args.base_ref == "dev":
        try:
            if not args.diff_against:
                raise ValueError("--diff-against is required for a pull request into dev")
            out = subprocess.run(["git", "-C", str(root), "diff", "--name-only", "--no-renames", args.diff_against, "HEAD"],
                                 check=True, capture_output=True, text=True).stdout
            changed = [line for line in out.splitlines() if line]
        except (OSError, ValueError, subprocess.CalledProcessError) as e:
            print(f"every suite runs: the changed files cannot be read ({e})", file=sys.stderr)
            print("\n".join(suites))
            return 0
    for s in select(graph, changed, args.event, args.base_ref, args.caller_workflow):
        print(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
