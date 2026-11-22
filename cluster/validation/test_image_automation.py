"""Flux registry webhook target resolution."""

from typing import Any

import pytest_bazel

from cluster.validation.cluster import ParsedCluster
from cluster.validation.image_automation import check_image_automation_webhook
from cluster.validation.k8s import parse_k8s_resources


def _repo(name: str) -> dict[str, Any]:
    return {"kind": "ImageRepository", "metadata": {"name": name, "namespace": "flux-system"}}


def _receiver(namespace: str, *repos: dict[str, str]) -> dict[str, Any]:
    return {
        "kind": "Receiver",
        "metadata": {"name": "webhook", "namespace": namespace},
        "spec": {"resources": [{"kind": "ImageRepository", **ref} for ref in repos]},
    }


def _cluster(*docs: dict[str, Any]) -> ParsedCluster:
    """A ParsedCluster whose single build result renders the given manifests."""
    return ParsedCluster(rendered_resources={"test-image-automation": parse_k8s_resources(docs)})


def test_receiver_resolves_local_and_cross_namespace_references() -> None:
    cluster = _cluster(
        _repo("local"),
        _repo("remote"),
        _receiver("flux-system", {"name": "local"}),
        _receiver("forgejo-images", {"name": "remote", "namespace": "flux-system"}),
    )
    assert check_image_automation_webhook(cluster) == []


def test_wrong_namespace_reports_both_the_unwatched_repo_and_the_dangling_ref() -> None:
    cluster = _cluster(_repo("image"), _receiver("forgejo-images", {"name": "image"}))
    missing, stale = check_image_automation_webhook(cluster)
    assert "flux-system/image" in missing
    assert "forgejo-images/image" in stale


if __name__ == "__main__":
    pytest_bazel.main()
