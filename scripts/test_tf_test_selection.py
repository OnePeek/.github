import contextlib
import io
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

import tf_test_selection as t

CALLER = "OnePeek/infra/.github/workflows/checks.yml@refs/pull/7/merge"

# A small repository shaped like onepeek-infra: a stack using two modules, an env using the stack, a root reading
# config by an interpolated path, a module reading a script through a path local, and a script importing a sibling.
TREE = {
    "modules/aws/cache/main.tf": 'resource "x" "y" {}\n',
    "modules/aws/cache/tests/a.tftest.hcl": "run \"a\" {}\n",
    "modules/aws/settings/main.tf": textwrap.dedent("""\
        locals {
          ca_scripts = "${path.module}/../../../scripts"
          ca_script  = "${local.ca_scripts}/mint.py"
          ca_req     = "${local.ca_scripts}/req.txt"
          arn        = "${local.bucket_arn}/*"
        }
        """),
    "modules/aws/settings/tests/a.tftest.hcl": "run \"a\" {}\n",
    "modules/aws/obs/main.tf": 'module "inner" {\n  source = "./inner"\n}\n',
    "modules/aws/obs/inner/main.tf": "",
    "modules/aws/obs/tests/a.tftest.hcl": 'variables {\n  c = file("../../../images/collector/c.yaml")\n}\n',
    "modules/aws/unused/main.tf": "",
    "modules/aws/unused/tests/a.tftest.hcl": "",
    "stacks/aws/main.tf": textwrap.dedent("""\
        module "cache" {
          source = "../../modules/aws/cache"
        }
        module "settings" {
          source = "../../modules/aws/settings"
        }
        module "obs" {
          source = "../../modules/aws/obs"
        }
        locals {
          acl = "${path.module}/../../config/egress-acl.${var.env}.yaml"
        }
        """),
    "stacks/aws/tests/a.tftest.hcl": 'variables {\n  config = yamldecode(file("../../config/preprod.yaml"))\n}\n',
    "stacks/aws/tests/fixtures/images.json": "{}",
    "envs/sandbox/main.tf": 'module "stack" {\n  source = "../../stacks/aws"\n}\n',
    "envs/sandbox/tests/a.tftest.hcl": 'variables {\n  images_file = "../../stacks/aws/tests/fixtures/images.json"\n}\n',
    "envs/preprod/main.tf": 'module "stack" {\n  source = "../../stacks/aws"\n}\n',
    "bootstrap/main.tf": 'locals {\n  c = yamldecode(file("${path.module}/../config/${var.environment}.yaml"))\n}\n',
    "bootstrap/tests/a.tftest.hcl": "",
    "scripts/mint.py": "import helper\nimport json\n",
    "scripts/helper.py": "",
    "scripts/req.txt": "",
    "scripts/up.py": "",
    "config/preprod.yaml": "",
    "config/egress-acl.sandbox.yaml": "",
    "config/ci.json": "",
    "images/collector/c.yaml": "",
    "policy/p.yaml": "",
    "docs/a.md": "",
}
ALL = ["bootstrap", "envs/sandbox", "modules/aws/cache", "modules/aws/obs", "modules/aws/settings", "modules/aws/unused",
       "stacks/aws"]
STACK = ["envs/sandbox", "stacks/aws"]


def build(root: Path, tree: dict) -> None:
    for name, text in tree.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)


class Select(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        build(self.root, TREE)
        self.graph = t.Graph(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def pr(self, *changed, caller=CALLER):
        with contextlib.redirect_stderr(io.StringIO()):
            return t.select(self.graph, list(changed), "pull_request", "dev", caller)

    def test_suite_dir_is_the_folder_above_tests(self):
        self.assertEqual(t.suite_dir("modules/aws/cache/tests/a.tftest.hcl"), "modules/aws/cache")
        self.assertEqual(t.suite_dir("stacks/aws/tests/deep/x/a.tftest.hcl"), "stacks/aws")
        self.assertEqual(t.suite_dir("modules/x/a.tftest.hcl"), "modules/x")
        self.assertEqual(t.suite_dir("tests/a.tftest.hcl"), ".")

    def test_a_module_change_runs_its_suite_and_every_suite_using_it(self):
        self.assertEqual(self.pr("modules/aws/cache/main.tf"), ["envs/sandbox", "modules/aws/cache", "stacks/aws"])
        self.assertEqual(self.pr("modules/aws/obs/inner/main.tf"), ["envs/sandbox", "modules/aws/obs", "stacks/aws"])

    def test_a_test_file_change_runs_only_its_suite(self):
        self.assertEqual(self.pr("modules/aws/cache/tests/a.tftest.hcl"), ["modules/aws/cache"])
        self.assertEqual(self.pr("bootstrap/tests/a.tftest.hcl"), ["bootstrap"])

    def test_a_stack_change_runs_the_stack_and_the_env_using_it(self):
        self.assertEqual(self.pr("stacks/aws/main.tf"), STACK)

    def test_files_read_by_path_run_their_readers(self):
        self.assertEqual(self.pr("config/preprod.yaml"), ["bootstrap", "envs/sandbox", "stacks/aws"])
        self.assertEqual(self.pr("config/egress-acl.sandbox.yaml"), ["bootstrap", "envs/sandbox", "stacks/aws"])
        self.assertEqual(self.pr("config/ci.json"), [])  # `${var.environment}.yaml`: a .json is not read
        self.assertEqual(self.pr("images/collector/c.yaml"), ["envs/sandbox", "modules/aws/obs", "stacks/aws"])
        self.assertEqual(self.pr("stacks/aws/tests/fixtures/images.json"), STACK)

    def test_a_path_local_reaches_only_the_files_built_on_it(self):
        settings = ["envs/sandbox", "modules/aws/settings", "stacks/aws"]
        self.assertEqual(self.pr("scripts/mint.py"), settings)
        self.assertEqual(self.pr("scripts/req.txt"), settings)
        self.assertEqual(self.pr("scripts/helper.py"), settings)  # imported by mint.py
        self.assertEqual(self.pr("scripts/up.py"), [])

    def test_a_path_local_with_another_use_reaches_its_whole_folder(self):
        build(self.root, {"modules/aws/settings/extra.tf": 'output "d" {\n  value = local.ca_scripts\n}\n'})
        self.graph = t.Graph(self.root)
        self.assertEqual(self.pr("scripts/up.py"), ["envs/sandbox", "modules/aws/settings", "stacks/aws"])

    def test_a_script_editing_sys_path_reaches_its_folder(self):
        build(self.root, {"scripts/mint.py": "import sys\nsys.path.insert(0, '.')\n"})
        self.assertEqual(self.pr("scripts/up.py"), ["envs/sandbox", "modules/aws/settings", "stacks/aws"])

    def test_unrelated_changes_run_nothing(self):
        self.assertEqual(self.pr("README.md", "docs/a.md", "policy/p.yaml", "envs/preprod/main.tf",
                                 ".github/workflows/sandbox.yml"), [])
        self.assertEqual(self.pr(), [])

    def test_a_directory_prefix_is_not_a_match(self):
        self.assertEqual(self.pr("bootstrap-notes/a.md", "configs/preprod.yaml"), [])

    def test_the_callers_workflow_runs_everything(self):
        self.assertEqual(self.pr(".github/workflows/checks.yml"), ALL)

    def test_an_unreadable_caller_runs_everything(self):
        for caller in ("", "checks.yml", "O/R/.github/workflows/checks.yml"):
            self.assertEqual(self.pr("docs/a.md", caller=caller), ALL, caller)

    def test_terraform_or_test_files_outside_every_unit_run_everything(self):
        for f in (".tflint.hcl", ".terraform-version", "shared/x.tf", "gone/main.tf", "helpers/tests/mock.json",
                  "x.tfvars", "y.tfmock.hcl"):
            self.assertEqual(self.pr(f), ALL, f)

    def test_release_pushes_and_schedules_run_everything(self):
        self.assertEqual(t.select(self.graph, ["README.md"], "pull_request", "release", CALLER), ALL)
        self.assertEqual(t.select(self.graph, [], "push", "", CALLER), ALL)
        self.assertEqual(t.select(self.graph, [], "schedule", "", ""), ALL)
        self.assertEqual(t.select(self.graph, [], "workflow_dispatch", "", ""), ALL)

    def test_an_unknown_module_source_is_unknown(self):
        build(self.root, {"stacks/aws/extra.tf": 'module "m" {\n  source = "../../modules/aws/missing"\n}\n'})
        with self.assertRaises(t.Unknown):
            t.Graph(self.root)

    def test_climbing_out_of_an_interpolation_is_unknown(self):
        build(self.root, {"bootstrap/extra.tf": 'locals {\n  d = "${path.module}/${var.x}/../y"\n}\n'})
        graph = t.Graph(self.root)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(t.select(graph, ["docs/a.md"], "pull_request", "dev", CALLER), ALL)


class Tokens(unittest.TestCase):
    def test_kinds_holes_and_heredocs(self):
        text = 'a = "${path.module}/../x/${var.e}.yaml"\nb = <<-EOT\n  python3 ../../s.py --e "${var.e}"\nEOT\n' \
               'c = "${local.p}/m.py"\nd = "${path.root}/f"\n'
        self.assertEqual(t.tokens(text), [("module", "../x/" + t.HOLE + ".yaml"), ("rel", "../../s.py"),
                                          ("local:p", "m.py"), ("cwd", "f")])

    def test_resolve(self):
        self.assertEqual(t.resolve("modules/aws/x", "../../../config/a.yaml"), "config/a.yaml")
        self.assertIsNone(t.resolve("stacks/aws", "../../../outside"))
        with self.assertRaises(t.Unknown):
            t.resolve("a", t.HOLE + "/../b")


class Embedded(unittest.TestCase):
    def test_workflow_embeds_the_script_verbatim(self):
        wf = (Path(__file__).parent.parent / ".github/workflows/terraform-checks.yml").read_text()
        body = wf.split("<<'PY'\n", 1)[1].split("          PY\n", 1)[0]
        self.assertEqual(textwrap.dedent(body), (Path(__file__).parent / "tf_test_selection.py").read_text())

    def test_workflow_passes_the_caller_workflow(self):
        wf = (Path(__file__).parent.parent / ".github/workflows/terraform-checks.yml").read_text()
        self.assertIn("CALLER: ${{ github.workflow_ref }}", wf)
        self.assertIn('--caller-workflow "${CALLER}"', wf)


class Cli(unittest.TestCase):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = t.main(list(argv))
        return code, out.getvalue().split(), err.getvalue()

    def test_git_diff_against_the_base(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)

            def git(*a):
                subprocess.run(["git", "-C", d, "-c", "user.email=a@b", "-c", "user.name=a", *a], check=True, capture_output=True)

            git("init", "-q", "-b", "dev")
            build(root, TREE)
            git("add", "-A")
            git("commit", "-qm", "base")
            git("branch", "base")
            (root / "modules/aws/cache/main.tf").write_text("# x\n")
            git("mv", "scripts/up.py", "scripts/up2.py")
            git("add", "-A")
            git("commit", "-qm", "change")
            code, out, _ = self.run_main("--event", "pull_request", "--base-ref", "dev", "--diff-against", "base",
                                         "--caller-workflow", CALLER, "--root", d)
            self.assertEqual((code, out), (0, ["envs/sandbox", "modules/aws/cache", "stacks/aws"]))
            code, out, err = self.run_main("--event", "pull_request", "--base-ref", "dev", "--diff-against", "nope",
                                           "--caller-workflow", CALLER, "--root", d)
            self.assertEqual((code, out), (0, ALL))
            self.assertIn("cannot be read", err)

    def test_an_unreadable_tree_runs_everything(self):
        with tempfile.TemporaryDirectory() as d:
            build(Path(d), {**TREE, "stacks/aws/extra.tf": 'module "m" {\n  source = "../nowhere"\n}\n'})
            code, out, err = self.run_main("--event", "pull_request", "--base-ref", "dev", "--diff-against", "x",
                                           "--caller-workflow", CALLER, "--root", d)
            self.assertEqual((code, out), (0, ALL))
            self.assertIn("every suite runs", err)

    def test_no_suites_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.run_main("--event", "push", "--root", d)[0], 1)


if __name__ == "__main__":
    unittest.main()
