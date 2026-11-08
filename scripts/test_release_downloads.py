"""Release-asset downloads in .github/workflows (F125): every curl of a `releases/download` URL retries with a delay,
reads and saves a cache first, and checks the pinned SHA-256 on every use; the three install steps (gitleaks, actionlint,
syft) are run for real against a local server that answers 504, serves an HTML error page or a good archive.
Run: python -m unittest discover -s scripts -p 'test_*.py'"""

import hashlib
import http.server
import io
import os
import re
import subprocess
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"
# (workflow, job, install step name, cache step name, tool, asset for X64)
TOOLS = [
    ("secret-scan.yml", "gitleaks", "Install gitleaks", "Cache the verified gitleaks", "gitleaks"),
    ("workflow-lint.yml", "actionlint", "Install actionlint", "Cache the verified actionlint", "actionlint"),
    ("release-ecs.yml", "build-image", "Install syft", "Cache the verified syft", "syft"),
]


def load(name):
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def steps_of(workflow, job):
    return load(workflow)["jobs"][job]["steps"]


def index_of(steps, prefix):
    return next(i for i, s in enumerate(steps) if s.get("name", "").startswith(prefix))


def make_archive(tool):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"#!/bin/sh\necho " + tool.encode() + b"\n"
        info = tarfile.TarInfo(tool)
        info.size, info.mode = len(data), 0o755
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class Server:
    """Answers each request from `plan` (a status code, or 'ok' for the archive, or 'html'); the last entry repeats."""

    def __init__(self, archive):
        self.plan, self.hits, outer = ["ok"], 0, self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits += 1
                step = outer.plan[min(outer.hits - 1, len(outer.plan) - 1)]
                if step == "ok":
                    body, code = archive, 200
                elif step == "html":
                    body, code = b"<html>Bad gateway page</html>", 200
                else:
                    body, code = b"upstream timeout", int(step)
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def run_install(workflow, job, install_prefix, tool, server, arch="X64", cached=None, script=None, sha=None):
    """Runs the step's real script with bash -e (as Actions does): the host is the local server, short retry wait."""
    steps = steps_of(workflow, job)
    step = steps[index_of(steps, install_prefix)]
    text = script if script is not None else step["run"]
    text = text.replace("https://github.com", server.url).replace("--retry-delay 10", "--retry-delay 1")
    with tempfile.TemporaryDirectory() as d:
        temp, work = Path(d) / "temp", Path(d) / "work"
        temp.mkdir()
        work.mkdir()
        good = hashlib.sha256(server_archive[tool]).hexdigest()
        env = {"PATH": os.environ["PATH"], "RUNNER_TEMP": str(temp), "RUNNER_ARCH": arch, "VERSION": "9.9.9",
               "SHA256_A": sha or good, "SHA256_B": sha or good}
        if cached is not None:
            cache_dir = temp / "release-tools" / tool
            cache_dir.mkdir(parents=True)
            asset = "linux_amd64" if tool != "gitleaks" else "linux_x64"
            name = f"syft_9.9.9_linux_amd64.tar.gz" if tool == "syft" else f"{tool}_9.9.9_{asset}.tar.gz"
            (cache_dir / name).write_bytes(cached)
        out = subprocess.run(["bash", "-e", "-c", text], cwd=work, env=env, capture_output=True, text=True, timeout=120)
        return out, (work / tool).exists(), sorted(p.name for p in (temp / "release-tools" / tool).glob("*")) if (temp / "release-tools" / tool).exists() else []


server_archive = {t[4]: make_archive(t[4]) for t in TOOLS}


class EveryDownloadIsResilient(unittest.TestCase):
    def test_every_release_download_curl_has_retry_cache_and_checksum(self):
        found = 0
        for path in sorted(WORKFLOWS.glob("*.yml")):
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            for job_name, job in (doc.get("jobs") or {}).items():
                steps = job.get("steps") or []
                for i, st in enumerate(steps):
                    run = st.get("run", "")
                    if "releases/download" not in run:
                        continue
                    found += 1
                    where = f"{path.name}:{job_name}:{st.get('name')}"
                    for flag in ("--retry 5", "--retry-all-errors", "--retry-delay", "--retry-max-time", "--connect-timeout", "--max-time"):
                        self.assertIn(flag, run, f"{where} lacks curl {flag}")
                    self.assertIn("sha256sum -c -", run, where)
                    self.assertNotIn("|| true", run, where)
                    self.assertNotIn("sha256sum -c - ||", run, where)
                    self.assertRegex(run, r"mv \"\$\{archive\}\.part\"", where)  # a partial download is never the archive
                    self.assertIn("::error::Could not download", run, where)
                    cache = steps[i - 1]
                    self.assertTrue(cache.get("uses", "").startswith("actions/cache@"), where + " needs the cache step right before it")
                    self.assertRegex(cache["uses"], r"@[0-9a-f]{40}$", where)
                    key = cache["with"]["key"]
                    for part in ("_VERSION", "runner.arch", "_SHA256_A", "_SHA256_B"):
                        self.assertIn(part, key, f"{where} cache key lacks {part}")
                    self.assertEqual(cache.get("if"), st.get("if"), where + " cache and install share a condition")
        self.assertGreaterEqual(found, 3)

    def test_secret_scan_inputs_and_secrets_are_unchanged(self):
        call = load("secret-scan.yml")[True]["workflow_call"]
        self.assertEqual(list(call["inputs"]), ["runner"])
        self.assertNotIn("required", call["inputs"]["runner"])
        self.assertNotIn("secrets", call)

    def test_archive_hash_pins_sit_in_the_workflow_env(self):
        for workflow, job, _, _, tool in TOOLS:
            env = load(workflow)["env"]
            pre = tool.upper()
            self.assertRegex(env[f"{pre}_SHA256_A"], r"^[0-9a-f]{64}\b")
            self.assertRegex(env[f"{pre}_SHA256_B"], r"^[0-9a-f]{64}\b")
            self.assertRegex(str(env[f"{pre}_VERSION"]), r"^\d+\.\d+\.\d+")


class InstallStepsRunForReal(unittest.TestCase):
    def setUp(self):
        self.server = Server(None)
        self.addCleanup(self.server.close)

    def go(self, row, plan, **kw):
        workflow, job, prefix, _, tool = row
        self.server.__init__(server_archive[tool]) if False else None
        self.server.plan = plan
        self.server.hits = 0
        return run_install(workflow, job, prefix, tool, self.server, **kw)

    def rows(self):
        return TOOLS

    def _server_for(self, tool):
        self.server.close()
        self.server = Server(server_archive[tool])
        self.addCleanup(self.server.close)

    def test_transient_504_is_retried_then_installs(self):
        for row in TOOLS:
            self._server_for(row[4])
            out, installed, _ = self.go(row, ["504", "504", "ok"])
            self.assertEqual(out.returncode, 0, (row[0], out.stderr))
            self.assertTrue(installed, row[0])
            self.assertEqual(self.server.hits, 3, row[0])

    def test_504_beyond_the_retry_budget_fails_naming_the_host_and_runs_nothing(self):
        for row in TOOLS:
            self._server_for(row[4])
            out, installed, left = self.go(row, ["504"])
            self.assertNotEqual(out.returncode, 0, row[0])
            self.assertIn("::error::Could not download " + row[4] + " from github.com after retries", out.stdout, row[0])
            self.assertFalse(installed, row[0])
            self.assertEqual(self.server.hits, 6, row[0])  # one try and five retries
            self.assertEqual(left, [], row[0])  # no partial file is left for the cache to save

    def test_200_with_an_html_body_fails_the_checksum_and_is_not_kept(self):
        for row in TOOLS:
            self._server_for(row[4])
            out, installed, left = self.go(row, ["html"])
            self.assertNotEqual(out.returncode, 0, row[0])
            self.assertIn("does not match its pinned SHA-256", out.stdout, row[0])
            self.assertFalse(installed, row[0])
            self.assertEqual(left, [], row[0])

    def test_a_valid_cached_archive_needs_no_download(self):
        for row in TOOLS:
            self._server_for(row[4])
            out, installed, _ = self.go(row, ["504"], cached=server_archive[row[4]])
            self.assertEqual(out.returncode, 0, (row[0], out.stdout, out.stderr))
            self.assertTrue(installed, row[0])
            self.assertEqual(self.server.hits, 0, row[0])

    def test_a_tampered_cached_archive_is_discarded_and_never_run(self):
        for row in TOOLS:
            self._server_for(row[4])
            bad = server_archive[row[4]][:-9] + b"tampered!"
            out, installed, _ = self.go(row, ["504"], cached=bad)  # the host is down too: nothing may run
            self.assertNotEqual(out.returncode, 0, row[0])
            self.assertFalse(installed, row[0])
            self.assertIn("failed its checksum; discarding", out.stdout, row[0])
            out, installed, _ = self.go(row, ["ok"], cached=bad)  # host up: discard, download, verify, install
            self.assertEqual(out.returncode, 0, (row[0], out.stdout, out.stderr))
            self.assertTrue(installed, row[0])
            self.assertEqual(self.server.hits, 1, row[0])

    def test_a_good_download_with_the_wrong_pin_never_installs(self):
        for row in TOOLS:
            self._server_for(row[4])
            out, installed, _ = self.go(row, ["ok"], sha="0" * 64)
            self.assertNotEqual(out.returncode, 0, row[0])
            self.assertFalse(installed, row[0])

    def test_an_unknown_architecture_stops_before_any_download(self):
        for row in TOOLS:
            self._server_for(row[4])
            out, installed, _ = self.go(row, ["ok"], arch="RISCV")
            self.assertNotEqual(out.returncode, 0, row[0])
            self.assertIn("Unsupported runner architecture RISCV", out.stdout, row[0])
            self.assertEqual(self.server.hits, 0, row[0])

    def test_a_binary_already_on_the_path_is_not_used_instead_of_the_verified_archive(self):
        # The pin is of the archive, so an installed binary cannot be checked against it: the step always uses ./<tool>.
        for workflow, job, prefix, _, tool in TOOLS:
            run = steps_of(workflow, job)[index_of(steps_of(workflow, job), prefix)]["run"]
            self.assertNotIn("command -v", run, workflow)
            self.assertIn(f'tar -xzf "${{archive}}" {tool}', run, workflow)


if __name__ == "__main__":
    unittest.main()
