"""Deployment metadata exposed by the Augur API."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from pydantic import Field

from finance.augur.api.schemas import ApiModel

_DUCKTAPE_REPO_URL = "https://github.com/agentydragon/ducktape"
_IMAGE_TAG_RE = re.compile(r"^[A-Za-z0-9_.-]+-\d{14}-(?P<commit>[0-9a-f]{7,40})$")


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


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _commit_from_tag(tag: str | None) -> str | None:
    if tag is None:
        return None
    match = _IMAGE_TAG_RE.match(tag)
    if match is None:
        return None
    return match.group("commit")


def _commit_url(commit: str | None) -> str | None:
    if commit is None:
        return None
    return f"{_DUCKTAPE_REPO_URL}/commit/{commit}"


def _image_info(*, image_tag: str | None, source_commit: str | None = None) -> DeploymentImageInfo:
    commit = _clean(source_commit) or _commit_from_tag(image_tag)
    return DeploymentImageInfo(image_tag=image_tag, source_commit=commit, source_commit_url=_commit_url(commit))


def build_deployment_info(env: Mapping[str, str] | None = None) -> DeploymentInfo:
    source = env if env is not None else os.environ
    api_image_tag = _clean(source.get("AUGUR_API_IMAGE_TAG") or source.get("AUGUR_IMAGE_TAG"))
    frontend_image_tag = _clean(source.get("AUGUR_FRONTEND_IMAGE_TAG"))
    api_commit = _clean(source.get("AUGUR_API_SOURCE_COMMIT") or source.get("AUGUR_DEPLOYED_COMMIT"))
    frontend_commit = _clean(source.get("AUGUR_FRONTEND_SOURCE_COMMIT"))
    return DeploymentInfo(
        api=_image_info(image_tag=api_image_tag, source_commit=api_commit),
        frontend=_image_info(image_tag=frontend_image_tag, source_commit=frontend_commit),
    )
