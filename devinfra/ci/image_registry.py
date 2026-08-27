"""What both halves of image publishing need to know about the registry.

The planner decides *whether* an image needs pushing from the digests `bazel-ci`
already built; the per-image push job decides the same thing again from the digest
it built itself, because the planner fails open and its "push it to be safe" verdict
is a guess, not a change. Both compare against the same thing — the content behind
`latest` — so that read and the repository naming live here rather than in each.

Talking to the registry is `util.crane`'s job, not this module's: it is standard
library only precisely so both halves can import it as bare `python3 -m` on a
GitHub Actions runner.
"""

from __future__ import annotations

from enum import StrEnum

from util.crane import Crane


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
    """Content of the most recent publish to `repo`, or None if there has not been one.

    `latest` answers this, so nothing here has to know how Flux picks a tag. The
    push job is the only writer and moves `latest` onto the bytes it just pushed, so
    "are these bytes already published" is a plain moving-tag question. Reading the
    tag list and re-deriving ImagePolicy's newest-`devel-*` rule would answer the
    same question by restating a convention that lives in the cluster.

    It fails safe rather than exactly: a push that dies between the pinned tag and
    `latest` leaves `latest` behind, and the next run republishes. Nothing makes it
    claim published content is unpublished's opposite — `latest` only ever points at
    something this job pushed.
    """
    return crane.digest_or_none(f"{repo}:latest")
