"""Deployment metadata exposed by the Augur API."""

from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic import Field

from finance.augur.api.schemas import ApiModel
from util.image_tag import image_provenance


class DeploymentImageInfo(ApiModel):
    image_tag: str | None = Field(
        default=None,
        description="Container image tag selected by deployment automation, when provided by the runtime manifest.",
    )
    source_commit: str | None = Field(
        default=None, description="Git commit inferred from the image tag or explicit runtime environment."
    )
    source_commit_url: str | None = Field(
        default=None, description="GitHub URL for source_commit when it points at the ducktape repository."
    )


class DeploymentInfo(ApiModel):
    api: DeploymentImageInfo = Field(description="Backend API image metadata.")
    frontend: DeploymentImageInfo = Field(description="Static frontend image metadata.")


def _image_info(*, image_tag: str | None, source_commit: str | None = None) -> DeploymentImageInfo:
    info = image_provenance(image_tag, source_commit=source_commit)
    return DeploymentImageInfo(
        image_tag=info.image_tag, source_commit=info.source_commit, source_commit_url=info.source_commit_url
    )


def build_deployment_info(env: Mapping[str, str] | None = None) -> DeploymentInfo:
    source = env if env is not None else os.environ
    return DeploymentInfo(
        api=_image_info(
            image_tag=source.get("AUGUR_API_IMAGE_TAG") or source.get("AUGUR_IMAGE_TAG"),
            source_commit=source.get("AUGUR_API_SOURCE_COMMIT") or source.get("AUGUR_DEPLOYED_COMMIT"),
        ),
        frontend=_image_info(
            image_tag=source.get("AUGUR_FRONTEND_IMAGE_TAG"), source_commit=source.get("AUGUR_FRONTEND_SOURCE_COMMIT")
        ),
    )
