"""Exercise PolicySet unions and opaque JSON through Kubernetes admission and CEL."""

from typing import Any

import pytest_bazel

from agentplane.testing.crd_admission import crd_namespace

NAMESPACE = "policyset-admission"


async def test_policyset_admission() -> None:
    async with crd_namespace(NAMESPACE) as namespace:
        policy_sets = namespace.kind("policysets", "PolicySet")

        def action(kind: str, **fields: Any) -> dict[str, Any]:
            return {"type": "action_auto_approve", "policy": {"type": kind, "actions": {"g": ["a"]}, **fields}}

        def inbox(targets: dict[str, Any], **fields: Any) -> dict[str, Any]:
            return {"type": "session_inbox_access", "operations": ["send_message"], "targets": targets, **fields}

        def environment(environments: list[dict[str, Any]], **fields: Any) -> dict[str, Any]:
            return {"type": "environment", "operations": ["resume"], "environments": environments, **fields}

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
            environment([{"sandbox": {"name": "worker"}}]),
            environment(
                [
                    {
                        "sandbox": {"name": "worker", "uid": "3b1c0f1e-7a52-4f0c-9d52-2d7a54f0b2a1"},
                        "boundIdentity": {"name": "worker"},
                    }
                ]
            ),
            environment([{"boundIdentity": {"name": "worker"}}]),
            environment([{"boundIdentity": {"self": True}}]),
            environment([{"boundIdentity": {"self": True}}], operations=["bind_session"]),
            environment(
                [{"sandbox": pinned, "boundIdentity": {"name": "worker"}}], operations=["resume", "bind_session"]
            ),
            environment([{"sandbox": {"name": "worker"}}, {"boundIdentity": {"self": True}}]),
        )
        for index, rule in enumerate(rules):
            created = await policy_sets.create(f"policy-{index}", {"rules": [rule]})
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
            {"type": "environment", "operations": ["resume"]},
            {"type": "environment", "environments": [{"sandbox": {"name": "worker"}}]},
            environment([{"sandbox": {"name": "worker"}}], operations=["send_message"]),
            environment([{"sandbox": {"name": "worker"}}], operations=["bind_session"]),
            environment([{"sandbox": {"name": "worker"}}], operations=["resume", "bind_session"]),
            environment([{"boundIdentity": {"self": True}}], operations=["read"]),
            environment([{}]),
            environment([{"sandbox": {}}]),
            environment([{"boundIdentity": {}}]),
            environment([{"boundIdentity": {"name": "worker", "self": True}}]),
            environment([{"sandbox": {"name": "worker"}}], targets={"allSandboxes": True}),
        )
        for rule in invalid_rules:
            await policy_sets.rejects_create({"rules": [rule]})


if __name__ == "__main__":
    pytest_bazel.main()
