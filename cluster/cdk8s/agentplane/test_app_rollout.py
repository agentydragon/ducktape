"""The Locator-column scope change must not overlap old and new app replicas."""

from typing import Any

import pytest_bazel


def test_app_recreates_before_migration_in_both_environments(
    agentplane_manifests: dict[str, list[dict[str, Any]]],
) -> None:
    assert set(agentplane_manifests) == {"agentplane-staging", "agentplane-testing"}
    for namespace, documents in agentplane_manifests.items():
        deployments = {doc["metadata"]["name"]: doc for doc in documents if doc["kind"] == "Deployment"}
        app = deployments["agentplane-app"]
        assert app["spec"]["strategy"] == {"type": "Recreate"}
        assert any(c["name"] == "migrate" for c in app["spec"]["template"]["spec"]["initContainers"])
        # This temporary availability tradeoff is specific to the app, not the archive.
        expected_archive_strategy = "RollingUpdate" if namespace == "agentplane-staging" else "Recreate"
        assert deployments["agentplane-sandbox-service"]["spec"]["strategy"]["type"] == expected_archive_strategy


if __name__ == "__main__":
    pytest_bazel.main()
