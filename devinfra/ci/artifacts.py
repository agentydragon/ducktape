"""Shared artifact definitions and SHA-256 helpers for the release pipeline."""

import base64
import hashlib
from collections.abc import Iterable
from pathlib import Path

import requests
from pydantic import BaseModel, Field

SOURCES_PATH = Path("npins/sources.json")


class Pin(BaseModel):
    url: str
    sha256: str


class Sources(BaseModel):
    pins: dict[str, Pin]


class Artifact(BaseModel, frozen=True):
    pkg: str = Field(description="npins package name")
    bazel_target: str = Field(description="Bazel target that builds this artifact")
    src_glob: str = Field(description="Path glob under bazel-bin/")
    dest: str = Field(description="Destination path or directory under dist/")
    notes: str = Field(description="GitHub release body")
    filename: str = Field(description="Artifact filename attached to the GitHub release")


ARTIFACTS = [
    Artifact(
        pkg="claude-hooks",
        bazel_target="//:claude_hooks_wheel",
        src_glob="bazel-bin/claude_hooks-*.whl",
        dest="dist/",
        notes="claude-hooks wheel for Claude Code integration.",
        filename="claude_hooks-0.1.0-py3-none-any.whl",
    ),
    Artifact(
        pkg="ducktape",
        bazel_target="//:wheel",
        src_glob="bazel-bin/ducktape-*.whl",
        dest="dist/",
        notes="ducktape wheel containing CLI tools.",
        filename="ducktape-0.1.0-py3-none-any.whl",
    ),
    Artifact(
        pkg="gterm-theme",
        bazel_target="//gterm_theme:wheel",
        src_glob="bazel-bin/gterm_theme/gterm_theme-*.whl",
        dest="dist/",
        notes="gterm-theme wheel (GNOME Terminal theme follower).",
        filename="gterm_theme-0.1.0-py3-none-any.whl",
    ),
    Artifact(
        pkg="skills",
        bazel_target="//skills:all_skills_tar",
        src_glob="bazel-bin/skills/all_skills_tar.tar",
        dest="dist/skills.tar",
        notes="Skills tarball for AI agent deployment.",
        filename="skills.tar",
    ),
    Artifact(
        pkg="bbapi",
        bazel_target="//devinfra/buildbuddy_cli:bbapi",
        src_glob="bazel-bin/devinfra/buildbuddy_cli/bbapi_/bbapi",
        dest="dist/",
        notes="bbapi — BuildBuddy API CLI (Linux x86_64).",
        filename="bbapi",
    ),
]


def _sha256_of_chunks(chunks: Iterable[bytes]) -> str:
    h = hashlib.sha256()
    for chunk in chunks:
        h.update(chunk)
    return base64.b64encode(h.digest()).decode()


def file_sha256(path: Path) -> str:
    with path.open("rb") as f:
        return _sha256_of_chunks(iter(lambda: f.read(65536), b""))


def url_sha256(url: str) -> str:
    with requests.get(url, stream=True) as response:
        response.raise_for_status()
        return _sha256_of_chunks(response.iter_content(65536))
