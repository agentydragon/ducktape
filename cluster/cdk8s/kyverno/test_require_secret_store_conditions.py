"""Tests for the require-secret-store-conditions Kyverno policy."""

from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel
import yaml
from cdk8s import App

from cluster.cdk8s.kyverno import policies
from cluster.cdk8s.kyverno.apply import apply_policy


@pytest.fixture(scope="module")
def store_policy(scratch_app_factory: Callable[[], App]) -> dict[str, Any]:
    document: dict[str, Any]
    [document] = policies.require_secret_store_conditions_chart(scratch_app_factory()).to_json()
    return document


@pytest.fixture(scope="module")
def json_policy(store_policy: dict[str, Any], tmp_path_factory: pytest.TempPathFactory) -> Path:
    # Offline Kyverno cannot resolve the ESO CRD. Evaluate the same validations against JSON.
    policy = deepcopy(store_policy)
    policy["spec"]["evaluation"] = {"mode": "JSON"}
    del policy["spec"]["matchConstraints"]
    path = tmp_path_factory.mktemp("kyverno") / "policy.yaml"
    path.write_text(yaml.safe_dump(policy, sort_keys=False))
    return path


def test_cluster_store_policy_enables_discoverable_v1_background_scan(store_policy: dict[str, Any]) -> None:
    spec = store_policy["spec"]
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


@pytest.mark.parametrize(
    ("spec", "allowed"),
    [
        pytest.param({}, False, id="absent"),
        pytest.param({"conditions": []}, False, id="empty-conditions"),
        pytest.param({"conditions": [{"namespaces": []}]}, False, id="empty-namespaces"),
        pytest.param({"conditions": [{"namespaceSelector": {}}]}, False, id="selector-matches-all"),
        pytest.param({"conditions": [{"namespaces": ["consumer"]}]}, True, id="named-namespace"),
    ],
)
def test_store_requires_a_named_namespace(json_policy: Path, tmp_path: Path, spec: dict, allowed: bool) -> None:
    resource = tmp_path / "store.json"
    resource.write_text(
        json.dumps(
            {
                "apiVersion": "external-secrets.io/v1",
                "kind": "ClusterSecretStore",
                "metadata": {"name": "store"},
                "spec": {
                    "provider": {
                        "kubernetes": {
                            "auth": {
                                "serviceAccount": {"name": "external-secrets", "namespace": "external-secrets-system"}
                            },
                            "remoteNamespace": "flux-system",
                        }
                    },
                    **spec,
                },
            }
        )
    )
    result = apply_policy(json_policy, resource, json_payload=True)
    assert (result.passed, result.failed, result.errored) == ((1, 0, 0) if allowed else (0, 1, 0)), result.stdout


if __name__ == "__main__":
    pytest_bazel.main()
