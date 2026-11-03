import tempfile
import unittest
from pathlib import Path

import check_service_list as c

LOCK = "          # --- release-lock begin (x) ---\n          lock_take() { :; }\n          # --- release-lock end ---\n"
BLOCK = "          # --- render-taskdef begin (x) ---\n          render() { :; }\n          # --- render-taskdef end ---\n" + LOCK + LOCK
OPERATE = "on:\n  options:\n    # service-list begin\n    options: [all, api]\n    # service-list end\n" + BLOCK
SERVICES = "services:\n  api: {triggers: [request]}\n  migrate: {triggers: [migration]}\n"


class T(unittest.TestCase):
    def run_check(self, operate, release=BLOCK, services=SERVICES, *extra):
        with tempfile.TemporaryDirectory() as d:
            w = Path(d)
            (w / "operate.yml").write_text(operate)
            (w / "release-ecs.yml").write_text(release)
            (w / "s.yaml").write_text(services)
            return c.main(["--workflows", d, "--services-file", str(w / "s.yaml"), *extra])

    def test_in_step(self):
        self.assertEqual(self.run_check(OPERATE), 0)

    def test_new_service_drifts(self):
        self.assertEqual(self.run_check(OPERATE, services=SERVICES + "  web: {triggers: [request]}\n"), 1)

    def test_render_function_differs(self):
        self.assertEqual(self.run_check(OPERATE, release=BLOCK.replace("render() { :; }", "render() { echo; }")), 1)

    def test_lock_functions_differ(self):
        # One lock (foundation-playbook §5.4): a lock or unlock job with its own copy of the functions is refused.
        self.assertEqual(self.run_check(OPERATE, release=BLOCK.replace(LOCK, LOCK.replace(":;", "echo;"), 1)), 1)
        self.assertEqual(self.run_check(OPERATE.replace(LOCK, LOCK.replace(":;", "echo;"), 1)), 1)

    def test_a_missing_lock_block_is_refused(self):
        self.assertEqual(self.run_check(OPERATE, release=BLOCK.replace(LOCK, "", 1)), 1)


if __name__ == "__main__":
    unittest.main()
