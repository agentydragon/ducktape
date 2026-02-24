"""Build info for the props backend, derived from the PROPS_IMAGE_TAG env var.

Image tags have the format: devel-YYYYMMDDHHMMSS-sha7
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from functools import lru_cache

from pydantic import BaseModel


class BuildInfo(BaseModel, frozen=True):
    commit: str
    commit_time: str


@lru_cache(maxsize=1)
def get_build_info() -> BuildInfo:
    tag = os.environ.get("PROPS_IMAGE_TAG", "")
    parts = tag.split("-")
    if len(parts) == 3 and parts[0] == "devel":
        _, compact_time, commit = parts
        dt = datetime.strptime(compact_time, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        return BuildInfo(commit=commit, commit_time=dt.isoformat())
    return BuildInfo(commit="dev", commit_time="dev")
