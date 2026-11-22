"""Shared artifact definitions and SHA-256 helpers for the release pipeline."""

import base64
import hashlib
import json
from collections.abc import Iterable
from pathlib import Path

import httpx
from pydantic import BaseModel, Field

from devinfra.bazel.workspace import get_build_workspace_directory


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
    filename: str
    release: str


class ArtifactTargets(BaseModel):
    pins: dict[str, ArtifactTarget]


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


def _skill_artifacts() -> list[Artifact]:
    """One pin-managed Artifact per Nix-enabled skill in the registry.

    Each skill releases independently as `skill-<name>-<hash>` carrying a single
    `<name>.skill` asset. Nix-enabled skills also get a `skill-<name>` pin in
    artifact-pins.json.
    """
    registry = json.loads(skills_registry_path().read_text())
    return [Artifact(pkg=s["pkg"], filename=s["filename"]) for s in registry["skills"] if s.get("nix", True)]


def _release_artifacts() -> list[Artifact]:
    targets = ArtifactTargets.model_validate_json(artifact_targets_path().read_text())
    return [
        Artifact(pkg=pkg, filename=target.filename, tag_pkg=target.release if target.release != pkg else None)
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
