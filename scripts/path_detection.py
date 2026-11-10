"""Job-level path detection: does this pull request change a file the caller's `paths` globs name? (delivery-pipelines §9.1)

The first step of node-checks.yml, python-checks.yml and terraform-checks.yml runs a verbatim copy of this file (a test
keeps the copies identical) when the caller passes `paths`. It writes `run=true` or `run=false` to $GITHUB_OUTPUT; the
job's later steps run unless the output is exactly `false`, so the job always reports (green when skipped here).

`paths` applies only to a `pull_request` run; the changed files are those of the pull request, compared from its base
sha (`GET /repos/<repo>/compare/<base sha>...<head sha>`, contents: read). It says run (fail closed) on any other event
(a push to dev, a workflow_dispatch run: certification), when `full` is true, when `paths` is empty, when `paths` is
malformed (with a warning), when the shas are missing or the API or its answer fails, when the change lists no file,
and when it lists 300 files or more (the API's cap: the list may be cut short). A renamed file counts by its old and its
new name, a deleted file by its name.

Globs, one per line (blank lines ignored, each line trimmed): `*` any characters but `/`, `?` one character but `/`,
`**` any characters, `**/` zero or more folders; everything else is literal (spaces, `$`, `;`, quotes and the rest are
plain characters: a glob is never given to a shell). `[`, `]`, `{`, `}`, `\\`, a leading `!` or `/`, a trailing `/`,
`***`, a control character, a glob over 512 characters or more than 100 globs make `paths` malformed.
It never fails the job: every error says run, and it always exits 0.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

FILE_CAP = 300  # the compare API lists at most 300 files: a list that long may be cut short
MAX_GLOBS = 100
MAX_GLOB_LEN = 512
MAX_BODY = 64 * 1024 * 1024  # bytes read from the API answer
TIMEOUT = 20  # seconds per request
SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
REFUSED = set("[]{}\\")


class Malformed(ValueError):
    pass


class DetectionError(Exception):
    pass


def parse(text):
    """The globs of a `paths` input as token lists; [] when it is empty. Raises Malformed."""
    lines = [line.strip() for line in text.splitlines()]
    globs = [line for line in lines if line]
    if len(globs) > MAX_GLOBS:
        raise Malformed(f"more than {MAX_GLOBS} globs")
    return [compile_glob(g, n) for n, g in enumerate(globs, 1)]


def compile_glob(glob, n):
    """Tokens: ("lit", c), ("one",), ("star",), ("any",), ("dirs",)."""
    if len(glob) > MAX_GLOB_LEN:
        raise Malformed(f"glob {n} is longer than {MAX_GLOB_LEN} characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in glob):
        raise Malformed(f"glob {n} holds a control character")
    if glob[0] in "!/" or glob.endswith("/"):
        raise Malformed(f"glob {n} starts with ! or / or ends with /")
    if any(c in REFUSED for c in glob) or "***" in glob:
        raise Malformed(f"glob {n} uses an unsupported pattern character")
    tokens, i = [], 0
    while i < len(glob):
        if glob.startswith("**/", i):
            tokens.append(("dirs",))
            i += 3
        elif glob.startswith("**", i):
            tokens.append(("any",))
            i += 2
        elif glob[i] == "*":
            tokens.append(("star",))
            i += 1
        elif glob[i] == "?":
            tokens.append(("one",))
            i += 1
        else:
            tokens.append(("lit", glob[i]))
            i += 1
    return tokens


def _closure(tokens, states):
    """Add the states reached without a character: past a `*` or `**`, and past a `**/` only before it took one."""
    out, todo = set(states), list(states)
    while todo:
        i, inside = todo.pop()
        if i < len(tokens) and not inside and tokens[i][0] in ("star", "any", "dirs") and (i + 1, False) not in out:
            out.add((i + 1, False))
            todo.append((i + 1, False))
    return out


def matches(tokens, path):
    """True when the whole path matches the glob (a set-of-states walk: linear, no backtracking).

    A state is (token index, inside): inside is True once a `**/` has taken a character, so it can end only at a `/`."""
    states = _closure(tokens, {(0, False)})
    for c in path:
        nxt = set()
        for i, inside in states:
            if i == len(tokens):
                continue
            kind = tokens[i][0]
            if kind == "lit" and tokens[i][1] == c or kind == "one" and c != "/":
                nxt.add((i + 1, False))
            elif kind == "star" and c != "/" or kind == "any":
                nxt.add((i, False))
            elif kind == "dirs":
                nxt.add((i, True))
                if c == "/":
                    nxt.add((i + 1, False))
        states = _closure(tokens, nxt)
        if not states:
            return False
    return (len(tokens), False) in states


def changed_files(body):
    """The file names a compare answer lists (old and new name of a rename). Raises DetectionError."""
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError) as e:
        raise DetectionError(f"the compare answer is not JSON ({type(e).__name__})")
    files = doc.get("files") if isinstance(doc, dict) else None
    if not isinstance(files, list):
        raise DetectionError("the compare answer has no files list")
    names = []
    for f in files:
        if not isinstance(f, dict) or not isinstance(f.get("filename"), str) or not f["filename"]:
            raise DetectionError("a compare entry has no filename")
        names.append(f["filename"])
        if f.get("status") == "renamed":
            if not isinstance(f.get("previous_filename"), str) or not f["previous_filename"]:
                raise DetectionError("a renamed entry has no previous_filename")
            names.append(f["previous_filename"])
    return len(files), names


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch_compare(api, repo, base, head, token):
    """The compare answer's bytes. Raises DetectionError."""
    if not api.startswith("https://") or not REPO.fullmatch(repo or "") or not token:
        raise DetectionError("GITHUB_API_URL, GITHUB_REPOSITORY or the token is missing or odd")
    url = f"{api.rstrip('/')}/repos/{repo}/compare/{base}...{head}?per_page=1"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "onepeek-path-detection"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=TIMEOUT) as r:
            if r.status != 200:
                raise DetectionError(f"the compare API answered {r.status}")
            body = r.read(MAX_BODY + 1)
    except urllib.error.HTTPError as e:
        raise DetectionError(f"the compare API answered {e.code}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise DetectionError(f"the compare API could not be read ({type(e).__name__})")
    if len(body) > MAX_BODY:
        raise DetectionError("the compare answer is too large")
    return body


def decide(event, full, paths, base, head, fetch):
    """(run, reason, warning or None). `fetch(base, head)` returns the compare answer's bytes."""
    if event != "pull_request":
        return True, f"paths is ignored on a {event or 'unknown'} run (certification and every other event run everything)", None
    if full not in ("", "false"):
        return True, "paths is ignored when full is true", None
    try:
        globs = parse(paths)
    except Malformed as e:
        return True, "paths is malformed, so everything runs", f"paths is malformed ({e}): everything runs"
    if not globs:
        return True, "paths is empty, so everything runs", None
    if not SHA.fullmatch(base or "") or not SHA.fullmatch(head or ""):
        return True, "the pull request's base or head sha is missing, so everything runs", \
            "path detection: the base or head sha is missing: everything runs"
    try:
        count, names = changed_files(fetch(base, head))
    except DetectionError as e:
        return True, "the changed files could not be read, so everything runs", f"path detection failed ({e}): everything runs"
    if count == 0:
        return True, "the change lists no file, so everything runs", None
    if count >= FILE_CAP:
        return True, f"the change lists {count} files (the API lists at most {FILE_CAP}), so everything runs", None
    hits = sum(1 for name in names if any(matches(g, name) for g in globs))
    if hits:
        return True, f"{hits} of the {count} changed files match paths", None
    return False, f"none of the {count} changed files match paths, so the checks were skipped (the job passes)", None


def main(env=None):
    env = os.environ if env is None else env
    run, reason, warning = True, "path detection did not finish, so everything runs", None
    try:
        run, reason, warning = decide(
            env.get("EVENT_NAME", ""), env.get("FULL", ""), env.get("PATHS", ""), env.get("BASE_SHA", ""), env.get("HEAD_SHA", ""),
            lambda b, h: fetch_compare(env.get("GITHUB_API_URL", ""), env.get("GITHUB_REPOSITORY", ""), b, h, env.get("GH_TOKEN", "")))
    except Exception as e:
        run, reason, warning = True, "path detection failed, so everything runs", f"path detection failed ({type(e).__name__}): everything runs"
    if warning:
        print(f"::warning::{warning}", flush=True)
    print(f"path detection: {reason}", flush=True)
    for name, line in (("GITHUB_STEP_SUMMARY", f"Path detection: {reason}.\n"), ("GITHUB_OUTPUT", f"run={'true' if run else 'false'}\n")):
        if env.get(name):
            try:
                with open(env[name], "a", encoding="utf-8") as f:
                    f.write(line)
            except (OSError, ValueError):
                print(f"path detection: {name} is not writable", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    sys.exit(0)
