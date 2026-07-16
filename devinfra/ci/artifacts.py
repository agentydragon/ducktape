"""Shared artifact definitions and SHA-256 helpers for the release pipeline."""

import base64
import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import Path

import httpx
from pydantic import BaseModel, Field

from util.bazel.workspace import get_build_workspace_directory


def sources_path() -> Path:
    return get_build_workspace_directory() / "nix" / "artifact-pins.json"


def skills_registry_path() -> Path:
    return get_build_workspace_directory() / "skills" / "skills_registry.json"


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
    pkg: str = Field(description="artifact pin name (artifact-pins.json key)")
    filename: str = Field(description="Artifact filename attached to the GitHub release")
    tag_pkg: str | None = Field(
        default=None,
        description=(
            "Release tag prefix (e.g. 'aiquota' matches 'aiquota-<sha>' tags). "
            "Defaults to `pkg`. Use when multiple artifact pins share a single release tag."
        ),
    )

    @property
    def release_tag_prefix(self) -> str:
        return self.tag_pkg or self.pkg


def _skill_artifacts() -> list["Artifact"]:
    """One Artifact per deployable skill, from skills/skills_registry.json.

    Each skill releases independently as `skill-<name>-<hash>` carrying a single
    `<name>.skill` asset, and pins under the `skill-<name>` key in
    artifact-pins.json.
    """
    registry = json.loads(skills_registry_path().read_text())
    return [Artifact(pkg=s["pkg"], filename=s["filename"]) for s in registry["skills"]]


ARTIFACTS = [
    Artifact(pkg="bbr", filename="bbr-0.1.0-py3-none-any.whl"),
    Artifact(pkg="claude-hooks", filename="claude_hooks-0.1.0-py3-none-any.whl"),
    Artifact(pkg="claude-hook-rs", filename="claude_hook"),
    Artifact(pkg="ducktape-git-hooks", filename="ducktape_git_hooks-0.1.0-py3-none-any.whl"),
    Artifact(pkg="ducktape-util", filename="ducktape_util-0.1.0-py3-none-any.whl"),
    Artifact(pkg="ducktape", filename="ducktape-0.1.0-py3-none-any.whl"),
    Artifact(pkg="aiquota", filename="aiquota-0.1.0-py3-none-any.whl"),
    Artifact(pkg="aiquota-extension", tag_pkg="aiquota", filename="aiquota.zip"),
    Artifact(pkg="gterm-theme", filename="gterm_theme-0.1.0-py3-none-any.whl"),
    # CLEANUP(added 2026-07-16): drop the combined bundle once nix stops
    #   reading the `skills` pin (i.e. flake.nix / nix/packages assemble from the
    #   per-skill `skill-*` pins instead — PR 2 of the all_skills teardown).
    Artifact(pkg="skills", filename="all_skills.skill"),
    Artifact(pkg="bbapi", filename="bbapi"),
    Artifact(pkg="debundle", filename="debundle"),
    *_skill_artifacts(),
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
