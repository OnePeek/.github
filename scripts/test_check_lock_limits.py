"""The limits test of the release-lock waits (foundation-playbook §5.4a "Waits that fit the runners"), with each of
its rules broken once. Run: python -m unittest discover -s scripts -p 'test_*.py'"""

import shutil
import tempfile
import unittest
from pathlib import Path

import check_lock_limits as c

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"
LIFE = {"spot": 90, "on-demand": 180}
SHARED = ("release-ecs.yml", "operate.yml", "release-lock.yml")


class Limits(unittest.TestCase):
    def check(self, edit=None, life=LIFE, table=None):
        with tempfile.TemporaryDirectory() as d:
            for name in SHARED:
                shutil.copy(WORKFLOWS / name, Path(d) / name)
            if edit:
                name, old, new = edit
                path = Path(d) / name
                text = path.read_text(encoding="utf-8")
                self.assertIn(old, text)
                path.write_text(text.replace(old, new, 1), encoding="utf-8")
            return c.check(Path(d), life, table)

    def test_the_shipped_workflows_pass(self):
        self.assertEqual(c.check(WORKFLOWS, LIFE), [])

    def test_a_limit_below_its_cap_plus_its_other_steps_fails(self):
        problems = self.check(("release-ecs.yml", "    timeout-minutes: 135\n", "    timeout-minutes: 130\n"))
        self.assertTrue(any("below its wait cap 120 plus its other steps 15" in p for p in problems), problems)

    def test_a_limit_above_its_timeout_exceptions_entry_fails(self):
        problems = self.check(("operate.yml", "    timeout-minutes: 135\n", "    timeout-minutes: 140\n"))
        self.assertTrue(any("above its TIMEOUT_EXCEPTIONS entry 135" in p for p in problems), problems)

    def test_a_limit_above_the_reaper_lifetime_less_20_fails(self):
        problems = c.check(WORKFLOWS, {"spot": 90, "on-demand": 150})
        self.assertTrue(any("above the stateful reaper lifetime 150 less 20" in p for p in problems), problems)

    def test_a_waiting_job_on_a_spot_class_is_held_to_the_spot_lifetime_and_the_class_cap(self):
        od = "inputs.runner == 'onepeek' && 'onepeek-arm64-od' || inputs.runner == 'canary' && 'onepeek-arm64-od'"
        fast = "inputs.runner == 'onepeek' && 'onepeek-arm64' || inputs.runner == 'canary' && 'onepeek-arm64-canary'"
        problems = self.check(("release-lock.yml", od, fast))
        self.assertTrue(any("above the fast lint cap 60" in p for p in problems), problems)
        self.assertTrue(any("above the fast reaper lifetime 90 less 20" in p for p in problems), problems)

    def test_an_expression_limit_is_read_as_its_larger_value(self):
        problems = self.check(("release-lock.yml", "&& 135 || 10 }}", "&& 150 || 10 }}"))
        self.assertTrue(any("release-lock.yml job lock: timeout-minutes 150" in p for p in problems), problems)

    def test_a_cap_that_differs_from_the_table_fails(self):
        problems = self.check(("release-ecs.yml", 'LOCK_WAIT_CAP_SECONDS: "7200"', 'LOCK_WAIT_CAP_SECONDS: "9000"'))
        self.assertTrue(any("LOCK_WAIT_CAP_SECONDS is '9000'" in p for p in problems), problems)

    def test_a_re_mint_interval_not_below_one_hour_fails(self):
        problems = self.check(("operate.yml", 'LOCK_REMINT_SECONDS: "3000"', 'LOCK_REMINT_SECONDS: "3600"'))
        self.assertTrue(any("LOCK_REMINT_SECONDS '3600' is not below" in p for p in problems), problems)

    def test_a_job_that_waits_but_is_missing_from_the_table_fails(self):
        table = {k: v for k, v in c.TABLE.items() if k != ("operate.yml", "lock")}
        problems = self.check(table=table)
        self.assertTrue(any("operate.yml job lock waits for a release lock but is missing" in p for p in problems), problems)

    def test_a_table_row_whose_job_no_longer_waits_fails(self):
        problems = self.check(("release-lock.yml", 'LOCK_TOKEN="${GIVEN_TOKEN}" lock_finish', 'LOCK_TOKEN="${GIVEN_TOKEN}" true'))
        self.assertEqual(problems, [])   # the job still waits (take)
        problems = c.check(WORKFLOWS, LIFE, {**c.TABLE, ("release-ecs.yml", "deploy"): {"cap": 1, "cap_env": "X", "other": 1}})
        self.assertTrue(any("release-ecs.yml job deploy is in the limits table but does not wait" in p for p in problems), problems)

    def test_the_lifetimes_are_read_from_the_infra_config(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ci.yaml"
            p.write_text("runner:\n  max_lifetime_minutes: { spot: 90, on-demand: 180 }   # the reaper\n")
            self.assertEqual(c.lifetimes(p), LIFE)
            self.assertEqual(c.main(["--infra-config", str(p), "--workflows", str(WORKFLOWS)]), 0)
            p.write_text("runner:\n  max_lifetime_minutes: { spot: 90, on-demand: 140 }\n")
            self.assertEqual(c.main(["--infra-config", str(p), "--workflows", str(WORKFLOWS)]), 1)


if __name__ == "__main__":
    unittest.main()
