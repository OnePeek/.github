import json
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

import check_no_prerelease_deps as c


def run(files):
    with tempfile.TemporaryDirectory() as d:
        for name, content in files.items():
            f = Path(d) / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(content) if isinstance(content, dict) else content)
        return c.main([d])


class T(unittest.TestCase):
    def test_preview_dependency_fails(self):
        self.assertEqual(run({"package.json": {"dependencies": {"@onepeek/ui": "0.0.0-pr12.abc1234"}}}), 1)

    def test_dev_dependency_in_workspace_package_fails(self):
        self.assertEqual(run({"apps/web/package.json": {"devDependencies": {"@onepeek/api-client": "1.0.0-pr7.deadbee"}}}), 1)

    def test_dist_tag_spec_fails(self):
        self.assertEqual(run({"package.json": {"dependencies": {"@onepeek/ui": "pr-12"}}}), 1)

    def test_pnpm_overrides_fails(self):
        self.assertEqual(run({"package.json": {"pnpm": {"overrides": {"@onepeek/ui": "0.0.0-pr3.abc1234"}}}}), 1)

    def test_top_level_overrides_and_resolutions_fail(self):
        self.assertEqual(run({"package.json": {"overrides": {"@onepeek/ui": "0.0.0-pr3.abc1234"}}}), 1)
        self.assertEqual(run({"package.json": {"resolutions": {"@onepeek/ui": "0.0.0-pr3.abc1234"}}}), 1)

    def test_npm_alias_fails(self):
        self.assertEqual(run({"package.json": {"dependencies": {"ui": "npm:@onepeek/ui@0.0.0-pr12.abc1234"}}}), 1)

    def test_catalog_in_workspace_yaml_fails(self):
        ws = "packages:\n  - 'apps/*'\ncatalog:\n  '@onepeek/ui': 0.0.0-pr12.abc1234\n"
        self.assertEqual(run({"package.json": {"dependencies": {"@onepeek/ui": "catalog:"}}, "pnpm-workspace.yaml": ws}), 1)

    def test_workspace_yaml_overrides_fail(self):
        self.assertEqual(run({"pnpm-workspace.yaml": "overrides:\n  '@onepeek/ui': 0.0.0-pr1.abc1234\n"}), 1)

    def test_lockfile_only_resolution_fails(self):
        pnpm = textwrap.dedent("""\
            importers:
              .:
                dependencies:
                  '@onepeek/ui':
                    specifier: ^1.0.0
                    version: 0.0.0-pr12.abc1234
            """)
        self.assertEqual(run({"package.json": {"dependencies": {"@onepeek/ui": "^1.0.0"}}, "pnpm-lock.yaml": pnpm}), 1)
        self.assertEqual(run({"pnpm-lock.yaml": "packages:\n  '@onepeek/ui@0.0.0-pr12.abc1234':\n    resolution: {}\n"}), 1)

    def test_package_lock_and_yarn_lock_fail(self):
        npm = {"packages": {"node_modules/@onepeek/ui": {"version": "0.0.0-pr12.abc1234"}}}
        self.assertEqual(run({"package-lock.json": json.dumps(npm, indent=2)}), 1)
        self.assertEqual(run({"yarn.lock": '"@onepeek/ui@npm:^1.0.0":\n  version: 0.0.0-pr12.abc1234\n'}), 1)

    def test_unrelated_pr_version_under_onepeek_importer_passes(self):
        lock = "importers:\n  .:\n    dependencies:\n      '@onepeek/ui':\n        specifier: ^1.0.0\n        version: 1.0.0\n      left-pad:\n        specifier: 1.0.0-pr3.x\n        version: 1.0.0-pr3.x\n"
        self.assertEqual(run({"pnpm-lock.yaml": lock}), 0)

    def test_dependency_under_onepeek_snapshot_passes(self):
        lock = "snapshots:\n  '@onepeek/ui@1.0.0':\n    dependencies:\n      left-pad: 1.0.0-pr3.x\n"
        self.assertEqual(run({"pnpm-lock.yaml": lock}), 0)

    def test_next_and_stable_pass(self):
        pkg = {"dependencies": {"@onepeek/ui": "1.0.0-next.20261101173547", "@onepeek/tokens": "^1.2.0"}}
        lock = "importers:\n  .:\n    dependencies:\n      '@onepeek/ui':\n        specifier: 1.0.0-next.20261101173547\n        version: 1.0.0-next.20261101173547\n"
        self.assertEqual(run({"package.json": pkg, "pnpm-lock.yaml": lock}), 0)

    def test_other_packages_ignored(self):
        self.assertEqual(run({"package.json": {"dependencies": {"left-pad": "1.0.0-pr1.x"}}}), 0)

    def test_node_modules_skipped(self):
        self.assertEqual(run({"node_modules/x/package.json": {"dependencies": {"@onepeek/ui": "0.0.0-pr1.x"}}}), 0)

    def test_the_workflow_runs_this_script_through_the_shared_action_at_its_own_commit(self):
        # Decision 180 (ruling R-S2i-1): the body is actions/no-prerelease-deps, which no-prerelease-deps.yml and
        # fast-checks.yml both run from the OnePeek/.github checkout at the caller's pin; no inlined copy is left.
        root = Path(__file__).parent.parent
        action = yaml.safe_load((root / "actions/no-prerelease-deps/action.yml").read_text())
        (only,) = action["runs"]["steps"]
        self.assertEqual(only["run"], 'python3 -I "${GITHUB_ACTION_PATH}/../../scripts/check_no_prerelease_deps.py" .')
        for wf, job in (("no-prerelease-deps.yml", "no-prerelease-deps"), ("fast-checks.yml", "fast")):
            steps = yaml.safe_load((root / ".github/workflows" / wf).read_text())["jobs"][job]["steps"]
            self.assertIn("./_shared/actions/no-prerelease-deps", [s.get("uses") for s in steps], wf)
            self.assertNotIn("SPEC = re.compile", (root / ".github/workflows" / wf).read_text(), wf)

if __name__ == "__main__":
    unittest.main()
