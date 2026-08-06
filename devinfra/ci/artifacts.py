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


def artifact_targets_path() -> Path:
    return get_build_workspace_directory() / "devinfra" / "ci" / "artifact_targets.json"


def skills_registry_path() -> Path:
    return get_build_workspace_directory() / "skills" / "skills_registry.json"


class Pin(BaseModel):
    url: str
    sha256: str


class Sources(BaseModel):
    pins: dict[str, Pin]


class ArtifactTarget(BaseModel):
    output: str
    release: str


class ArtifactTargets(BaseModel):
    pins: dict[str, ArtifactTarget]


_HEX_SUFFIX_RE = re.compile(r"^[0-9a-f]{7}$|^[0-9a-f]{12}$")


def is_tag_for_pkg(tag: str, pkg: str) -> bool:
    """Match `{pkg}-{7hex}` or `{pkg}-{12hex}` exactly, avoiding prefix collisions."""
    prefix = f"{pkg}-"
    if not tag.startswith(prefix):
        return False
    return _HEX_SUFFIX_RE.match(tag[len(prefix) :]) is not None


def tag_package(tag: str) -> str | None:
    """Return the package a release tag belongs to, or None if it is not one of ours."""
    pkg, _, suffix = tag.rpartition("-")
    return pkg if pkg and _HEX_SUFFIX_RE.match(suffix) else None


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


def _release_artifacts() -> list["Artifact"]:
    targets = ArtifactTargets.model_validate_json(artifact_targets_path().read_text())
    return [
        Artifact(pkg=pkg, filename=Path(target.output).name, tag_pkg=target.release if target.release != pkg else None)
        for pkg, target in targets.pins.items()
    ]


ARTIFACTS = [*_release_artifacts(), *_skill_artifacts()]


def _sha256_of_chunks(chunks: Iterable[bytes]) -> str:
    h = hashlib.sha256()
    for chunk in chunks:
        h.update(chunk)
    return base64.b64encode(h.digest()).decode()


def url_sha256(url: str) -> str:
    with httpx.stream("GET", url, follow_redirects=True) as response:
        response.raise_for_status()
        return _sha256_of_chunks(response.iter_bytes(65536))
