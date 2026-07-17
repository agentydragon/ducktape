"""Deployment metadata exposed by the Study Casino API."""

from __future__ import annotations

import os
from collections.abc import Mapping

from util.image_tag import image_provenance
from x.study_casino.state import DeploymentInfo


def build_deployment_info(env: Mapping[str, str] | None = None) -> DeploymentInfo:
    source = env if env is not None else os.environ
    info = image_provenance(
        source.get("STUDY_CASINO_IMAGE_TAG"),
        source_commit=source.get("STUDY_CASINO_SOURCE_COMMIT") or source.get("STUDY_CASINO_DEPLOYED_COMMIT"),
    )
    return DeploymentInfo(
        image_tag=info.image_tag, source_commit=info.source_commit, source_commit_url=info.source_commit_url
    )
