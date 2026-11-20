"""Tests for the require-secret-store-conditions Kyverno policy."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel
from cdk8s import App, Testing as Cdk8sTesting  # pytest would collect `Testing` as a test class

from cluster.cdk8s.kyverno import policies
from cluster.cdk8s.kyverno.apply import apply_policy
from cluster.cdk8s.kyverno.test_utils import manifest, policy


@pytest.fixture
def secret_store_conditions_policy(scratch_app: App, tmp_path: Path) -> Path:
    return policy(scratch_app, "require-secret-store-conditions", tmp_path, json_mode=True)


def test_cluster_store_policy_enables_discoverable_v1_background_scan(scratch_app: App) -> None:
    document = Cdk8sTesting.synth(policies.require_secret_store_conditions_chart(scratch_app))[0]
    spec = document["spec"]
    assert spec["evaluation"]["background"]["enabled"] is True
    assert spec["evaluation"]["admission"]["enabled"] is True
    assert spec["matchConstraints"]["resourceRules"] == [
        {
            "apiGroups": ["external-secrets.io"],
            "apiVersions": ["v1"],
            "operations": ["CREATE", "UPDATE"],
            "resources": ["clustersecretstores"],
        }
    ]


class TestRequireSecretStoreConditions:
    """An unfenced ClusterSecretStore is readable from every namespace.

    Evaluated as a JSON payload: offline `kyverno apply` cannot map the ESO CRD's kind (see
    `apply_policy`). Only the CEL `validations` are exercised, not the admission match.
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
        result = apply_policy(secret_store_conditions_policy, manifest(manifest_name), json_payload=True)
        assert result.failed == 1, f"{manifest_name} must be denied\n{result.stdout}"

    def test_declared_conditions_allowed(self, secret_store_conditions_policy: Path):
        result = apply_policy(
            secret_store_conditions_policy, manifest("cluster_secret_store_with_conditions.yaml"), json_payload=True
        )
        assert result.failed == 0, f"store naming its namespaces must pass\n{result.stdout}"


if __name__ == "__main__":
    pytest_bazel.main()
