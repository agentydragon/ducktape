import pytest_bazel

from devinfra.ci.image_registry import REGISTRY_PREFIX, Registry, repo_for


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
