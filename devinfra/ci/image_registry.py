"""Registry names and digest lookups for the bare-Python CI publication jobs."""

from __future__ import annotations

import re
from enum import StrEnum

from devinfra.oci.crane import Crane, CraneError
from devinfra.oci.image_tags import pinned_tag_pattern


class Registry(StrEnum):
    """Where an image is published.

    Not a free-form URL: the name keys an image's `registry` in
    `image_targets.json`, travels through the plan matrix, and gates the
    credential step `push-images.yml` runs before the push. So the set is closed
    by what CI can authenticate to, and a new member needs a login step beside
    its prefix.
    """

    GHCR = "ghcr"
    FORGEJO = "forgejo"


REGISTRY_PREFIX = {Registry.GHCR: "ghcr.io/agentydragon", Registry.FORGEJO: "git.allegedly.works/ducktape-ci"}

#: `rules_oci` writes an image's content digest to this sidecar of the OCI layout.
DIGEST_SUFFIX = ".json.sha256"


def repo_for(name: str, registry: Registry) -> str:
    return f"{REGISTRY_PREFIX[registry]}/{name}"


def registry_digest(crane: Crane, repo: str) -> str | None:
    """Read the last completed publication. The publisher moves latest after pushing
    the pinned tag, so an interrupted publication can be retried."""
    return crane.digest_or_none(f"{repo}:latest")


def newest_pinned_digest(crane: Crane, repo: str, tag_prefix: str) -> str | None:
    """Read the newest immutable branch tag selected by Flux."""
    pattern = re.compile(pinned_tag_pattern(tag_prefix))
    try:
        tags = crane.tags(repo)
    except CraneError as exc:
        if "NAME_UNKNOWN" in exc.stderr:
            return None
        raise
    newest = max((tag for tag in tags if pattern.fullmatch(tag)), default=None)
    return crane.digest(f"{repo}:{newest}") if newest is not None else None
