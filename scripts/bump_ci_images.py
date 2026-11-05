"""Move digest-pinned CI container images to their tag's current digest (foundation-playbook §12a slice 4 item 4).

A CI image is written `image: <name>@sha256:<64 hex> # <tag>` (or `container: ...` as a string), in a workflow under
.github/workflows; the comment's first word is the tag (`17`, or `postgres:17`: the part after the last colon). For each
one this asks the registry for the tag's current digest and rewrites the line when it moved. Registry access is read-only
and anonymous (a pull token, a manifest GET); no credentials are used or needed for the public registries CI containers
come from (public.ecr.aws). A tag the registry cannot resolve is an error, never a skip, and so is a pinned line this
cannot move (an `image:`/`container:` line naming `@sha256:` that is not in the form above, such as one with no tag
comment). A `:tag` written in the name (`mongo:7@sha256:...`) is dropped before the registry is asked: the digest names
the image, the comment names the tag.

Usage: bump_ci_images.py [--check] [ROOT]   (--check lists what would move and exits 1 if anything would; default rewrites)
Prints one `file:line: <name> <tag> <old> -> <new>` line per move.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

LINE = re.compile(
    r"^(?P<head>\s*(?:-\s*)?(?:image|container):\s*)(?P<q>['\"]?)(?P<name>[^\s'\"@#]+)@(?P<digest>sha256:[0-9a-f]{64})(?P=q)"
    r"(?P<gap>\s+)#\s*(?P<comment>.*?)\s*$")
ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"])
PINNED = re.compile(r"^\s*(?:-\s*)?(?:image|container):.*@sha256:")  # a pinned line: it must match LINE
Resolver = Callable[[str, str], str]  # (name, tag) -> "sha256:<64 hex>"


def tag_of(comment: str) -> str | None:
    word = comment.split()[0] if comment.split() else ""
    tag = word.rsplit(":", 1)[-1]
    return tag or None


def repository(name: str) -> str:
    """The name without a `:tag` in its last path part (a registry host's `:port` stays)."""
    head, slash, last = name.rpartition("/")
    return f"{head}{slash}{last.split(':', 1)[0]}"


def split_name(name: str) -> tuple[str, str]:
    """(registry host, repository path); a name with no host is Docker Hub."""
    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        return first, rest
    return "registry-1.docker.io", (name if "/" in name else f"library/{name}")


def _bearer(header: str) -> dict[str, str]:
    return dict(re.findall(r'(\w+)="([^"]*)"', header))


def registry_digest(name: str, tag: str) -> str:
    """The digest a tag points at, from the Docker-Content-Digest header of an anonymous manifest request."""
    host, repo = split_name(name)
    url = f"https://{host}/v2/{repo}/manifests/{tag}"
    token = None
    for _ in range(2):
        req = urllib.request.Request(url, method="HEAD", headers={"Accept": ACCEPT})
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                digest = resp.headers.get("Docker-Content-Digest", "")
                if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                    raise RuntimeError(f"{name}:{tag}: the registry sent no digest")
                return digest
        except urllib.error.HTTPError as e:
            challenge = e.headers.get("WWW-Authenticate", "")
            if e.code != 401 or token or not challenge.lower().startswith("bearer"):
                raise RuntimeError(f"{name}:{tag}: registry answered {e.code}") from e
            c = _bearer(challenge)
            query = "&".join(f"{k}={v}" for k, v in (("service", c.get("service")), ("scope", c.get("scope"))) if v)
            with urllib.request.urlopen(f"{c['realm']}?{query}", timeout=30) as tr:  # anonymous: no credentials sent
                body = json.load(tr)
            token = body.get("token") or body.get("access_token")
    raise RuntimeError(f"{name}:{tag}: no digest")


def bump_file(path: Path, resolve: Resolver, write: bool) -> list[str]:
    moved: list[str] = []
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    for n, raw in enumerate(lines, 1):
        m = LINE.match(raw.rstrip("\r\n"))
        if not m:
            if PINNED.match(raw):
                raise RuntimeError(f"{path}:{n}: a digest-pinned image this cannot move: write it "
                                   "`image: <name>@sha256:<64 hex> # <tag>` on one line")
            continue
        tag = tag_of(m["comment"])
        if tag is None:
            raise RuntimeError(f"{path}:{n}: the comment after the digest must start with the tag")
        new = resolve(repository(m["name"]), tag)
        if new != m["digest"]:
            moved.append(f"{path}:{n}: {m['name']} {tag} {m['digest']} -> {new}")
            lines[n - 1] = raw.replace(m["digest"], new, 1)
    if moved and write:
        path.write_text("".join(lines), encoding="utf-8")
    return moved


def bump(root: Path, resolve: Resolver, write: bool = True) -> list[str]:
    moved: list[str] = []
    workflows = root / ".github/workflows"
    for p in sorted([*workflows.glob("*.yml"), *workflows.glob("*.yaml")]):
        moved += bump_file(p, resolve, write)
    return moved


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("root", nargs="?", type=Path, default=Path("."))
    a = ap.parse_args(argv)
    try:
        moved = bump(a.root, registry_digest, write=not a.check)
    except (RuntimeError, urllib.error.URLError, OSError) as e:
        print(f"::error::{e}")
        return 1
    for line in moved:
        print(line)
    if not moved:
        print("ci images: all digests are current")
    return 1 if (moved and a.check) else 0


if __name__ == "__main__":
    sys.exit(main())
