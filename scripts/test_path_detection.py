"""Tests of job-level path detection (scripts/path_detection.py and its copies in the three check workflows; S2i-G2)."""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
import urllib.error
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
import path_detection as pd  # noqa: E402

ROOT = Path(__file__).parent.parent
WORKFLOWS = ROOT / ".github/workflows"
CHECKS = ("node-checks.yml", "python-checks.yml", "terraform-checks.yml")
WITH_FULL = {"node-checks.yml", "python-checks.yml"}
COND = "steps.paths.outputs.run != 'false'"
RUN = "cat > \"$RUNNER_TEMP/path_detection.py\" <<'PATHS_PY'\n{source}PATHS_PY\npython3 -I \"$RUNNER_TEMP/path_detection.py\"\n"
BASE = "a" * 40
HEAD = "b" * 40


def answer(*files):
    """A compare answer: a name, or (status, name, previous name)."""
    out = []
    for f in files:
        if isinstance(f, tuple):
            status, name, prev = f
            out.append({"filename": name, "status": status, "previous_filename": prev})
        else:
            out.append({"filename": f, "status": "modified"})
    return json.dumps({"status": "ahead", "files": out}).encode()


class Fetch:
    def __init__(self, body=None, error=None):
        self.body, self.error, self.calls = body, error, []

    def __call__(self, base, head):
        self.calls.append((base, head))
        if self.error:
            raise self.error
        return self.body


def pr(paths, fetch, full="false", event="pull_request", base=BASE, head=HEAD):
    return pd.decide(event, full, paths, base, head, fetch)


class Decide(unittest.TestCase):
    def test_paths_ignored_on_certification_and_full(self):
        # §9.1 / R-S2i-7: a push to dev, a workflow_dispatch run and full: true run everything, even when no file matches.
        for event, full in (("push", "false"), ("workflow_dispatch", "false"), ("push", ""), ("pull_request", "true")):
            fetch = Fetch(answer("README.md"))
            run, reason, warning = pr("src/**", fetch, full=full, event=event)
            self.assertTrue(run, (event, full, reason))
            self.assertIsNone(warning)
            self.assertEqual(fetch.calls, [], (event, full))
        # The same through each workflow's own step env, so a wiring that hides the event or `full` from the script fails.
        for name in CHECKS:
            for event, full in (("push", "false"), ("workflow_dispatch", "false"), ("pull_request", "true")):
                if full == "true" and name not in WITH_FULL:
                    continue
                out = Main.run_step(self, name, event=event, full=full, body=answer("README.md"))
                self.assertEqual(out, "run=true\n", (name, event, full))

    def test_a_pull_request_with_no_matching_file_is_skipped(self):
        fetch = Fetch(answer("README.md", "docs/a.md"))
        run, reason, warning = pr("src/**\n*.toml", fetch)
        self.assertFalse(run, reason)
        self.assertIn("none of the 2 changed files match paths", reason)
        self.assertIsNone(warning)
        self.assertEqual(fetch.calls, [(BASE, HEAD)])

    def test_a_matching_file_runs(self):
        run, reason, _ = pr("src/**\n*.toml", Fetch(answer("README.md", "pyproject.toml")))
        self.assertTrue(run)
        self.assertIn("1 of the 2 changed files match paths", reason)

    def test_empty_paths_run_everything_without_reading_the_change(self):
        for paths in ("", "\n", "  \n\t\n"):
            fetch = Fetch(answer("README.md"))
            run, reason, warning = pr(paths, fetch)
            self.assertTrue(run, repr(paths))
            self.assertIn("empty", reason)
            self.assertEqual(fetch.calls, [])

    def test_a_renamed_file_counts_by_its_old_and_its_new_name(self):
        self.assertTrue(pr("src/**", Fetch(answer(("renamed", "lib/a.py", "src/a.py"))))[0])  # moved out of src
        self.assertTrue(pr("src/**", Fetch(answer(("renamed", "src/a.py", "lib/a.py"))))[0])  # moved into src
        self.assertFalse(pr("src/**", Fetch(answer(("renamed", "lib/b.py", "lib/a.py"))))[0])

    def test_a_deleted_file_counts(self):
        self.assertTrue(pr("src/**", Fetch(answer(("removed", "src/gone.py", None))))[0])

    def test_zero_changed_files_run_everything(self):
        run, reason, _ = pr("src/**", Fetch(answer()))
        self.assertTrue(run)
        self.assertIn("no file", reason)

    def test_300_files_or_more_run_everything_299_do_not(self):
        many = [f"docs/{i}.md" for i in range(300)]
        run, reason, _ = pr("src/**", Fetch(answer(*many)))
        self.assertTrue(run, reason)
        self.assertIn("300 files", reason)
        self.assertFalse(pr("src/**", Fetch(answer(*many[:299])))[0])

    def test_a_detection_error_runs_everything_with_a_warning(self):
        bodies = (b"not json", b"\xff\xfe", b"[]", json.dumps({"files": None}).encode(), json.dumps({"x": 1}).encode(),
                  json.dumps({"files": ["a"]}).encode(), json.dumps({"files": [{"status": "modified"}]}).encode(),
                  json.dumps({"files": [{"filename": ""}]}).encode(),
                  json.dumps({"files": [{"filename": "a", "status": "renamed"}]}).encode())
        for body in bodies:
            run, reason, warning = pr("src/**", Fetch(body))
            self.assertTrue(run, body)
            self.assertIn("could not be read", reason)
            self.assertIn("everything runs", warning)
        run, _, warning = pr("src/**", Fetch(error=pd.DetectionError("the compare API answered 404")))
        self.assertTrue(run)
        self.assertIn("404", warning)

    def test_a_missing_or_odd_sha_runs_everything_without_a_call(self):
        for base, head in (("", HEAD), (BASE, ""), ("main", HEAD), (BASE, "b" * 39), (BASE.upper(), HEAD),
                           (BASE, HEAD + "/../x"), (BASE + "\n", HEAD)):
            fetch = Fetch(answer("README.md"))
            run, _, warning = pr("src/**", fetch, base=base, head=head)
            self.assertTrue(run, (base, head))
            self.assertIn("sha is missing", warning)
            self.assertEqual(fetch.calls, [])

    def test_malformed_paths_run_everything_with_a_warning(self):
        for paths in ("src/[ab].py", "src/{a,b}/**", "src\\a", "!docs/**", "/src/**", "src/", "a/***", "a\x00b", "a\x1bb", "a\tb",
                      "x" * 513, "\n".join(f"g{i}" for i in range(101)), "ok/**\n!bad"):
            fetch = Fetch(answer("README.md"))
            run, reason, warning = pr(paths, fetch)
            self.assertTrue(run, repr(paths)[:40])
            self.assertIn("malformed", reason)
            self.assertIn("malformed", warning)
            self.assertEqual(fetch.calls, [])
        self.assertFalse(pr("x" * 512, Fetch(answer("README.md")))[0])
        self.assertFalse(pr("\n".join(f"g{i}" for i in range(100)), Fetch(answer("README.md")))[0])

    def test_the_warning_never_repeats_the_glob_text(self):
        _, _, warning = pr("::error::x\n[", Fetch(answer("a")))
        self.assertNotIn("::error::x", warning)
        self.assertNotIn("[", warning)

    def test_other_events_run_everything(self):
        for event in ("pull_request_target", "merge_group", "schedule", "", "PULL_REQUEST"):
            fetch = Fetch(answer("README.md"))
            self.assertTrue(pr("src/**", fetch, event=event)[0], event)
            self.assertEqual(fetch.calls, [])

    def test_any_full_value_but_false_or_empty_runs_everything(self):
        for full in ("true", "True", "1", "yes"):
            self.assertTrue(pr("src/**", Fetch(answer("README.md")), full=full)[0], full)
        self.assertFalse(pr("src/**", Fetch(answer("README.md")), full="")[0])


class Globs(unittest.TestCase):
    def match(self, glob, path):
        return pd.matches(pd.compile_glob(glob, 1), path)

    def test_star_and_question_stop_at_a_slash(self):
        self.assertTrue(self.match("*.py", "a.py"))
        self.assertFalse(self.match("*.py", "src/a.py"))
        self.assertTrue(self.match("src/?.py", "src/a.py"))
        self.assertFalse(self.match("src/?.py", "src/ab.py"))
        self.assertFalse(self.match("src?a.py", "src/a.py"))

    def test_double_star_crosses_folders(self):
        self.assertTrue(self.match("src/**", "src/a/b/c.py"))
        self.assertFalse(self.match("src/**", "srcx/a.py"))
        self.assertTrue(self.match("**.py", "a/b/c.py"))
        self.assertTrue(self.match("a**z", "a/b/z"))

    def test_double_star_slash_is_zero_or_more_folders(self):
        for path in ("a.py", "x/a.py", "x/y/a.py"):
            self.assertTrue(self.match("**/a.py", path), path)
        self.assertTrue(self.match("src/**/a.py", "src/a.py"))
        self.assertTrue(self.match("src/**/a.py", "src/x/y/a.py"))
        self.assertFalse(self.match("src/**/a.py", "src/xa.py"))
        self.assertFalse(self.match("**/a.py", "ba.py"))
        self.assertFalse(self.match("**/a.py", "x/ba.py"))
        self.assertTrue(self.match("**/**/a.py", "a.py"))

    def test_the_whole_path_must_match(self):
        self.assertFalse(self.match("src", "src/a.py"))
        self.assertFalse(self.match("a.py", "a.pyc"))
        self.assertFalse(self.match("b.py", "ab.py"))
        self.assertTrue(self.match("src", "src"))

    def test_shell_and_odd_characters_are_literal(self):
        for glob in ("$(touch x)", "`id`", "a; rm -rf b", "a | b", "a && b", "it's \"q\"", "my file.txt", "$HOME/*", "%25", "ünï/*.py"):
            tokens = pd.compile_glob(glob, 1)
            literal = glob.replace("*", "x")
            self.assertTrue(pd.matches(tokens, literal), glob)
            self.assertFalse(pd.matches(tokens, literal + "/!"), glob)

    def test_a_hostile_glob_is_never_run_by_a_shell(self):
        with tempfile.TemporaryDirectory() as d:
            mark = Path(d) / "pwned"
            env = {"PATH": os.environ.get("PATH", ""), "EVENT_NAME": "pull_request", "FULL": "false",
                   "PATHS": f"$(touch {mark})\n`touch {mark}`\nx; touch {mark}", "BASE_SHA": BASE, "HEAD_SHA": HEAD,
                   "GITHUB_API_URL": "http://127.0.0.1:9", "GITHUB_REPOSITORY": "OnePeek/x", "GH_TOKEN": "t"}
            p = subprocess.run([sys.executable, "-I", "-B", str(Path(pd.__file__))], env=env, capture_output=True, text=True,
                               timeout=30)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertFalse(mark.exists())

    def test_matching_is_linear_on_a_hostile_glob(self):
        tokens = pd.compile_glob("*a" * 200 + "b", 1)
        start = time.monotonic()
        self.assertFalse(pd.matches(tokens, "a" * 400))
        self.assertLess(time.monotonic() - start, 5.0)


class FetchCompare(unittest.TestCase):
    def test_odd_settings_are_refused_without_a_call(self):
        with unittest.mock.patch.object(pd.urllib.request, "build_opener") as opener:
            for api, repo, token in (("http://api.github.com", "OnePeek/x", "t"), ("https://api.github.com", "x", "t"),
                                     ("https://api.github.com", "OnePeek/x/../y", "t"), ("https://api.github.com", "OnePeek/x", ""),
                                     ("", "OnePeek/x", "t")):
                with self.assertRaises(pd.DetectionError):
                    pd.fetch_compare(api, repo, BASE, HEAD, token)
            opener.assert_not_called()

    def opened(self, response=None, error=None):
        class Resp(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        opener = unittest.mock.Mock()
        if error:
            opener.open.side_effect = error
        else:
            opener.open.return_value = response if response is not None else Resp(answer("a"))
        return opener, Resp

    def test_the_request_compares_base_to_head_with_a_bearer_token_and_no_redirects(self):
        opener, _ = self.opened()
        with unittest.mock.patch.object(pd.urllib.request, "build_opener", return_value=opener) as build:
            self.assertEqual(pd.fetch_compare("https://api.github.com/", "OnePeek/x", BASE, HEAD, "tok"), answer("a"))
        build.assert_called_once_with(pd._NoRedirect)
        req = opener.open.call_args[0][0]
        self.assertEqual(req.full_url, f"https://api.github.com/repos/OnePeek/x/compare/{BASE}...{HEAD}?per_page=1")
        self.assertEqual(req.get_header("Authorization"), "Bearer tok")
        self.assertIsNone(pd._NoRedirect().redirect_request(req, None, 302, "Found", {}, "https://elsewhere"))

    def test_http_and_network_errors_are_detection_errors(self):
        for error in (urllib.error.HTTPError("u", 404, "nf", {}, None), urllib.error.HTTPError("u", 302, "f", {}, None),
                      urllib.error.URLError("down"), TimeoutError(), ConnectionResetError()):
            opener, _ = self.opened(error=error)
            with unittest.mock.patch.object(pd.urllib.request, "build_opener", return_value=opener):
                with self.assertRaises(pd.DetectionError, msg=repr(error)):
                    pd.fetch_compare("https://api.github.com", "OnePeek/x", BASE, HEAD, "t")

    def test_a_non_200_or_too_large_answer_is_a_detection_error(self):
        _, Resp = self.opened()
        odd = Resp(answer("a"))
        odd.status = 204
        opener, _ = self.opened(response=odd)
        with unittest.mock.patch.object(pd.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(pd.DetectionError):
                pd.fetch_compare("https://api.github.com", "OnePeek/x", BASE, HEAD, "t")
        opener, _ = self.opened(response=Resp(b"x" * 11))
        with unittest.mock.patch.object(pd.urllib.request, "build_opener", return_value=opener), \
                unittest.mock.patch.object(pd, "MAX_BODY", 10):
            with self.assertRaises(pd.DetectionError):
                pd.fetch_compare("https://api.github.com", "OnePeek/x", BASE, HEAD, "t")


class Main(unittest.TestCase):
    def run_step(self, name, event="pull_request", full="false", paths="src/**", body=None, error=None, base=BASE, head=HEAD):
        """Run main() with the env the workflow's own step gives it (its expressions filled in); returns GITHUB_OUTPUT."""
        step = yaml.safe_load((WORKFLOWS / name).read_text())["jobs"]["checks"]["steps"][0]
        values = {"${{ inputs.paths }}": paths, "${{ github.event_name }}": event, "${{ inputs.full }}": full,
                  "${{ github.event.pull_request.base.sha }}": base if event == "pull_request" else "",
                  "${{ github.event.pull_request.head.sha }}": head if event == "pull_request" else "",
                  "${{ github.token }}": "tok"}
        with tempfile.TemporaryDirectory() as d:
            out, summary = Path(d) / "out", Path(d) / "summary"
            env = {k: values[v] for k, v in step["env"].items()}
            env.update({"GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(summary), "GITHUB_API_URL": "https://api.github.com",
                        "GITHUB_REPOSITORY": "OnePeek/x"})
            seen = []

            def fake(api, repo, b, h, token):
                seen.append((api, repo, b, h, token))
                if error:
                    raise error
                return body

            with unittest.mock.patch.object(pd, "fetch_compare", fake), \
                    unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(pd.main(env), 0)
            if seen:
                self.assertEqual(seen, [("https://api.github.com", "OnePeek/x", BASE, HEAD, "tok")])
            self.last_summary = summary.read_text() if summary.exists() else ""
            return out.read_text() if out.exists() else ""

    def test_each_workflow_step_skips_on_no_match_and_says_why(self):
        for name in CHECKS:
            self.assertEqual(self.run_step(name, body=answer("README.md")), "run=false\n", name)
            self.assertIn("none of the 1 changed files match paths, so the checks were skipped", self.last_summary)
            self.assertEqual(self.run_step(name, body=answer("src/a.py")), "run=true\n", name)
            self.assertEqual(self.run_step(name, error=pd.DetectionError("x")), "run=true\n", name)

    def test_an_unexpected_error_says_run(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out"
            with unittest.mock.patch.object(pd, "decide", side_effect=KeyError("x")), \
                    unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                self.assertEqual(pd.main({"GITHUB_OUTPUT": str(out)}), 0)
            self.assertEqual(out.read_text(), "run=true\n")
            self.assertIn("::warning::path detection failed (KeyError)", stdout.getvalue())

    def test_an_unwritable_output_leaves_no_run_false(self):
        with tempfile.TemporaryDirectory() as d:
            with unittest.mock.patch("sys.stdout", new_callable=io.StringIO), \
                    unittest.mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                self.assertEqual(pd.main({"EVENT_NAME": "push", "GITHUB_OUTPUT": d}), 0)  # a folder: not writable as a file
            self.assertIn("GITHUB_OUTPUT is not writable", err.getvalue())

    def test_the_script_exits_0_even_when_main_raises(self):
        p = subprocess.run([sys.executable, "-I", "-B", "-c",
                            "import os, runpy\nos.environ.__class__.get = lambda *a: 1/0\n"
                            f"runpy.run_path({str(Path(pd.__file__))!r}, run_name='__main__')"],
                           env={"PATH": os.environ.get("PATH", "")}, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)


class Workflows(unittest.TestCase):
    def doc(self, name):
        return yaml.safe_load((WORKFLOWS / name).read_text())

    def test_each_workflow_takes_an_optional_string_paths_input(self):
        for name in CHECKS:
            spec = self.doc(name)[True]["workflow_call"]["inputs"]["paths"]
            self.assertEqual(spec["type"], "string", name)
            self.assertEqual(spec["default"], "", name)
            self.assertNotIn("required", spec, name)

    def test_the_first_step_runs_this_exact_script_and_never_fails_the_job(self):
        run = RUN.format(source=(Path(__file__).parent / "path_detection.py").read_text())
        for name in CHECKS:
            step = self.doc(name)["jobs"]["checks"]["steps"][0]
            self.assertEqual(step["id"], "paths", name)
            self.assertEqual(step["if"], "inputs.paths != ''", name)
            self.assertIs(step["continue-on-error"], True, name)
            self.assertEqual(step["run"], run, name)
            env = {"PATHS": "${{ inputs.paths }}", "EVENT_NAME": "${{ github.event_name }}",
                   "BASE_SHA": "${{ github.event.pull_request.base.sha }}", "HEAD_SHA": "${{ github.event.pull_request.head.sha }}",
                   "GH_TOKEN": "${{ github.token }}"}
            if name in WITH_FULL:
                env["FULL"] = "${{ inputs.full }}"
            self.assertEqual(step["env"], env, name)

    def test_every_later_step_but_job_usage_runs_unless_detection_said_false(self):
        for name in CHECKS:
            steps = self.doc(name)["jobs"]["checks"]["steps"]
            last = steps[-1]
            self.assertTrue(str(last.get("name", "")).startswith("Job usage"), name)
            self.assertEqual(last["if"], "${{ always() && runner.environment != 'github-hosted' }}", name)
            for step in steps[1:-1]:
                where = f"{name}: {step.get('name') or step.get('uses')}"
                cond = str(step.get("if", ""))
                self.assertTrue(cond == COND or cond.endswith(" && " + COND), where)
                self.assertNotIn("always()", cond, where)
                self.assertNotIn("== 'true'", cond, where)

    def test_the_node_build_keeps_its_full_condition(self):
        steps = self.doc("node-checks.yml")["jobs"]["checks"]["steps"]
        build = next(s for s in steps if s.get("name") == "Production build (release only)")
        self.assertEqual(build["if"], f"inputs.full && {COND}")

    def test_paths_reaches_a_run_only_through_env_and_the_script_holds_no_expression(self):
        source = (Path(__file__).parent / "path_detection.py").read_text()
        self.assertNotIn("${{", source)
        for name in CHECKS:
            for step in self.doc(name)["jobs"]["checks"]["steps"]:
                self.assertNotIn("inputs.paths", str(step.get("run", "")), name)


if __name__ == "__main__":
    unittest.main()
