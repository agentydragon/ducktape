"""Tests for the require-secret-store-conditions Kyverno policy."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel

from cluster.validation.kyverno.apply import apply_policy
from cluster.validation.kyverno.paths import manifest, policy


@pytest.fixture
def secret_store_conditions_policy() -> Path:
    return policy("require-secret-store-conditions.yaml")


class TestRequireSecretStoreConditions:
    """An unfenced ClusterSecretStore is readable from every namespace.

    Kind-scoped with no subject match, so `kyverno apply` exercises it directly.
    """

    def test_missing_conditions_denied(self, secret_store_conditions_policy: Path):
        result = apply_policy(secret_store_conditions_policy, manifest("cluster_secret_store_no_conditions.yaml"))
        assert result.failed == 1, f"store without conditions must be denied\n{result.stdout}"

    def test_empty_conditions_denied(self, secret_store_conditions_policy: Path):
        """`conditions: []` is not a tightening — ESO treats it like an absent field."""
        result = apply_policy(secret_store_conditions_policy, manifest("cluster_secret_store_empty_conditions.yaml"))
        assert result.failed == 1, f"store with empty conditions must be denied\n{result.stdout}"

    def test_empty_namespaces_denied(self, secret_store_conditions_policy: Path):
        """A condition can be present and still name nothing."""
        result = apply_policy(secret_store_conditions_policy, manifest("cluster_secret_store_empty_namespaces.yaml"))
        assert result.failed == 1, f"condition with empty namespaces must be denied\n{result.stdout}"

    def test_bare_namespace_selector_denied(self, secret_store_conditions_policy: Path):
        """An empty LabelSelector matches every namespace — the widest setting, not a fence."""
        result = apply_policy(secret_store_conditions_policy, manifest("cluster_secret_store_bare_selector.yaml"))
        assert result.failed == 1, f"bare namespaceSelector must be denied\n{result.stdout}"

    def test_declared_conditions_allowed(self, secret_store_conditions_policy: Path):
        result = apply_policy(secret_store_conditions_policy, manifest("cluster_secret_store_with_conditions.yaml"))
        assert result.failed == 0, f"store naming its namespaces must pass\n{result.stdout}"


if __name__ == "__main__":
    pytest_bazel.main()
