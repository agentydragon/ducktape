"""The Policy Controller may create and delete ServiceAccounts only in the namespace it creates managed accounts in."""

from typing import Any

import pytest_bazel
from more_itertools import one

from agentplane.policy_controller.settings import Settings
from cluster.cdk8s.agentplane import policy_controller
from cluster.cdk8s.agentplane.namespaces import STAGING_NAMESPACE
from util.settings_contract import env_name

_WRITES = {"create", "delete", "*"}


def _role(documents: list[dict[str, Any]], kind: str, name: str, namespace: str | None) -> dict[str, Any]:
    return one(
        doc
        for doc in documents
        if doc["kind"] == kind and doc["metadata"]["name"] == name and doc["metadata"].get("namespace") == namespace
    )


def _service_account_writes(documents: list[dict[str, Any]]) -> set[str | None]:
    """The namespaces where the controller's bindings let it create or delete ServiceAccounts; None is cluster-wide."""
    subject = ("ServiceAccount", policy_controller.NAME, STAGING_NAMESPACE)
    out: set[str | None] = set()
    for binding in documents:
        if binding["kind"] not in {"RoleBinding", "ClusterRoleBinding"} or subject not in {
            (s["kind"], s["name"], s.get("namespace")) for s in binding.get("subjects", [])
        }:
            continue
        namespace = binding["metadata"].get("namespace")
        ref = binding["roleRef"]
        role = _role(documents, ref["kind"], ref["name"], namespace if ref["kind"] == "Role" else None)
        for rule in role["rules"]:
            if (
                set(rule.get("apiGroups", [])) & {"", "*"}
                and set(rule["resources"]) & {"serviceaccounts", "*"}
                and set(rule["verbs"]) & _WRITES
            ):
                out.add(namespace)
    return out


def test_service_account_writes_are_scoped_to_the_accounts_namespace(staging_manifests: list[dict[str, Any]]) -> None:
    deployment = one(
        doc
        for doc in staging_manifests
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == policy_controller.NAME
    )
    env = {
        entry["name"]: entry.get("value") for entry in one(deployment["spec"]["template"]["spec"]["containers"])["env"]
    }
    accounts_namespace = env[env_name(Settings, "accounts_namespace")]
    assert accounts_namespace != STAGING_NAMESPACE
    assert _service_account_writes(staging_manifests) == {accounts_namespace}


if __name__ == "__main__":
    pytest_bazel.main()
