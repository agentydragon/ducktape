"""Tests for check_image_automation_webhook.

Drives the check the way the validator does — through `ParsedCluster.build_results`
(typed resources parsed from the kustomize build output) — so it exercises the same
path that runs in CI / pre-commit. The real-tree coverage lives in
test_cluster_integration.py::test_image_automation_webhook_consistency.
"""

from pathlib import Path
from typing import Any

import pytest_bazel

from cluster.validation.cluster import ParsedCluster
from cluster.validation.image_automation import check_image_automation_webhook
from cluster.validation.k8s import parse_k8s_resources
from cluster.validation.kustomize import KustomizeBuildResult


def _repo(name: str, image: str | None = None) -> dict[str, Any]:
    return {
        "apiVersion": "image.toolkit.fluxcd.io/v1beta2",
        "kind": "ImageRepository",
        "metadata": {"name": name, "namespace": "flux-system"},
        "spec": {"image": image or f"ghcr.io/agentydragon/{name}"},
    }


def _policy(name: str, repo: str) -> dict[str, Any]:
    return {
        "apiVersion": "image.toolkit.fluxcd.io/v1",
        "kind": "ImagePolicy",
        "metadata": {"name": name, "namespace": "flux-system"},
        "spec": {"imageRepositoryRef": {"name": repo}},
    }


def _receiver(repos: list[str]) -> dict[str, Any]:
    return {
        "apiVersion": "notification.toolkit.fluxcd.io/v1",
        "kind": "Receiver",
        "metadata": {"name": "github", "namespace": "flux-system"},
        "spec": {
            "type": "github",
            "resources": [
                {"apiVersion": "image.toolkit.fluxcd.io/v1beta2", "kind": "ImageRepository", "name": r} for r in repos
            ],
        },
    }


def _cluster(*docs: dict[str, Any]) -> ParsedCluster:
    """A ParsedCluster whose single build result renders the given manifests."""
    result = KustomizeBuildResult(
        kustomization_path=Path("flux/kustomization.yaml"), resources=parse_k8s_resources(docs)
    )
    return ParsedCluster(build_results=[result])


def test_consistent_is_clean() -> None:
    cluster = _cluster(_repo("foo"), _policy("foo", "foo"), _receiver(["foo"]))
    assert check_image_automation_webhook(cluster) == []


def test_repository_missing_from_webhook() -> None:
    cluster = _cluster(_repo("foo"), _policy("foo", "foo"), _receiver([]))
    errors = check_image_automation_webhook(cluster)
    assert any("foo" in e and "github-webhook-receiver" in e for e in errors)


def test_non_ghcr_repository_exempt_from_webhook() -> None:
    # Non-GHCR (e.g. Forgejo) images can't use the GitHub registry_package webhook,
    # so they aren't required in the Receiver.
    cluster = _cluster(
        _repo("codex-pod", image="git.allegedly.works/ducktape-ci/codex-pod"),
        _policy("codex-pod", "codex-pod"),
        _receiver([]),
    )
    assert check_image_automation_webhook(cluster) == []


def test_stale_webhook_entry() -> None:
    cluster = _cluster(_repo("foo"), _policy("foo", "foo"), _receiver(["foo", "gone"]))
    errors = check_image_automation_webhook(cluster)
    assert any("gone" in e for e in errors)


def test_dangling_policy_reference() -> None:
    cluster = _cluster(_policy("bar", "ghost"), _receiver([]))
    errors = check_image_automation_webhook(cluster)
    assert any("bar" in e and "ghost" in e for e in errors)


if __name__ == "__main__":
    pytest_bazel.main()
