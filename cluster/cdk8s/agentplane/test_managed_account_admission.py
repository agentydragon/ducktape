"""The Policy Controller writes a ManagedServiceAccount's use only in the shape cdk8s renders for a static
account, acting as the owning account, and never takes over an object that account does not control."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any, cast

import pytest_bazel
from cdk8s import Chart
from kubernetes_asyncio import client as k8s_client
from more_itertools import one

from agentplane.crd_group import GROUP, VERSION
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.kubernetes_apiserver import (
    KubernetesApiServer,
    dry_run_admitted,
    kubernetes_apiserver,
    wait_until_refused,
)
from cluster.cdk8s.agentplane.kubernetes_account_ref import KubernetesAccountRef
from cluster.cdk8s.agentplane.kubernetes_accounts import KubernetesAccount
from cluster.cdk8s.agentplane.managed_account_admission import add_managed_account_admission

Body = dict[str, Any]

NAMESPACE = "test-policy"
ACCOUNTS_NAMESPACE = "test-policy-accounts"
CONTROLLER = ServiceAccountRef(namespace=NAMESPACE, name="test-policy-controller")
CONTROLLER_USER = f"system:serviceaccount:{CONTROLLER.namespace}:{CONTROLLER.name}"
OTHER_USER = f"system:serviceaccount:{NAMESPACE}:test-other-account"
ACCOUNT = KubernetesAccountRef(namespace=ACCOUNTS_NAMESPACE, name="test-managed-account")
OWNER: Body = {
    "apiVersion": f"{GROUP}/{VERSION}",
    "kind": "ManagedServiceAccount",
    "name": ACCOUNT.name,
    "uid": "3b1c0f1e-7a52-4f0c-9d52-2d7a54f0b2a1",
    "controller": True,
}
_PLURALS = {"EgressCredential": "egresscredentials", "PolicySet": "policysets"}


def _owned(body: Body, owners: list[Body]) -> Body:
    changed = copy.deepcopy(body)
    changed["metadata"]["ownerReferences"] = owners
    return changed


def _changed(body: Body, edit: Callable[[Body], None]) -> Body:
    changed = copy.deepcopy(body)
    edit(changed)
    return changed


def _renamed(body: Body, name: str) -> Body:
    return _changed(body, lambda b: b["metadata"].update(name=name))


async def _admitted(server: KubernetesApiServer, user: str, body: Body) -> bool:
    async with server.impersonating(user) as impersonating:
        return await dry_run_admitted(
            k8s_client.CustomObjectsApi(impersonating).create_namespaced_custom_object(
                GROUP, VERSION, NAMESPACE, _PLURALS[body["kind"]], body, dry_run="All"
            )
        )


async def _replaced(server: KubernetesApiServer, user: str, body: Body) -> bool:
    """Whether `user` may replace the stored object at `body`'s name with `body`."""
    plural = _PLURALS[body["kind"]]
    current = cast(
        Body,
        await k8s_client.CustomObjectsApi(server.api).get_namespaced_custom_object(
            GROUP, VERSION, NAMESPACE, plural, body["metadata"]["name"]
        ),
    )
    replacement = copy.deepcopy(body)
    replacement["metadata"]["resourceVersion"] = current["metadata"]["resourceVersion"]
    async with server.impersonating(user) as impersonating:
        return await dry_run_admitted(
            k8s_client.CustomObjectsApi(impersonating).replace_namespaced_custom_object(
                GROUP, VERSION, NAMESPACE, plural, body["metadata"]["name"], replacement, dry_run="All"
            )
        )


async def test_controller_writes_only_its_accounts_use(scratch_chart: Chart) -> None:
    # The static account's use is the shape a managed account's must have.
    KubernetesAccount(
        scratch_chart,
        "account",
        account=ACCOUNT,
        policy_namespace=NAMESPACE,
        description="A test account.",
        role_sets=[],
    )
    add_managed_account_admission(
        scratch_chart, namespace=NAMESPACE, controller=CONTROLLER, accounts_namespace=ACCOUNTS_NAMESPACE
    )
    documents = scratch_chart.to_json()
    static_credential = one(d for d in documents if d["kind"] == "EgressCredential")
    static_set = one(d for d in documents if d["kind"] == "PolicySet")
    credential = _owned(static_credential, [OWNER])
    use_set = _owned(static_set, [OWNER])

    async with kubernetes_apiserver() as server:
        await server.install_admission_policy(
            one(d for d in documents if d["kind"] == "ValidatingAdmissionPolicy"),
            one(d for d in documents if d["kind"] == "ValidatingAdmissionPolicyBinding"),
            namespaces=[NAMESPACE],
        )
        await wait_until_refused(lambda: _admitted(server, CONTROLLER_USER, static_credential))

        assert await _admitted(server, CONTROLLER_USER, credential)
        assert await _admitted(server, CONTROLLER_USER, use_set)

        def without_controller(owners: list[Body]) -> list[Body]:
            return [{k: v for k, v in owner.items() if k != "controller"} for owner in owners]

        other_owner = {**OWNER, "name": "test-other-managed-account"}
        refused: dict[str, Body] = {}
        for kind, body in (("credential", credential), ("use set", use_set)):
            refused |= {
                f"an unowned {kind}": _owned(body, []),
                f"a {kind} owned without control": _owned(body, without_controller([OWNER])),
                f"a {kind} owned by another kind": _owned(body, [{**OWNER, "kind": "ServiceAccountGrant"}]),
                f"a {kind} with a second owner": _owned(body, [OWNER, {**other_owner, "controller": False}]),
                f"a {kind} named for another account": _owned(body, [other_owner]),
                f"a {kind} at another name": _renamed(body, "test-other-name"),
            }
        refused |= {
            "a credential with a secret source": _changed(
                credential, lambda b: b["spec"].update(source={"secretRef": {"name": "test-secret", "key": "token"}})
            ),
            "a credential acting as another account": _changed(
                credential, lambda b: b["spec"]["source"]["kubernetesServiceAccount"].update(name="test-other-account")
            ),
            "a credential acting in another namespace": _changed(
                credential, lambda b: b["spec"]["source"]["kubernetesServiceAccount"].update(namespace=NAMESPACE)
            ),
            "a use set with a second rule": _changed(
                use_set, lambda b: b["spec"]["rules"].append({"type": "egress", "hosts": ["test.example"]})
            ),
            "a use set to another host": _changed(
                use_set, lambda b: b["spec"]["rules"][0].update(hosts=["test.example"])
            ),
            "a use set with a method list": _changed(use_set, lambda b: b["spec"]["rules"][0].update(methods=["GET"])),
            "a use set presenting another credential": _changed(
                use_set, lambda b: b["spec"]["rules"][0].update(credentialRef={"name": "test-other-credential"})
            ),
            "a use set without clusterInternal": _changed(
                use_set, lambda b: b["spec"]["rules"][0].update(clusterInternal=False)
            ),
        }
        for case, body in refused.items():
            assert not await _admitted(server, CONTROLLER_USER, body), case
            # Schema-valid, and admitted from anyone else: this policy alone refuses it.
            assert await _admitted(server, OTHER_USER, body), case

        # A static account's use at the same name stays the applier's: the controller may not take
        # it over, even into the right shape.
        objects = k8s_client.CustomObjectsApi(server.api)
        for body in (static_credential, static_set):
            await objects.create_namespaced_custom_object(GROUP, VERSION, NAMESPACE, _PLURALS[body["kind"]], body)
        assert not await _replaced(server, CONTROLLER_USER, credential)
        assert not await _replaced(server, CONTROLLER_USER, use_set)

        # Its account's own it may restore, but not hand to the next account of the same name.
        successor = {**OWNER, "uid": "5d2e7c4a-1b3f-4e6a-8c9d-0f1e2d3c4b5a"}
        for body in (credential, use_set):
            plural = _PLURALS[body["kind"]]
            await objects.delete_namespaced_custom_object(GROUP, VERSION, NAMESPACE, plural, body["metadata"]["name"])
            await objects.create_namespaced_custom_object(GROUP, VERSION, NAMESPACE, plural, body)
            assert await _replaced(server, CONTROLLER_USER, body), body["kind"]
            assert not await _replaced(server, CONTROLLER_USER, _owned(body, [successor])), body["kind"]


if __name__ == "__main__":
    pytest_bazel.main()
