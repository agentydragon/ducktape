"""The Policy Controller's RBAC matches what it writes: ServiceAccounts only in its accounts namespace,
managed accounts' EgressCredentials and PolicySets only in its policy namespace, and the status of every
policy kind it reports on."""

from typing import Any

import pytest
import pytest_bazel

from agentplane.crd_group import GROUP
from agentplane.policy_controller.settings import Settings
from cluster.cdk8s.agent_rbac_test_support import Permission, Rbac
from cluster.cdk8s.agentplane import policy_controller
from cluster.cdk8s.agentplane.namespaces import STAGING_NAMESPACE
from cluster.cdk8s.testing.manifests import config_map_yaml


@pytest.fixture(scope="module")
def controller_permissions(staging_manifests: list[dict[str, Any]]) -> set[Permission]:
    return Rbac(staging_manifests).identity("ServiceAccount", policy_controller.NAME, STAGING_NAMESPACE)


def _allowed(permissions: set[Permission], group: str, resource: str, *verbs: str) -> set[str | None]:
    """The namespaces where the grants allow any of verbs on group's resource."""
    return {
        grant.namespace
        for grant in permissions
        if any(grant.covers(Permission(grant.namespace, group, resource, verb, grant.name)) for verb in verbs)
    }


@pytest.fixture(scope="module")
def settings(staging_manifests: list[dict[str, Any]]) -> Settings:
    return Settings.model_validate(
        config_map_yaml(staging_manifests, "policy-controller-settings", "config.yaml"), strict=False
    )


def test_service_account_writes_are_scoped_to_the_accounts_namespace(
    settings: Settings, controller_permissions: set[Permission]
) -> None:
    accounts_namespace = settings.accounts_namespace
    assert accounts_namespace != STAGING_NAMESPACE
    assert _allowed(controller_permissions, "", "serviceaccounts", "create", "delete") == {accounts_namespace}


@pytest.mark.parametrize("resource", ["egresscredentials", "policysets"])
def test_managed_account_use_writes_are_scoped_to_the_policy_namespace(
    settings: Settings, controller_permissions: set[Permission], resource: str
) -> None:
    policy_namespace = settings.policy_namespace
    # Server-side apply creates or patches.
    assert _allowed(controller_permissions, GROUP, resource, "create", "patch") == {policy_namespace}
    assert not _allowed(controller_permissions, GROUP, resource, "delete", "deletecollection")


@pytest.mark.parametrize(
    "status", ["policybindings/status", "serviceaccountgrants/status", "managedserviceaccounts/status"]
)
def test_controller_may_apply_the_status_it_reports(
    settings: Settings, controller_permissions: set[Permission], status: str
) -> None:
    policy_namespace = settings.policy_namespace
    # Server-side apply of a status subresource is a patch.
    assert policy_namespace in _allowed(controller_permissions, GROUP, status, "patch")


def test_only_the_policy_controller_writes_workload_credential_status(staging_manifests: list[dict[str, Any]]) -> None:
    """Requesters write a record's spec. Verifiers trust the issuer alone to write its status."""
    rbac = Rbac(staging_manifests)
    writers = {
        (subject["kind"], subject["name"], subject.get("namespace"))
        for binding in rbac.bindings
        if _allowed(rbac.binding_rules(binding), GROUP, "workloadcredentials/status", "update", "patch")
        for subject in binding.get("subjects", [])
    }
    assert writers == {("ServiceAccount", policy_controller.NAME, STAGING_NAMESPACE)}


if __name__ == "__main__":
    pytest_bazel.main()
