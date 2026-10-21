"""Exercise PolicySet unions and opaque JSON through Kubernetes admission and CEL."""

from typing import Any, cast

import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client.exceptions import ApiException

from agentplane.testing.kubernetes_apiserver import kubernetes_apiserver

GROUP = "agentplane.allegedly.works"
VERSION = "v1alpha1"
NAMESPACE = "policyset-admission"


async def test_policyset_admission() -> None:
    async with kubernetes_apiserver() as server:
        core = k8s_client.CoreV1Api(server.api)
        await core.create_namespace(k8s_client.V1Namespace(metadata=k8s_client.V1ObjectMeta(name=NAMESPACE)))
        api = k8s_client.CustomObjectsApi(server.api)

        async def create(rule: dict[str, Any]) -> dict[str, Any]:
            body = {
                "apiVersion": f"{GROUP}/{VERSION}",
                "kind": "PolicySet",
                "metadata": {"generateName": "policy-"},
                "spec": {"rules": [rule]},
            }
            return cast(
                dict[str, Any], await api.create_namespaced_custom_object(GROUP, VERSION, NAMESPACE, "policysets", body)
            )

        def action(kind: str, **fields: Any) -> dict[str, Any]:
            return {"type": "action_auto_approve", "policy": {"type": kind, "actions": {"g": ["a"]}, **fields}}

        def inbox(targets: dict[str, Any], **fields: Any) -> dict[str, Any]:
            return {"type": "session_inbox_access", "operations": ["send_message"], "targets": targets, **fields}

        pinned = {"name": "dev-1", "uid": "3b1c0f1e-7a52-4f0c-9d52-2d7a54f0b2a1"}

        rules: tuple[dict[str, Any], ...] = (
            {"type": "kubernetes_role_binding", "role": {"kind": "Role", "name": "reader"}, "namespaces": ["team-a"]},
            {"type": "kubernetes_cluster_role_binding", "clusterRole": "reader"},
            {
                "type": "egress",
                "hosts": ["api.example.com"],
                "credentialRef": {"name": "token"},
                "clusterInternal": False,
            },
            action("exact_actions"),
            action("argument_schema", schema={"type": "object", "x-extension": {"keep": True}}),
            action("argument_schema", schema={}),
            action("github_repository", owner="owner", repository="repo"),
            action("github_public_repository"),
            action("home_assistant_entity_control", entities={"light.office": ["turn_on"]}),
            inbox({"sandboxes": [{"name": "coordinator"}, pinned]}),
            inbox({"allSandboxes": True}),
            inbox({"inboxTags": ["test-group"], "sandboxes": [{"name": "coordinator"}]}),
        )
        for rule in rules:
            created = await create(rule)
            policy = rule.get("policy", {})
            if "schema" in policy:
                assert created["spec"]["rules"][0]["policy"]["schema"] == policy["schema"]

        invalid_rules = (
            {"type": "action_auto_approve"},
            {"type": "kubernetes_role_binding", "role": {"kind": "Role", "name": "reader"}},
            {
                "type": "kubernetes_role_binding",
                "role": {"kind": "Role", "name": "reader"},
                "namespaces": ["team-a"],
                "clusterInternal": False,
            },
            {"type": "kubernetes_cluster_role_binding", "clusterRole": "reader", "namespaces": ["team-a"]},
            {"type": "egress", "hosts": ["*example.com"]},
            {"type": "egress", "hosts": ["*"], "credentialRef": {"name": "token"}},
            {"type": "egress", "hosts": ["*"], "clusterInternal": True},
            action("exact_actions", schema={}),
            action("argument_schema"),
            action("github_repository", owner="owner"),
            action("home_assistant_entity_control"),
            inbox({}),
            inbox({"allSandboxes": False}),
            inbox({"inboxTags": []}),
            inbox({"inboxTags": [""]}),
            inbox({"sandboxes": [{"name": "dev-1", "uid": "dev-1"}]}),
            inbox({"allSandboxes": True}, hosts=["api.example.com"]),
            {"type": "session_inbox_access", "operations": ["read_transcript"], "targets": {"allSandboxes": True}},
            {"type": "session_inbox_access", "targets": {"allSandboxes": True}},
            {"type": "egress", "hosts": ["api.example.com"], "operations": ["send_message"]},
            {**action("exact_actions"), "targets": {"allSandboxes": True}},
        )
        for rule in invalid_rules:
            with pytest.raises(ApiException) as rejected:
                await create(rule)
            assert rejected.value.status == 422, rejected.value.body


if __name__ == "__main__":
    pytest_bazel.main()
