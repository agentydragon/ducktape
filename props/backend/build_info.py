"""Build info for the props backend, derived from the PROPS_IMAGE_TAG env var."""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import BaseModel


class BuildInfo(BaseModel, frozen=True):
    image_tag: str


@lru_cache(maxsize=1)
def get_build_info() -> BuildInfo:
    return BuildInfo(image_tag=os.environ.get("PROPS_IMAGE_TAG", "dev"))
