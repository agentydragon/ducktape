"""Runtime deployment metadata derived from Flux-selected image tags."""

from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic import BaseModel, Field

from util.image_tag import image_provenance


class DeploymentImageInfo(BaseModel):
    image_tag: str | None = Field(
        default=None,
        description="Container image tag selected by deployment automation, when provided by the runtime manifest.",
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


def build_deployment_info(env: Mapping[str, str] | None = None) -> DeploymentInfo:
    source = env if env is not None else os.environ
    return DeploymentInfo(
        server=_image_info(source.get("HAKU_CONSOLE_IMAGE_TAG")),
        frontend=_image_info(source.get("HAKU_CONSOLE_STATIC_IMAGE_TAG")),
    )
