"""The `actions/pins` step that runs the public-repo runner lint on a caller's workflows (FU-PUBLIC-RUNNER: plugin-owl#62's
`fast` job had no `runner: public-hosted`, queued 24 hours on a runner that never takes a public repo's job)."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / "actions/pins/action.yml"
CALL = "    uses: OnePeek/.github/.github/workflows/fast-checks.yml@8f6c35483b42b2f01b678597e7693c5ada31eb0f # v1.29.3\n"


class PublicRunnerStep(unittest.TestCase):
    def step(self) -> dict:
        return next(s for s in yaml.safe_load(ACTION.read_text())["runs"]["steps"] if "check_runner_labels.py" in str(s.get("run")))

    def test_it_runs_only_in_a_public_repo_after_the_venv_exists(self) -> None:
        steps = yaml.safe_load(ACTION.read_text())["runs"]["steps"]
        self.assertEqual(self.step()["if"], "${{ github.event.repository.private == false }}")
        venv = next(i for i, s in enumerate(steps) if "-m venv" in str(s.get("run")))
        self.assertGreater(steps.index(self.step()), venv)
        self.assertIn("--kind public", self.step()["run"])

    def caller(self, with_block: str) -> subprocess.CompletedProcess:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        wf = d / "repo/.github/workflows"
        wf.mkdir(parents=True)
        (wf / "checks.yml").write_text("on: pull_request\njobs:\n  fast:\n" + CALL + with_block)
        venv = d / "tmp/cci/bin"
        venv.mkdir(parents=True)
        (venv / "python").write_text(f'#!/bin/sh\nexec {sys.executable} "$@"\n')
        (venv / "python").chmod(0o755)
        action = d / "shared/actions/pins"
        action.mkdir(parents=True)
        (d / "shared/scripts").symlink_to(ROOT / "scripts")
        return subprocess.run(["bash", "-eo", "pipefail", "-c", self.step()["run"]], cwd=d / "repo", capture_output=True, text=True,
                              env={"GITHUB_ACTION_PATH": str(action), "RUNNER_TEMP": str(d / "tmp"), "REPO": "OnePeek/plugin-owl",
                                   "PATH": "/usr/bin:/bin"})

    def test_a_call_without_the_runner_fails(self) -> None:
        r = self.caller("    with:\n      pins: true\n")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("passes runner: public-hosted", r.stdout)

    def test_a_call_with_the_wrong_runner_fails(self) -> None:
        self.assertEqual(self.caller("    with:\n      runner: github\n").returncode, 1)

    def test_a_call_with_public_hosted_passes(self) -> None:
        r = self.caller("    with:\n      runner: public-hosted\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
