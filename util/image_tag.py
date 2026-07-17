"""Commit provenance parsed from Flux-selected ducktape image tags.

Deployment automation tags images ``<branch>-<YYYYMMDDHHMMSS>-<commit>``
(see cluster/docs/container-images.md). Apps receive their Flux-selected tag
via an env var and link "deployed version" UI to the trailing commit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_DUCKTAPE_REPO_URL = "https://github.com/agentydragon/ducktape"
_IMAGE_TAG_RE = re.compile(r"^[A-Za-z0-9_.-]+-\d{14}-(?P<commit>[0-9a-f]{7,40})$")


@dataclass(frozen=True)
class ImageProvenance:
    image_tag: str | None
    source_commit: str | None
    source_commit_url: str | None


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def image_provenance(image_tag: str | None, *, source_commit: str | None = None) -> ImageProvenance:
    """Parse commit provenance from an automation image tag.

    An explicit ``source_commit`` (runtime env override) wins over the tag's
    trailing commit. A tag that doesn't match the automation format (e.g.
    ``latest``) is kept verbatim with no commit; blank values mean absent.
    """
    tag = _clean(image_tag)
    match = _IMAGE_TAG_RE.match(tag) if tag else None
    commit = _clean(source_commit) or (match.group("commit") if match else None)
    return ImageProvenance(
        image_tag=tag,
        source_commit=commit,
        source_commit_url=f"{_DUCKTAPE_REPO_URL}/commit/{commit}" if commit else None,
    )
