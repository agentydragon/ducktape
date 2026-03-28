"""Shared artifact definitions and SHA-256 helpers for the release pipeline."""

import base64
import hashlib
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

SOURCES_PATH = Path("npins/sources.json")


class Pin(BaseModel):
    url: str
    sha256: str


class Sources(BaseModel):
    pins: dict[str, Pin]


@dataclass(frozen=True)
class Artifact:
    pkg: str
    bazel_target: str
    src_glob: str  # path glob under bazel-bin/
    dest: str  # destination path or directory under dist/
    notes: str  # GitHub release body
    filename: str  # artifact filename attached to the GitHub release


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


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return base64.b64encode(h.digest()).decode()


def url_sha256(url: str) -> str:
    h = hashlib.sha256()
    with urllib.request.urlopen(url) as response:
        while chunk := response.read(65536):
            h.update(chunk)
    return base64.b64encode(h.digest()).decode()
