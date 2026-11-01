import tempfile
import unittest
from pathlib import Path

import check_service_list as c

BLOCK = "          # --- render-taskdef begin (x) ---\n          render() { :; }\n          # --- render-taskdef end ---\n"
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
        self.assertEqual(self.run_check(OPERATE, release=BLOCK.replace(":;", "echo;")), 1)


if __name__ == "__main__":
    unittest.main()
