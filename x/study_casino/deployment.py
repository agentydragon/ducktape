"""Deployment metadata exposed by the Study Casino API."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from x.study_casino.state import DeploymentInfo

_DUCKTAPE_REPO_URL = "https://github.com/agentydragon/ducktape"
_IMAGE_TAG_RE = re.compile(r"^[A-Za-z0-9_.-]+-\d{14}-(?P<commit>[0-9a-f]{7,40})$")


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


def build_deployment_info(env: Mapping[str, str] | None = None) -> DeploymentInfo:
    source = env if env is not None else os.environ
    image_tag = _clean(source.get("STUDY_CASINO_IMAGE_TAG"))
    source_commit = _clean(source.get("STUDY_CASINO_SOURCE_COMMIT") or source.get("STUDY_CASINO_DEPLOYED_COMMIT"))
    commit = source_commit or _commit_from_tag(image_tag)
    return DeploymentInfo(image_tag=image_tag, source_commit=commit, source_commit_url=_commit_url(commit))
