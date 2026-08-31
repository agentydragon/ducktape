"""Runtime deployment metadata derived from Flux-selected image tags."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from util.image_tag import image_provenance


class DeploymentImageInfo(BaseModel):
    image_tag: str | None = Field(
        default=None,
        description=(
            "Container image tag selected by deployment automation, when provided by the runtime manifest. "
            "This is desired deployment metadata, not an attestation that every replica serves the tag."
        ),
    )
    source_commit: str | None = Field(default=None, description="Ducktape commit parsed from image_tag.")
    source_commit_url: str | None = Field(
        default=None, description="GitHub URL for source_commit when image_tag identifies a Ducktape build."
    )


class DeploymentInfo(BaseModel):
    server: DeploymentImageInfo = Field(description="Haku console API image metadata.")
    frontend: DeploymentImageInfo = Field(description="Haku console static frontend image metadata.")


def _image_info(image_tag: str | None) -> DeploymentImageInfo:
    info = image_provenance(image_tag)
    return DeploymentImageInfo(
        image_tag=info.image_tag, source_commit=info.source_commit, source_commit_url=info.source_commit_url
    )


def _projected_tag(path: Path | None) -> str | None:
    """Read optional Flux metadata from a projected ConfigMap volume.

    ConfigMap projection is eventually consistent. A missing or temporarily unreadable
    file must make the frontend revision unknown, never make the Console API unavailable.
    """
    if path is None:
        return None
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def build_deployment_info(
    *, image_tag: str | None, static_image_tag: str | None, static_image_tag_file: Path | None = None
) -> DeploymentInfo:
    return DeploymentInfo(
        server=_image_info(image_tag), frontend=_image_info(_projected_tag(static_image_tag_file) or static_image_tag)
    )
