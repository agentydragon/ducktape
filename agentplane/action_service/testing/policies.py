"""PolicySets and PolicyBindings put into a `PolicyIndex` the way the informer parses them, so a rule
the service refuses stays refused."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from agentplane import policy_sets
from agentplane.action_service.policies.resources import ACTION_RULE_TYPE, parse_action_rule
from agentplane.action_service.policy_informer import PolicyIndex
from agentplane.action_service.testing.callers import TEST_NAMESPACE
from agentplane.subjects import ServiceAccountRef


def _meta(
    name: str, namespace: str, generation: int, resource_version: str, labels: Mapping[str, str] | None
) -> dict[str, Any]:
    return {
        "name": name,
        "namespace": namespace,
        "uid": f"uid-{name}",
        "generation": generation,
        "resourceVersion": resource_version,
        "labels": dict(labels or {}),
    }


def add_rule_set(
    index: PolicyIndex, name: str, rules: list[dict[str, Any]], *, generation: int = 1, namespace: str = TEST_NAMESPACE
) -> None:
    """A PolicySet of exactly these rules, of any reader's type."""
    index.rule_sets[name] = policy_sets.parse_policy_set(
        {"metadata": _meta(name, namespace, generation, "1", None), "spec": {"rules": rules}},
        parse_rule=parse_action_rule,
    )


def add_policy_set(
    index: PolicyIndex,
    name: str,
    policies: list[dict[str, Any]],
    *,
    generation: int = 1,
    namespace: str = TEST_NAMESPACE,
) -> None:
    """A PolicySet of one `action_auto_approve` rule per policy, in order."""
    add_rule_set(
        index,
        name,
        [{"type": ACTION_RULE_TYPE, "policy": policy} for policy in policies],
        generation=generation,
        namespace=namespace,
    )


def add_policy_binding(
    index: PolicyIndex,
    name: str,
    subject: ServiceAccountRef,
    sets: list[str],
    *,
    resource_version: str = "1",
    labels: Mapping[str, str] | None = None,
    expires_at: datetime | None = None,
    namespace: str = TEST_NAMESPACE,
) -> None:
    index.policy_bindings[name] = policy_sets.PolicyBinding.model_validate(
        {
            "metadata": _meta(name, namespace, 1, resource_version, labels),
            "spec": {
                "subject": subject.model_dump(),
                "policySets": sets,
                **({"expiresAt": expires_at.isoformat()} if expires_at is not None else {}),
            },
        }
    )
