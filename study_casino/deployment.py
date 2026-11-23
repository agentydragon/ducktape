"""Deployment metadata exposed by the Study Casino API."""

from __future__ import annotations

import os

from study_casino.state import DeploymentInfo
from util.image_tag import image_provenance


def build_deployment_info() -> DeploymentInfo:
    info = image_provenance(os.environ.get("STUDY_CASINO_IMAGE_TAG"))
    return DeploymentInfo(
        image_tag=info.image_tag, source_commit=info.source_commit, source_commit_url=info.source_commit_url
    )
