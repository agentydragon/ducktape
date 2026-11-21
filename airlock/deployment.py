"""Deployment metadata exposed by the Airlock API.

The deployed image tag is injected into the pod via the `AIRLOCK_IMAGE_TAG`
env var (kept fresh by Flux image automation); the trailing commit sha links
back to GitHub.
"""

from __future__ import annotations

import os

from airlock.models import DeploymentInfo
from util.image_tag import image_provenance


def build_deployment_info() -> DeploymentInfo:
    info = image_provenance(os.environ.get("AIRLOCK_IMAGE_TAG"))
    return DeploymentInfo(
        image_tag=info.image_tag, source_commit=info.source_commit, source_commit_url=info.source_commit_url
    )
