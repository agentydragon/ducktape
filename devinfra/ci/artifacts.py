# /// script
# requires-python = ">=3.12"
# dependencies = ["pydantic>=2.0", "httpx>=0.27"]
# ///
"""Shared artifact definitions and SHA-256 helpers for the release pipeline.

Also runnable as a script from the release-artifact action:
    uv run --script devinfra/ci/artifacts.py filename claude-hook-rs
prints the registered filename for the given pkg.
"""

import base64
import hashlib
import re
import sys
from collections.abc import Iterable

import httpx
from pydantic import BaseModel, Field


class Pin(BaseModel):
    url: str
    sha256: str


class Sources(BaseModel):
    pins: dict[str, Pin]


_HEX_SUFFIX_RE = re.compile(r"^[0-9a-f]{7}$|^[0-9a-f]{12}$")


def is_tag_for_pkg(tag: str, pkg: str) -> bool:
    """Match `{pkg}-{7hex}` or `{pkg}-{12hex}` exactly, avoiding prefix collisions."""
    prefix = f"{pkg}-"
    if not tag.startswith(prefix):
        return False
    return _HEX_SUFFIX_RE.match(tag[len(prefix) :]) is not None


class Artifact(BaseModel, frozen=True):
    pkg: str = Field(description="npins package name")
    filename: str = Field(description="Artifact filename attached to the GitHub release")


ARTIFACTS = [
    Artifact(pkg="claude-hooks", filename="claude_hooks-0.1.0-py3-none-any.whl"),
    Artifact(pkg="claude-hook-rs", filename="claude_hook"),
    Artifact(pkg="ducktape-util", filename="ducktape_util-0.1.0-py3-none-any.whl"),
    Artifact(pkg="ducktape", filename="ducktape-0.1.0-py3-none-any.whl"),
    Artifact(pkg="gterm-theme", filename="gterm_theme-0.1.0-py3-none-any.whl"),
    Artifact(pkg="skills", filename="all_skills_tar.tar"),
    Artifact(pkg="bbapi", filename="bbapi"),
]


def _sha256_of_chunks(chunks: Iterable[bytes]) -> str:
    h = hashlib.sha256()
    for chunk in chunks:
        h.update(chunk)
    return base64.b64encode(h.digest()).decode()


def url_sha256(url: str) -> str:
    with httpx.stream("GET", url, follow_redirects=True) as response:
        response.raise_for_status()
        return _sha256_of_chunks(response.iter_bytes(65536))


if __name__ == "__main__":
    # Tiny CLI for the release-artifact action: `filename <pkg>` prints the
    # registered filename for pkg, or exits 1 if unknown.
    if len(sys.argv) == 3 and sys.argv[1] == "filename":
        pkg = sys.argv[2]
        match = next((a for a in ARTIFACTS if a.pkg == pkg), None)
        if match is None:
            print(f"unknown pkg: {pkg}", file=sys.stderr)
            sys.exit(1)
        print(match.filename)
    else:
        print("usage: artifacts.py filename <pkg>", file=sys.stderr)
        sys.exit(2)
