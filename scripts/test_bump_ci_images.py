"""The weekly CI-image bump (bump_ci_images.py) and the reusable workflow that embeds it (ci-image-bump.yml)
(onepeek-docs architecture/lld/infra/foundation-playbook.md §12a slice 4 item 4).
Run: python -m unittest discover -s scripts -p 'test_*.py'"""
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import yaml

import bump_ci_images as bump
from check_runner_labels import classify

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/ci-image-bump.yml"
OLD, NEW = "sha256:" + "a" * 64, "sha256:" + "b" * 64
PG = "public.ecr.aws/docker/library/postgres"

CI = f"""\
jobs:
  t:
    runs-on: ubuntu-latest
    services:
      pg:
        image: {PG}@{OLD} # 17
        env: {{A: b}}
      mongo:
        image: "public.ecr.aws/docker/library/mongo@{OLD}" # mongo:7 (keep)
    container: node@{OLD} # 22
    steps:
      - run: echo image: not-an-image
"""


def tree(text=CI):
    d = Path(tempfile.mkdtemp())
    (d / ".github/workflows").mkdir(parents=True)
    (d / ".github/workflows/ci.yml").write_text(text)
    return d


class Rewrite(unittest.TestCase):
    def test_every_moved_digest_is_rewritten_and_the_rest_of_the_file_is_untouched(self):
        d, calls = tree(), []
        def resolve(name, tag):
            calls.append((name, tag))
            return NEW
        moved = bump.bump(d, resolve)
        text = (d / ".github/workflows/ci.yml").read_text()
        self.assertEqual(calls, [(PG, "17"), ("public.ecr.aws/docker/library/mongo", "7"), ("node", "22")])
        self.assertEqual(len(moved), 3)
        self.assertEqual(text, CI.replace(OLD, NEW))
        self.assertIn("# mongo:7 (keep)", text)

    def test_a_current_digest_changes_nothing(self):
        d = tree()
        self.assertEqual(bump.bump(d, lambda n, t: OLD), [])
        self.assertEqual((d / ".github/workflows/ci.yml").read_text(), CI)

    def test_check_mode_reports_but_does_not_write(self):
        d = tree()
        self.assertEqual(len(bump.bump(d, lambda n, t: NEW, write=False)), 3)
        self.assertEqual((d / ".github/workflows/ci.yml").read_text(), CI)

    def test_each_image_resolves_to_its_own_digest(self):
        d = tree()
        bump.bump(d, lambda n, t: "sha256:" + ("1" if "mongo" in n else "2") * 64)
        text = (d / ".github/workflows/ci.yml").read_text()
        self.assertIn(f"mongo@sha256:{'1' * 64}", text)
        self.assertIn(f"{PG}@sha256:{'2' * 64}", text)

    def test_an_image_without_a_tag_comment_or_digest_is_left_alone(self):
        text = f"jobs:\n  t:\n    services:\n      a:\n        image: {PG}@{OLD}\n      b:\n        image: {PG}:17\n"
        d = tree(text)
        self.assertEqual(bump.bump(d, lambda n, t: NEW), [])

    def test_a_tag_that_cannot_be_resolved_is_an_error_not_a_skip(self):
        def boom(n, t):
            raise RuntimeError("registry answered 404")
        with self.assertRaises(RuntimeError):
            bump.bump(tree(), boom)

    def test_tag_of(self):
        self.assertEqual(bump.tag_of("17"), "17")
        self.assertEqual(bump.tag_of("postgres:17-alpine more words"), "17-alpine")
        self.assertIsNone(bump.tag_of(""))

    def test_split_name(self):
        self.assertEqual(bump.split_name(PG), ("public.ecr.aws", "docker/library/postgres"))
        self.assertEqual(bump.split_name("node"), ("registry-1.docker.io", "library/node"))
        self.assertEqual(bump.split_name("grafana/grafana"), ("registry-1.docker.io", "grafana/grafana"))


class Registry(unittest.TestCase):
    """The anonymous, read-only registry flow: a manifest HEAD, one anonymous token on a 401, the digest header."""

    def test_token_flow_sends_no_credentials_and_only_reads(self):
        seen = []
        def urlopen(req, timeout=0):
            url = req.full_url if hasattr(req, "full_url") else req
            seen.append((getattr(req, "method", "GET"), url, getattr(req, "headers", {})))
            if "/token" in url:
                return io.BytesIO(json.dumps({"token": "t0k"}).encode())
            if not any(h.lower() == "authorization" for h in req.headers):
                hdr = {"WWW-Authenticate": 'Bearer realm="https://public.ecr.aws/token/",service="public.ecr.aws",scope="repository:docker/library/postgres:pull"'}
                raise urllib.error.HTTPError(url, 401, "no", hdr, None)
            resp = mock.MagicMock()
            resp.headers = {"Docker-Content-Digest": NEW}
            resp.__enter__.return_value = resp
            return resp
        with mock.patch.object(urllib.request, "urlopen", urlopen):
            self.assertEqual(bump.registry_digest(PG, "17"), NEW)
        self.assertEqual([m for m, *_ in seen if m != "GET"], ["HEAD", "HEAD"])
        self.assertTrue(all(m in ("HEAD", "GET") for m, *_ in seen))
        self.assertFalse(any(h.lower() == "authorization" for _, u, hs in seen if "/token" in u for h in hs))

    def test_an_error_status_other_than_401_fails(self):
        def urlopen(req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
        with mock.patch.object(urllib.request, "urlopen", urlopen), self.assertRaises(RuntimeError):
            bump.registry_digest(PG, "nope")

    def test_a_missing_digest_header_fails(self):
        resp = mock.MagicMock()
        resp.headers = {}
        resp.__enter__.return_value = resp
        with mock.patch.object(urllib.request, "urlopen", lambda req, timeout=0: resp), self.assertRaises(RuntimeError):
            bump.registry_digest(PG, "17")


class SharedWorkflow(unittest.TestCase):
    text = WORKFLOW.read_text(encoding="utf-8")
    wf = yaml.safe_load(text)

    def test_it_is_reusable_with_the_runner_input_and_light_class(self):
        call = self.wf[True]["workflow_call"]
        self.assertEqual(call["inputs"]["runner"]["default"], "github")
        job = self.wf["jobs"]["bump"]
        self.assertEqual(classify(job["runs-on"], "inputs.runner", True), "light")
        self.assertLessEqual(job["timeout-minutes"], 30)

    def test_it_embeds_the_script_verbatim(self):
        src = (ROOT / "scripts/bump_ci_images.py").read_text(encoding="utf-8")
        indented = "\n".join(("          " + line) if line else "" for line in src.splitlines())
        self.assertIn(indented, self.text)

    def test_write_access_is_on_the_job_only_and_uses_github_token_not_the_app(self):
        self.assertEqual(self.wf["permissions"], {"contents": "read"})
        self.assertEqual(self.wf["jobs"]["bump"]["permissions"], {"contents": "write", "pull-requests": "write"})
        self.assertNotIn("create-github-app-token", self.text)
        self.assertNotIn("AGENTS_APP", self.text)
        self.assertIn("GH_TOKEN: ${{ github.token }}", self.text)

    def test_the_header_says_the_pull_request_needs_a_close_and_reopen(self):
        self.assertIn("closes and reopens", self.text)

    def test_the_registry_step_carries_no_credentials(self):
        step = next(s for s in self.wf["jobs"]["bump"]["steps"] if "bump_ci_images" in s.get("run", "") or "check.py" in s.get("run", ""))
        self.assertNotIn("secrets.", json.dumps(step))
        self.assertNotIn("TOKEN", json.dumps(step.get("env", {})))


if __name__ == "__main__":
    unittest.main()
