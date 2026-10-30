"""The Action Service creates Sandboxes and launcher grants only in the orchestration_v1 executor's shape."""

from __future__ import annotations

import copy
from typing import Any, cast

import cdk8s
import pytest_bazel
from cdk8s import Chart
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio.config import new_client_from_config
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from agentplane.crd_group import GROUP, VERSION
from agentplane.testing.kubernetes_apiserver import kubernetes_apiserver
from cluster.cdk8s.agentplane import actions

NAMESPACE = "test-launcher-admission"

Body = dict[str, Any]
_PLURALS = {"Sandbox": "sandboxes", "PolicySet": "policysets", "PolicyBinding": "policybindings"}
Path = tuple[str | int, ...]

_INBOX: Path = ("spec", "rules", 0)
_APPROVAL: Path = ("spec", "rules", 1, "policy")
_SANDBOX_SCHEMA: Path = (*_APPROVAL, "schema", "properties", "sandbox")


def _object(kind: str, name: str, spec: Body, owners: list[Body] | None = None) -> Body:
    metadata: Body = {"name": name}
    if owners is not None:
        metadata["ownerReferences"] = owners
    return {"apiVersion": f"{GROUP}/{VERSION}", "kind": kind, "metadata": metadata, "spec": spec}


def _sandbox(name: str, *policy_sets: str) -> Body:
    return _object("Sandbox", name, {"template": "test-template", "policySets": list(policy_sets)})


def _owner(sandbox: Body) -> Body:
    metadata = sandbox["metadata"]
    return {"apiVersion": f"{GROUP}/{VERSION}", "kind": "Sandbox", "name": metadata["name"], "uid": metadata["uid"]}


def _launcher_grant(sandbox: Body) -> tuple[Body, Body]:
    """The PolicySet and PolicyBinding the executor writes for launcher grants of both kinds on `sandbox`."""
    target = {"name": sandbox["metadata"]["name"], "uid": sandbox["metadata"]["uid"]}
    name = f"{target['name']}.launcher"
    rules = [
        {"type": "session_inbox_access", "operations": ["send_message"], "targets": {"sandboxes": [target]}},
        {
            "type": "action_auto_approve",
            "policy": {
                "type": "argument_schema",
                "actions": {
                    actions.ORCHESTRATION_GROUP: [
                        "open_session",
                        "submit_input",
                        "get_session",
                        "read_turns",
                        "wait_for_turn",
                    ]
                },
                "schema": {"type": "object", "required": ["sandbox"], "properties": {"sandbox": {"const": target}}},
            },
        },
    ]
    binding = {"subject": {"namespace": "test-caller-namespace", "name": "test-caller"}, "policySets": [name]}
    owners = [_owner(sandbox)]
    return _object("PolicySet", name, {"rules": rules}, owners), _object("PolicyBinding", name, binding, owners)


def _with(body: Body, path: Path, value: object) -> Body:
    changed = copy.deepcopy(body)
    *parents, key = path
    node: Any = changed
    for parent in parents:
        node = node[parent]
    node[key] = value
    return changed


def _without(body: Body, path: Path) -> Body:
    changed = copy.deepcopy(body)
    *parents, key = path
    node: Any = changed
    for parent in parents:
        node = node[parent]
    del node[key]
    return changed


async def test_the_action_service_writes_only_launcher_shaped_grants(scratch_chart: Chart) -> None:
    actions.add_launcher_admission(scratch_chart, NAMESPACE)
    policy, binding = cdk8s.Testing.synth(scratch_chart)
    async with kubernetes_apiserver() as server:
        await k8s_client.CoreV1Api(server.api).create_namespace(
            k8s_client.V1Namespace(metadata=k8s_client.V1ObjectMeta(name=NAMESPACE))
        )
        admission = k8s_client.AdmissionregistrationV1Api(server.api)
        await admission.create_validating_admission_policy(policy)
        await admission.create_validating_admission_policy_binding(binding)
        async with await new_client_from_config(config_file=str(server.kubeconfig)) as impersonating:
            impersonating.set_default_header("Impersonate-User", f"system:serviceaccount:{NAMESPACE}:actions")
            # Authorized as an administrator, so that admission alone decides.
            impersonating.set_default_header("Impersonate-Group", "system:masters")
            operator = k8s_client.CustomObjectsApi(server.api)
            action_service = k8s_client.CustomObjectsApi(impersonating)

            async def admitted(api: k8s_client.CustomObjectsApi, body: Body) -> bool:
                try:
                    await api.create_namespaced_custom_object(
                        GROUP, VERSION, NAMESPACE, _PLURALS[body["kind"]], body, dry_run="All"
                    )
                except ApiException as error:
                    if error.status != 422:
                        raise
                    return False
                return True

            launcher_bound = _sandbox("test-launcher-bound", "test-other.launcher")
            # The API server loads a new policy in the background, and shows no sign of having loaded it.
            async for attempt in AsyncRetrying(stop=stop_after_delay(30), wait=wait_fixed(0.1), reraise=True):
                with attempt:
                    assert not await admitted(action_service, launcher_bound)

            launched = cast(
                Body,
                await action_service.create_namespaced_custom_object(
                    GROUP, VERSION, NAMESPACE, "sandboxes", _sandbox("test-launched", "test-default")
                ),
            )
            policy_set, policy_binding = _launcher_grant(launched)
            assert await admitted(action_service, policy_set)
            assert await admitted(action_service, policy_binding)

            other = {"name": "test-other", "uid": "3b1c0f1e-7a52-4f0c-9d52-2d7a54f0b2a1"}
            owners = ("metadata", "ownerReferences")
            denied = {
                "a Sandbox bound to a launcher grant": launcher_bound,
                "an unowned grant": _without(policy_set, owners),
                "a grant owned by two Sandboxes": _with(
                    policy_set, owners, [_owner(launched), {**_owner(launched), **other}]
                ),
                "a grant owned by another kind": _with(policy_set, (*owners, 0, "kind"), "SandboxTemplate"),
                "a grant named for another Sandbox": _with(policy_set, ("metadata", "name"), "test-other.launcher"),
                "a binding of another PolicySet too": _with(
                    policy_binding, ("spec", "policySets"), [policy_binding["metadata"]["name"], "test-default"]
                ),
                "an inbox rule on another Sandbox": _with(policy_set, (*_INBOX, "targets", "sandboxes"), [other]),
                "an inbox rule on any incarnation": _without(policy_set, (*_INBOX, "targets", "sandboxes", 0, "uid")),
                "an inbox rule on every Sandbox": _with(policy_set, (*_INBOX, "targets"), {"allSandboxes": True}),
                "an inbox rule on an inbox tag": _with(policy_set, (*_INBOX, "targets", "inboxTags"), ["test-tag"]),
                "an approval of another group's Actions": _with(
                    policy_set, (*_APPROVAL, "actions", "github"), ["create_branch"]
                ),
                "an approval of a group Action that does not take a Sandbox": _with(
                    policy_set, (*_APPROVAL, "actions", actions.ORCHESTRATION_GROUP), ["open_session", "launch_sandbox"]
                ),
                "an approval of another Sandbox": _with(policy_set, (*_SANDBOX_SCHEMA, "const"), other),
                "an approval of any incarnation": _without(policy_set, (*_SANDBOX_SCHEMA, "const", "uid")),
                "an approval of any Sandbox": _with(policy_set, _SANDBOX_SCHEMA, {"type": "object"}),
                "an approval not requiring the Sandbox": _with(policy_set, (*_APPROVAL, "schema", "required"), []),
                "an approval of any arguments": _with(
                    _without(policy_set, (*_APPROVAL, "schema")), (*_APPROVAL, "type"), "exact_actions"
                ),
                "a rule of another type": _with(
                    policy_set,
                    ("spec", "rules"),
                    [
                        *policy_set["spec"]["rules"],
                        {"type": "kubernetes_cluster_role_binding", "clusterRole": "test-reader"},
                    ],
                ),
            }
            for case, body in denied.items():
                # Schema-valid, and admitted from anyone but the Action Service: its policy alone denies it.
                assert await admitted(operator, body), case
                assert not await admitted(action_service, body), case


if __name__ == "__main__":
    pytest_bazel.main()
