from pathlib import Path

import pytest_bazel

from devinfra.ci.image_registry import REGISTRY_PREFIX, Registry, registry_digest, repo_for
from util.crane import Crane


class FakeCrane(Crane):
    """Registry stub: tag -> digest. An absent tag was never pushed."""

    def __init__(self, digests: dict[str, str]) -> None:
        super().__init__(Path("/nonexistent/crane"))
        self.digests = digests
        self.asked: list[str] = []

    def digest_or_none(self, image_ref: str) -> str | None:
        self.asked.append(image_ref)
        return self.digests.get(image_ref)


def test_the_registry_digest_is_whatever_latest_points_at() -> None:
    crane = FakeCrane({"r:latest": "sha256:current"})
    assert registry_digest(crane, "r") == "sha256:current"
    assert crane.asked == ["r:latest"], "one round-trip, and no tag listing"


def test_a_repository_never_pushed_to_has_no_registry_digest() -> None:
    assert registry_digest(FakeCrane({}), "r") is None


def test_pinned_tags_are_not_consulted() -> None:
    """The publish check goes through the moving tag, so ImagePolicy's newest-first
    ordering over `devel-*` stays in the cluster rather than being restated here."""
    crane = FakeCrane({"r:devel-20260827054143-96b61f5": "sha256:pinned"})
    assert registry_digest(crane, "r") is None


def test_repo_url_is_the_one_the_cluster_pulls() -> None:
    """Exact values, because they are an external contract: `cluster/k8s/**` pins
    `ghcr.io/agentydragon/<name>` and `git.allegedly.works/ducktape-ci/<name>` in
    its manifests, and Flux ImagePolicy watches those repositories. Building the
    expectation from REGISTRY_PREFIX would restate the implementation instead."""
    assert repo_for("airlock", Registry.GHCR) == "ghcr.io/agentydragon/airlock"
    assert repo_for("cpap-gateway", Registry.FORGEJO) == "git.allegedly.works/ducktape-ci/cpap-gateway"


def test_every_registry_has_a_prefix() -> None:
    """A new member without one would fail only at the push that needs it."""
    assert set(REGISTRY_PREFIX) == set(Registry)


if __name__ == "__main__":
    pytest_bazel.main()
