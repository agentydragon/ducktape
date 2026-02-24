"""Build info for the props backend, derived from the PROPS_IMAGE_TAG env var.

Image tags have the format: devel-YYYY-MM-DDTHHMMSSZ-sha7
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import BaseModel


class BuildInfo(BaseModel, frozen=True):
    commit: str
    commit_time: str


@lru_cache(maxsize=1)
def get_build_info() -> BuildInfo:
    tag = os.environ.get("PROPS_IMAGE_TAG", "")
    # Tag format: devel-YYYY-MM-DDTHHMMSSZ-sha7
    # rsplit on the last "-" to isolate the sha7; removeprefix to drop "devel-".
    if tag.startswith("devel-") and tag.count("-") >= 2:
        prefix, commit = tag.rsplit("-", 1)
        commit_time = prefix.removeprefix("devel-")
        return BuildInfo(commit=commit, commit_time=commit_time)
    return BuildInfo(commit="dev", commit_time="dev")
