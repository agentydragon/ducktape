"""Runtime deployment metadata derived from Flux-selected image tags."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from pydantic import BaseModel, Field

_DUCKTAPE_REPO_URL = "https://github.com/agentydragon/ducktape"
_IMAGE_TAG_RE = re.compile(r"^[A-Za-z0-9_.-]+-\d{14}-(?P<commit>[0-9a-f]{7,40})$")


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
    tag = image_tag.strip() if image_tag and image_tag.strip() else None
    match = _IMAGE_TAG_RE.match(tag) if tag else None
    commit = match.group("commit") if match else None
    return DeploymentImageInfo(
        image_tag=tag,
        source_commit=commit,
        source_commit_url=f"{_DUCKTAPE_REPO_URL}/commit/{commit}" if commit else None,
    )


def build_deployment_info(env: Mapping[str, str] | None = None) -> DeploymentInfo:
    source = env if env is not None else os.environ
    return DeploymentInfo(
        server=_image_info(source.get("HAKU_CONSOLE_IMAGE_TAG")),
        frontend=_image_info(source.get("HAKU_CONSOLE_STATIC_IMAGE_TAG")),
    )
