"""The Policy Controller's RBAC matches what it writes: ServiceAccounts only in its accounts namespace, and the
status of every policy kind it reports on."""

from collections.abc import Iterator
from typing import Any

import pytest
import pytest_bazel
from more_itertools import one

from agentplane.policy_controller.settings import Settings
from cluster.cdk8s.agentplane import policy_controller
from cluster.cdk8s.agentplane.namespaces import STAGING_NAMESPACE
from util.settings_contract import env_name

_GROUP = "agentplane.allegedly.works"


def _role(documents: list[dict[str, Any]], kind: str, name: str, namespace: str | None) -> dict[str, Any]:
    return one(
        doc
        for doc in documents
        if doc["kind"] == kind and doc["metadata"]["name"] == name and doc["metadata"].get("namespace") == namespace
    )


def _bound_rules(documents: list[dict[str, Any]]) -> Iterator[tuple[str | None, dict[str, Any]]]:
    """Each rule bound to the controller's ServiceAccount, with the namespace it applies in; None is cluster-wide."""
    subject = ("ServiceAccount", policy_controller.NAME, STAGING_NAMESPACE)
    for binding in documents:
        if binding["kind"] not in {"RoleBinding", "ClusterRoleBinding"} or subject not in {
            (s["kind"], s["name"], s.get("namespace")) for s in binding.get("subjects", [])
        }:
            continue
        namespace = binding["metadata"].get("namespace")
        ref = binding["roleRef"]
        for rule in _role(documents, ref["kind"], ref["name"], namespace if ref["kind"] == "Role" else None)["rules"]:
            yield namespace, rule


def _allowed(documents: list[dict[str, Any]], group: str, resource: str, verbs: set[str]) -> set[str | None]:
    """The namespaces where the controller may use any of verbs on group's resource."""
    return {
        namespace
        for namespace, rule in _bound_rules(documents)
        if set(rule.get("apiGroups", [])) & {group, "*"}
        and set(rule["resources"]) & {resource, "*"}
        and set(rule["verbs"]) & (verbs | {"*"})
    }


def _settings(documents: list[dict[str, Any]]) -> dict[str, str]:
    deployment = one(
        doc for doc in documents if doc["kind"] == "Deployment" and doc["metadata"]["name"] == policy_controller.NAME
    )
    return {
        entry["name"]: entry.get("value") for entry in one(deployment["spec"]["template"]["spec"]["containers"])["env"]
    }


def test_service_account_writes_are_scoped_to_the_accounts_namespace(staging_manifests: list[dict[str, Any]]) -> None:
    accounts_namespace = _settings(staging_manifests)[env_name(Settings, "accounts_namespace")]
    assert accounts_namespace != STAGING_NAMESPACE
    assert _allowed(staging_manifests, "", "serviceaccounts", {"create", "delete"}) == {accounts_namespace}


@pytest.mark.parametrize(
    "status", ["policybindings/status", "serviceaccountgrants/status", "managedserviceaccounts/status"]
)
def test_controller_may_apply_the_status_it_reports(staging_manifests: list[dict[str, Any]], status: str) -> None:
    policy_namespace = _settings(staging_manifests)[env_name(Settings, "policy_namespace")]
    # Server-side apply of a status subresource is a patch.
    assert policy_namespace in _allowed(staging_manifests, _GROUP, status, {"patch"})


if __name__ == "__main__":
    pytest_bazel.main()
