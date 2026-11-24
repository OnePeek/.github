"""Change scope: what kind of change is this pull request? (PROC-CI-SCOPE, founder 2026-11-24: "only what changed reruns")

change-scope.yml runs this once per pull request, before the heavy jobs; the caller gates them on its two outputs:

  code   `true` when the change touches any file that is not "light" (markdown, docs/**, LICENSE); architecture-style
         checks (lint, type, tests) run only then. `.env.example` is code: a test reads it (backend-service#214).
  image  `true` when the change touches a file the `image` globs name (Dockerfile, dependency files, shipped app code; empty `image` means the image is always built: fail safe);
         the image build and boot run only then.

A docs-only change therefore runs the minimal set (the caller's fast job: secret scan, pins); a `.env.example`-only change
runs the tests but not the image build.
Fail safe, like path_detection.py (whose glob rules and compare call it reuses): any other event than `pull_request`,
`full` true (a release pull request), empty `light`, malformed globs, missing shas, an API failure, an empty change or a
change of 300 files or more says `code=true image=true`; an unknown path is not light, so it counts as code. It never
fails the job and always exits 0.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import path_detection as pd  # noqa: E402


def classify(event, full, light, image, base, head, fetch):
    """(code, image, reason, warning or None). `fetch(base, head)` returns the compare answer's bytes."""
    everything = (True, True)
    if event != "pull_request":
        return (*everything, f"a {event or 'unknown'} run runs everything", None)
    if full not in ("", "false"):
        return (*everything, "full is true, so everything runs", None)
    try:
        light_globs, image_globs = pd.parse(light), pd.parse(image)
    except pd.Malformed as e:
        return (*everything, "light or image is malformed, so everything runs", f"change scope: a glob list is malformed ({e}): everything runs")
    if not light_globs:
        return (*everything, "light is empty, so everything runs", None)
    if not pd.SHA.fullmatch(base or "") or not pd.SHA.fullmatch(head or ""):
        return (*everything, "the base or head sha is missing, so everything runs", "change scope: the base or head sha is missing: everything runs")
    try:
        count, names = pd.changed_files(fetch(base, head))
    except pd.DetectionError as e:
        return (*everything, "the changed files could not be read, so everything runs", f"change scope failed ({e}): everything runs")
    if count == 0:
        return (*everything, "the change lists no file, so everything runs", None)
    if count >= pd.FILE_CAP:
        return (*everything, f"the change lists {count} files (the API lists at most {pd.FILE_CAP}), so everything runs", None)
    code = any(not any(pd.matches(g, n) for g in light_globs) for n in names)
    img = not image_globs or any(any(pd.matches(g, n) for g in image_globs) for n in names)  # no image globs: always build
    return code, img, f"{count} changed files: code={'true' if code else 'false'}, image={'true' if img else 'false'}", None


def main(env=None):
    env = os.environ if env is None else env
    code, img, reason, warning = True, True, "change scope did not finish, so everything runs", None
    try:
        code, img, reason, warning = classify(
            env.get("EVENT_NAME", ""), env.get("FULL", ""), env.get("LIGHT", ""), env.get("IMAGE", ""),
            env.get("BASE_SHA", ""), env.get("HEAD_SHA", ""),
            lambda b, h: pd.fetch_compare(env.get("GITHUB_API_URL", ""), env.get("GITHUB_REPOSITORY", ""), b, h, env.get("GH_TOKEN", "")))
    except Exception as e:
        code, img, reason, warning = True, True, "change scope failed, so everything runs", f"change scope failed ({type(e).__name__}): everything runs"
    if warning:
        print(f"::warning::{warning}", flush=True)
    print(f"change scope: {reason}", flush=True)
    out = f"code={'true' if code else 'false'}\nimage={'true' if img else 'false'}\n"
    for name, line in (("GITHUB_STEP_SUMMARY", f"Change scope: {reason}.\n"), ("GITHUB_OUTPUT", out)):
        if env.get(name):
            try:
                with open(env[name], "a", encoding="utf-8") as f:
                    f.write(line)
            except (OSError, ValueError):
                print(f"change scope: {name} is not writable", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    sys.exit(0)
