"""Exercise the generated PolicySet schema through Kubernetes admission and CEL."""

from typing import Any, cast

import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client.exceptions import ApiException

from agentplane.testing.kubernetes_apiserver import kubernetes_apiserver

GROUP = "agentplane.allegedly.works"
VERSION = "v1alpha1"
NAMESPACE = "policyset-admission"


async def test_policyset_rule_and_action_policy_validation() -> None:
    async with kubernetes_apiserver() as server:
        core = k8s_client.CoreV1Api(server.api)
        await core.create_namespace(k8s_client.V1Namespace(metadata=k8s_client.V1ObjectMeta(name=NAMESPACE)))
        api = k8s_client.CustomObjectsApi(server.api)
        counter = 0

        async def create(rules: list[dict[str, Any]], valid: bool) -> dict[str, Any] | None:
            nonlocal counter
            counter += 1
            body = {
                "apiVersion": f"{GROUP}/{VERSION}",
                "kind": "PolicySet",
                "metadata": {"name": f"policy-{counter}"},
                "spec": {"rules": rules},
                "status": {"conditions": [{"type": "unserved", "status": "True"}]},
            }
            if not valid:
                with pytest.raises(ApiException) as rejected:
                    await api.create_namespaced_custom_object(GROUP, VERSION, NAMESPACE, "policysets", body)
                assert rejected.value.status == 422, f"{body!r}: {rejected.value.body!r}"
                return None
            return cast(
                dict[str, Any], await api.create_namespaced_custom_object(GROUP, VERSION, NAMESPACE, "policysets", body)
            )

        valid_rules: list[dict[str, Any]] = [
            {"type": "kubernetes_role_binding", "role": {"kind": "Role", "name": "reader"}, "namespaces": ["team-a"]},
            {"type": "kubernetes_cluster_role_binding", "clusterRole": "reader"},
            {"type": "egress", "hosts": ["*.example.com"], "methods": ["GET"], "paths": ["/health"]},
            {"type": "egress", "hosts": ["*"], "clusterInternal": False},
            {"type": "egress", "hosts": ["api.example.com"], "credentialRef": {"name": "api-token"}},
        ]
        for rule in valid_rules:
            created = await create([rule], True)
            assert created is not None
            assert "status" not in created

        schema = {
            "type": "object",
            "properties": {"name": {"type": "string", "x-opaque-extension": {"mode": "keep"}}},
            "required": ["name"],
        }
        valid_policies: list[dict[str, Any]] = [
            {"type": "exact_actions", "actions": {"github": ["search_code"]}},
            {"type": "argument_schema", "actions": {"github": ["search_code"]}, "schema": schema},
            {
                "type": "github_repository",
                "actions": {"github": ["search_code"]},
                "owner": "owner",
                "repository": "repo",
            },
            {"type": "github_public_repository", "actions": {"github": ["search_code"]}},
            {
                "type": "home_assistant_entity_control",
                "actions": {"home-assistant": ["call_service"]},
                "entities": {"light.office": ["turn_on"]},
            },
        ]
        for policy in valid_policies:
            created = await create([{"type": "action_auto_approve", "policy": policy}], True)
            assert created is not None
            if policy["type"] == "argument_schema":
                assert created["spec"]["rules"][0]["policy"]["schema"] == schema

        invalid_rules: list[list[dict[str, Any]]] = [
            [{}],
            [],
            [{"type": "unknown"}],
            [{"type": "kubernetes_role_binding", "role": {"kind": "Role", "name": "reader"}}],
            [
                {
                    "type": "kubernetes_role_binding",
                    "role": {"kind": "ServiceAccount", "name": "reader"},
                    "namespaces": ["team-a"],
                }
            ],
            [{"type": "kubernetes_role_binding", "role": {"kind": "Role", "name": ""}, "namespaces": ["team-a"]}],
            [{"type": "kubernetes_role_binding", "role": {"kind": "Role", "name": "reader"}, "namespaces": []}],
            [{"type": "kubernetes_role_binding", "role": {"kind": "Role", "name": "reader"}, "namespaces": [""]}],
            [
                {
                    "type": "kubernetes_role_binding",
                    "role": {"kind": "Role", "name": "reader"},
                    "namespaces": ["team-a", "team-a"],
                }
            ],
            [
                {
                    "type": "kubernetes_role_binding",
                    "role": {"kind": "Role", "name": "reader"},
                    "namespaces": ["team-a"],
                    "clusterInternal": False,
                }
            ],
            [{"type": "kubernetes_cluster_role_binding", "clusterRole": "reader", "namespaces": ["team-a"]}],
            [{"type": "kubernetes_cluster_role_binding", "clusterRole": ""}],
            [{"type": "egress", "hosts": []}],
            [{"type": "egress", "hosts": ["*example.com"]}],
            [{"type": "egress", "hosts": ["*"], "credentialRef": {"name": "token"}}],
            [{"type": "egress", "hosts": ["api.example.com"], "credentialRef": {"name": ""}}],
            [{"type": "egress", "hosts": ["*"], "clusterInternal": True}],
            [{"type": "egress", "hosts": ["example.com"], "methods": []}],
            [{"type": "egress", "hosts": ["example.com"], "methods": ["get"]}],
            [{"type": "egress", "hosts": ["example.com"], "paths": []}],
            [{"type": "egress", "hosts": ["example.com"], "paths": [""]}],
            [{"type": "action_auto_approve"}],
            [{"type": "action_auto_approve", "policy": {"type": "exact_actions", "actions": {}, "schema": {}}}],
            [{"type": "action_auto_approve", "policy": {"type": "argument_schema", "actions": {"g": ["a"]}}}],
            [
                {
                    "type": "action_auto_approve",
                    "policy": {"type": "github_repository", "actions": {"g": ["a"]}, "owner": "owner"},
                }
            ],
            [
                {
                    "type": "action_auto_approve",
                    "policy": {"type": "github_public_repository", "actions": {"g": ["a"]}, "owner": "owner"},
                }
            ],
            [
                {
                    "type": "action_auto_approve",
                    "policy": {"type": "home_assistant_entity_control", "actions": {"g": ["a"]}, "entities": {}},
                }
            ],
            [{"type": "action_auto_approve", "policy": {"type": "exact_actions", "actions": {"g": [""]}}}],
            [{"type": "action_auto_approve", "policy": {"type": "exact_actions", "actions": {"g": []}}}],
            [{"type": "egress", "hosts": ["example.com"], "policy": valid_policies[0]}],
        ]
        for rules in invalid_rules:
            await create(rules, False)

        await create([valid_rules[0]] * 257, False)


if __name__ == "__main__":
    pytest_bazel.main()
