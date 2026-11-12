import contextlib
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
import check_runner_labels as labels  # noqa: E402
import job_usage as ju  # noqa: E402

ROOT = Path(__file__).parent.parent
WORKFLOWS = ROOT / ".github/workflows"
# Jobs on our runners that do not measure: pins.yml, the release paths (release-*, deploy-ecs, operate: the stateful
# on-demand class, not sized by S0) and spot-rerun (a workflow_run follow-up).
NOT_MEASURED = {"pins.yml", "release-ecs.yml", "release-lock.yml", "release-tag.yml", "deploy-ecs.yml", "operate.yml",
                "spot-rerun.yml"}
STEP_IF = "${{ always() && runner.environment != 'github-hosted' }}"
# The step's run: a verbatim copy of scripts/job_usage.py, written to the runner's temp folder and run isolated.
RUN = "cat > \"$RUNNER_TEMP/job_usage.py\" <<'PY'\n{source}PY\npython3 -I \"$RUNNER_TEMP/job_usage.py\"\n"
DEAD = "http://127.0.0.1:9"  # nothing listens on the discard port: refused at once


def tick():
    return os.sysconf("SC_CLK_TCK")


class Fake:
    """A /proc and /sys/fs/cgroup tree in a temp folder."""

    def __init__(self, d):
        self.proc, self.sysfs = Path(d) / "proc", Path(d) / "cg"
        (self.proc / "self").mkdir(parents=True)
        self.sysfs.mkdir()

    def write(self, rel, text, base=None):
        p = (base or self.sysfs) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def v2(self, path="/system.slice/runner.service", usage_usec="12500000", peak="734003200"):
        self.write("self/cgroup", f"0::{path}\n", self.proc)
        if usage_usec is not None:
            self.write(f"{path.lstrip('/')}/cpu.stat", f"usage_usec {usage_usec}\nuser_usec 1\nsystem_usec 2\n")
        if peak is not None:
            self.write(f"{path.lstrip('/')}/memory.peak", f"{peak}\n")

    def procs(self, btime=1000, chain=((300, "python3", 200, 900), (200, "bash", 100, 800), (100, ju.WORKER, 1, 500))):
        self.write("stat", f"cpu  100 0 50 1000 0 0 0 0 0 0\nbtime {btime}\n", self.proc)
        for pid, comm, ppid, start in chain:
            rest = " ".join(["S", str(ppid)] + ["0"] * 17 + [str(start)] + ["0"] * 10)
            self.write(f"{pid}/stat", f"{pid} ({comm}) {rest}\n", self.proc)

    def collect(self, env=None, now=1100.0, pid=300, imds=DEAD):
        return ju.collect(env or {}, proc=str(self.proc), sysfs=str(self.sysfs), imds=imds, now=now, pid=pid)



class Imds(http.server.BaseHTTPRequestHandler):
    mode = "ok"

    def log_message(self, *args):
        pass

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):
        if self.mode == "hang":
            time.sleep(3)
        if self.mode == "denied":
            return self._send(403, b"")
        self._send(200, b"tok")

    def do_GET(self):
        if self.headers.get("X-aws-ec2-metadata-token") != "tok":
            return self._send(401, b"")
        self._send(200, b"odd value!" if self.mode == "odd" else b"c7g.large")


@contextlib.contextmanager
def imds(mode):
    handler = type("H", (Imds,), {"mode": mode})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


class Readings(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.f = Fake(self.d.name)

    def tearDown(self):
        self.d.cleanup()

    def test_cgroup_v2_totals_and_wall_from_the_runner_worker(self):
        self.f.v2()
        self.f.procs()
        r = self.f.collect()
        self.assertEqual((r["cpu_s"], r["cpu_source"]), (12.5, "cgroup"))
        self.assertEqual((r["mem_peak_bytes"], r["mem_source"]), (734003200, "cgroup"))
        self.assertEqual(r["wall_s"], round(1100 - (1000 + 500 / tick()), 1))
        self.assertEqual(r["avg_vcpu"], round(12.5 / r["wall_s"], 3))
        self.assertEqual(r["cgroup"], "/system.slice/runner.service")

    def test_cgroup_v1_files_are_the_fallback(self):
        self.f.write("self/cgroup", "4:memory:/runner\n3:cpu,cpuacct:/runner\n1:name=systemd:/runner\n", self.f.proc)
        self.f.write("cpuacct/runner/cpuacct.usage", "3000000000\n")
        self.f.write("memory/runner/memory.max_usage_in_bytes", "2048\n")
        r = self.f.collect()
        self.assertEqual((r["cpu_s"], r["cpu_source"]), (3.0, "cgroup_v1"))
        self.assertEqual((r["mem_peak_bytes"], r["mem_source"]), (2048, "cgroup_v1"))

    def test_a_missing_cgroup_v2_file_falls_back_to_proc_stat_and_memory_is_unavailable(self):
        self.f.v2(usage_usec=None, peak=None)
        self.f.procs()
        r = self.f.collect()
        self.assertEqual((r["cpu_s"], r["cpu_source"]), (round(150 / tick(), 3), "proc_stat_since_boot"))
        self.assertEqual((r["mem_peak_bytes"], r["mem_source"]), (ju.NA, ju.NA))

    def test_malformed_files_read_as_unavailable(self):
        self.f.v2(usage_usec="lots", peak="-5")
        self.f.write("stat", "cpu  a b c\nbtime soon\n", self.f.proc)
        self.f.write("300/stat", "300 (python3 S 200\n", self.f.proc)
        r = self.f.collect()
        for key in ("cpu_s", "cpu_source", "mem_peak_bytes", "mem_source", "wall_s", "avg_vcpu"):
            self.assertEqual(r[key], ju.NA, key)

    def test_binary_garbage_reads_as_unavailable(self):
        self.f.v2()
        (self.f.sysfs / "system.slice/runner.service/memory.peak").write_bytes(b"\xff\xfe\x00")
        self.assertEqual(self.f.collect()["mem_peak_bytes"], ju.NA)

    @unittest.skipIf(os.geteuid() == 0, "root reads a mode-000 file")
    def test_an_unreadable_file_is_unavailable(self):
        self.f.v2()
        p = self.f.sysfs / "system.slice/runner.service/cpu.stat"
        p.chmod(0)
        try:
            self.assertEqual(ju.cpu_seconds(str(self.f.proc), str(self.f.sysfs), ju.cgroups(str(self.f.proc))), (ju.NA, ju.NA))
        finally:
            p.chmod(0o644)

    def test_no_proc_at_all_is_unavailable_everywhere(self):
        r = ju.collect({}, proc=str(Path(self.d.name) / "none"), sysfs=str(Path(self.d.name) / "none"), imds=DEAD, now=5.0, pid=1)
        for key in ("cpu_s", "mem_peak_bytes", "wall_s", "avg_vcpu", "cgroup"):
            self.assertEqual(r[key], ju.NA, key)

    def test_a_comm_with_spaces_and_parens_is_parsed_from_the_last_paren(self):
        self.f.v2()
        self.f.procs(chain=((300, "a) (b", 100, 900), (100, ju.WORKER, 1, 500)))
        self.assertNotEqual(self.f.collect()["wall_s"], ju.NA)

    def test_no_runner_worker_ancestor_means_wall_unavailable(self):
        self.f.v2()
        self.f.procs(chain=((300, "python3", 200, 900), (200, "bash", 1, 800), (1, "systemd", 0, 1)))
        r = self.f.collect()
        self.assertEqual((r["wall_s"], r["avg_vcpu"]), (ju.NA, ju.NA))

    def test_a_parent_loop_ends(self):
        self.f.v2()
        self.f.procs(chain=((300, "a", 200, 900), (200, "b", 300, 800)))
        self.assertEqual(self.f.collect()["wall_s"], ju.NA)

    def test_disk_used_reads_both_filesystems(self):
        r = self.f.collect(env={"GITHUB_WORKSPACE": self.d.name})
        self.assertIsInstance(r["disk_used_root_bytes"], int)
        self.assertIsInstance(r["disk_used_workspace_bytes"], int)
        self.assertEqual(self.f.collect(env={"GITHUB_WORKSPACE": "/no/such/dir"})["disk_used_workspace_bytes"], ju.NA)
        self.assertEqual(self.f.collect()["disk_used_workspace_bytes"], ju.NA)

    def test_runner_fields_come_from_the_env(self):
        env = {"GITHUB_REPOSITORY": "OnePeek/x", "GITHUB_JOB": "checks", "RUNNER_CLASS": "fast", "RUNNER_INPUT": "onepeek",
               "RUNNER_NAME": "r-1"}
        r = self.f.collect(env=env)
        self.assertEqual([r[k] for k in ("repo", "job", "runner_class", "runner_input", "runner_name")],
                         ["OnePeek/x", "checks", "fast", "onepeek", "r-1"])


class InstanceType(unittest.TestCase):
    def test_imdsv2_answer(self):
        with imds("ok") as url:
            self.assertEqual(ju.instance_type(url), "c7g.large")

    def test_a_hung_imds_is_unknown_within_the_budget(self):
        with imds("hang") as url:
            start = time.monotonic()
            self.assertEqual(ju.instance_type(url), "unknown")
            self.assertLess(time.monotonic() - start, ju.IMDS_BUDGET + 0.3)

    def test_denied_is_unknown(self):
        with imds("denied") as url:
            self.assertEqual(ju.instance_type(url), "unknown")

    def test_an_odd_value_is_unknown(self):
        with imds("odd") as url:
            self.assertEqual(ju.instance_type(url), "unknown")

    def test_no_imds_is_unknown(self):
        self.assertEqual(ju.instance_type(DEAD), "unknown")

    def test_a_proxy_setting_is_ignored(self):
        with imds("ok") as url, unittest.mock.patch.dict(os.environ, {"http_proxy": DEAD, "HTTP_PROXY": DEAD, "no_proxy": ""}):
            self.assertEqual(ju.instance_type(url), "c7g.large")


class Main(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.f = Fake(self.d.name)
        self.f.v2()
        self.f.procs()

    def tearDown(self):
        self.d.cleanup()

    def run_main(self, env, imds=DEAD):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = ju.main(env, proc=str(self.f.proc), sysfs=str(self.f.sysfs), imds=imds, now=1100.0, pid=300)
        return rc, out.getvalue(), err.getvalue()

    def test_one_notice_line_and_one_summary_line(self):
        summary = Path(self.d.name) / "summary.md"
        rc, out, _ = self.run_main({"GITHUB_STEP_SUMMARY": str(summary), "RUNNER_NAME": "100%"})
        self.assertEqual(rc, 0)
        lines = out.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("::notice::job-usage {"))
        self.assertIn('"runner_name":"100%25"', lines[0])  # a workflow command escapes %
        record = json.loads(summary.read_text())
        self.assertEqual(record["runner_name"], "100%")
        self.assertEqual(json.loads(lines[0][len("::notice::job-usage "):].replace("%25", "%")), record)
        self.assertEqual(summary.read_text().count("\n"), 1)

    def test_an_unwritable_summary_still_prints_the_notice_and_exits_0(self):
        rc, out, err = self.run_main({"GITHUB_STEP_SUMMARY": self.d.name})  # a directory: open() fails
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("::notice::job-usage {"))
        self.assertIn("not writable", err)

    def test_a_github_hosted_runner_prints_nothing(self):
        summary = Path(self.d.name) / "summary.md"
        rc, out, _ = self.run_main({"RUNNER_ENVIRONMENT": "github-hosted", "GITHUB_STEP_SUMMARY": str(summary)})
        self.assertEqual((rc, out, summary.exists()), (0, "", False))

    def test_a_hung_imds_keeps_the_whole_run_under_2_seconds(self):
        with imds("hang") as url:
            start = time.monotonic()
            rc, out, _ = self.run_main({}, imds=url)
            self.assertLess(time.monotonic() - start, 2.0)
        self.assertEqual(rc, 0)
        self.assertIn('"instance_type":"unknown"', out)

    def test_the_script_exits_0_even_when_main_raises(self):
        import subprocess
        env = {k: v for k, v in os.environ.items() if k != "RUNNER_ENVIRONMENT"}
        # os.getpid raising inside the run stands for any unexpected error in collect().
        p = subprocess.run([sys.executable, "-I", "-B", "-c",
                            "import os, runpy\nos.getpid = lambda: 1/0\n"
                            f"runpy.run_path({str(Path(ju.__file__))!r}, run_name='__main__')"],
                           env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual((p.returncode, p.stdout), (0, ""), p.stderr)


class Workflows(unittest.TestCase):
    """Every shared job that can run on our runners ends with the job-usage step, running this exact script."""

    def jobs(self):
        for path in sorted(WORKFLOWS.glob("*.yml")):
            doc = yaml.safe_load(path.read_text())
            for job_id, job in (doc.get("jobs") or {}).items():
                if "onepeek-" in json.dumps(job.get("runs-on", "")):
                    yield path.name, job_id, job

    def test_every_measured_job_ends_with_the_step(self):
        run = RUN.format(source=(Path(__file__).parent / "job_usage.py").read_text())
        seen = []
        for name, job_id, job in self.jobs():
            if name in NOT_MEASURED:
                continue
            seen.append((name, job_id))
            step = job["steps"][-1]
            where = f"{name} {job_id}"
            self.assertEqual(step.get("if"), STEP_IF, where)
            self.assertIs(step.get("continue-on-error"), True, where)
            self.assertEqual(step.get("timeout-minutes"), 1, where)
            self.assertNotIn("shell", step, where)
            self.assertEqual(step.get("run"), run, where)
            self.assertEqual(step.get("env"), {"RUNNER_CLASS": labels.classify(job["runs-on"], "inputs.runner", False),
                                               "RUNNER_INPUT": "${{ inputs.runner }}"}, where)
            self.assertEqual(sum(1 for s in job["steps"] if s.get("run") == run), 1, where)
        # fast-checks.yml fast joined with decision 180; ready.yml ready (GH-1); depends-on.yml gone (A2b)
        self.assertEqual(len(seen), 11, seen)

    def test_jobs_not_measured_have_no_step(self):
        for name, job_id, job in self.jobs():
            if name in NOT_MEASURED:
                steps = job.get("steps") or []
                self.assertFalse(any(isinstance(s, dict) and "job_usage.py" in str(s.get("run")) for s in steps),
                                 f"{name} {job_id}")

    def test_the_script_has_no_expression_and_no_runner_label(self):
        source = (Path(__file__).parent / "job_usage.py").read_text()
        self.assertNotIn("${{", source)
        self.assertNotRegex(source, r"self-hosted|onepeek-arm64|vars\.RUNNER")


if __name__ == "__main__":
    unittest.main()
