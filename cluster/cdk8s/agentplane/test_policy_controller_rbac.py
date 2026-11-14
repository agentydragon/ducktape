"""The Policy Controller's RBAC matches what it writes: ServiceAccounts only in its accounts namespace,
managed accounts' EgressCredentials and PolicySets only in its policy namespace, and the status of every
policy kind it reports on."""

from collections.abc import Iterator
from typing import Any, cast

import pytest
import pytest_bazel
import yaml
from more_itertools import one

from agentplane.net.peertls.config import POLICY_CONTROLLER_URI
from cluster.cdk8s.agentplane import policy_controller
from cluster.cdk8s.agentplane.namespaces import STAGING_NAMESPACE

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


def _settings(documents: list[dict[str, Any]]) -> dict[str, Any]:
    config = one(
        doc
        for doc in documents
        if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "policy-controller-settings"
    )
    return cast(dict[str, Any], yaml.safe_load(one(config["data"].values())))


def test_policy_controller_has_a_workload_server_certificate(staging_manifests: list[dict[str, Any]]) -> None:
    certificate = _role(staging_manifests, "Certificate", "policy-controller-workload", STAGING_NAMESPACE)
    assert certificate["spec"]["uris"] == [POLICY_CONTROLLER_URI]
    deployment = _role(staging_manifests, "Deployment", policy_controller.NAME, STAGING_NAMESPACE)
    pod = deployment["spec"]["template"]["spec"]
    assert any(
        volume.get("secret", {}).get("secretName") == f"{policy_controller.NAME}-workload-tls"
        for volume in pod["volumes"]
    )


def test_policy_controller_serves_tls_on_a_separate_port(staging_manifests: list[dict[str, Any]]) -> None:
    settings = _settings(staging_manifests)
    assert settings["grpc_tls"] == {
        "listen": f":{policy_controller.tls_service(STAGING_NAMESPACE).pod_port}",
        "cert_file": "/run/agentplane/workload-tls/tls.crt",
        "key_file": "/run/agentplane/workload-tls/tls.key",
    }
    deployment = _role(staging_manifests, "Deployment", policy_controller.NAME, STAGING_NAMESPACE)
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert policy_controller.tls_service(STAGING_NAMESPACE).pod_port in {
        port["containerPort"] for port in container["ports"]
    }
    service = _role(staging_manifests, "Service", policy_controller.NAME, STAGING_NAMESPACE)
    assert policy_controller.tls_service(STAGING_NAMESPACE).port.number in {
        port["port"] for port in service["spec"]["ports"]
    }


def test_service_account_writes_are_scoped_to_the_accounts_namespace(staging_manifests: list[dict[str, Any]]) -> None:
    accounts_namespace = _settings(staging_manifests)["accounts_namespace"]
    assert accounts_namespace != STAGING_NAMESPACE
    assert _allowed(staging_manifests, "", "serviceaccounts", {"create", "delete"}) == {accounts_namespace}


@pytest.mark.parametrize("resource", ["egresscredentials", "policysets"])
def test_managed_account_use_writes_are_scoped_to_the_policy_namespace(
    staging_manifests: list[dict[str, Any]], resource: str
) -> None:
    policy_namespace = _settings(staging_manifests)["policy_namespace"]
    # Server-side apply creates or patches.
    assert _allowed(staging_manifests, _GROUP, resource, {"create", "patch"}) == {policy_namespace}
    assert not _allowed(staging_manifests, _GROUP, resource, {"delete", "deletecollection"})


@pytest.mark.parametrize(
    "status", ["policybindings/status", "serviceaccountgrants/status", "managedserviceaccounts/status"]
)
def test_controller_may_apply_the_status_it_reports(staging_manifests: list[dict[str, Any]], status: str) -> None:
    policy_namespace = _settings(staging_manifests)["policy_namespace"]
    # Server-side apply of a status subresource is a patch.
    assert policy_namespace in _allowed(staging_manifests, _GROUP, status, {"patch"})


def test_only_the_policy_controller_writes_workload_credential_status(staging_manifests: list[dict[str, Any]]) -> None:
    """Requesters write a record's spec; the status verifiers trust is the issuer's alone. Roles staging
    does not render, the API server's own, grant nothing on Agentplane kinds."""
    roles = {
        (doc["kind"], doc["metadata"]["name"], doc["metadata"].get("namespace")): doc
        for doc in staging_manifests
        if doc["kind"] in {"Role", "ClusterRole"}
    }
    writers = set()
    for binding in staging_manifests:
        if binding["kind"] not in {"RoleBinding", "ClusterRoleBinding"}:
            continue
        ref = binding["roleRef"]
        role = roles.get(
            (ref["kind"], ref["name"], binding["metadata"].get("namespace") if ref["kind"] == "Role" else None)
        )
        if role is None or not any(
            set(rule.get("apiGroups", [])) & {_GROUP, "*"}
            and set(rule["resources"]) & {"workloadcredentials/status", "*"}
            and set(rule["verbs"]) & {"update", "patch", "*"}
            for rule in role.get("rules", [])
        ):
            continue
        writers |= {(s["kind"], s["name"], s.get("namespace")) for s in binding.get("subjects", [])}
    assert writers == {("ServiceAccount", policy_controller.NAME, STAGING_NAMESPACE)}


if __name__ == "__main__":
    pytest_bazel.main()
