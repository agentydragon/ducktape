"""Tests for the require-secret-store-conditions Kyverno policy."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel
from cdk8s import App

from cluster.cdk8s.kyverno.apply import apply_policy
from cluster.cdk8s.kyverno.test_utils import manifest, policy


@pytest.fixture
def secret_store_conditions_policy(scratch_app: App, tmp_path: Path) -> Path:
    return policy(scratch_app, "require-secret-store-conditions", tmp_path)


class TestRequireSecretStoreConditions:
    """An unfenced ClusterSecretStore is readable from every namespace.

    Kind-scoped with no subject match, so `kyverno apply` exercises it directly.
    """

    @pytest.mark.parametrize(
        "manifest_name",
        [
            "cluster_secret_store_no_conditions.yaml",
            # ESO treats an empty conditions list like an absent field.
            "cluster_secret_store_empty_conditions.yaml",
            "cluster_secret_store_empty_namespaces.yaml",
            # An empty LabelSelector matches every namespace, so it provides no fence.
            "cluster_secret_store_bare_selector.yaml",
        ],
    )
    def test_non_fencing_conditions_are_denied(self, secret_store_conditions_policy: Path, manifest_name: str) -> None:
        result = apply_policy(secret_store_conditions_policy, manifest(manifest_name))
        assert result.failed == 1, f"{manifest_name} must be denied\n{result.stdout}"

    def test_declared_conditions_allowed(self, secret_store_conditions_policy: Path):
        result = apply_policy(secret_store_conditions_policy, manifest("cluster_secret_store_with_conditions.yaml"))
        assert result.failed == 0, f"store naming its namespaces must pass\n{result.stdout}"


if __name__ == "__main__":
    pytest_bazel.main()
