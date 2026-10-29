from __future__ import annotations

from typing import Any

import pytest_bazel
from pydantic import BaseModel

from agentplane.policy_sets import NumberedRule, PolicySet, RefusedPolicySet, parse_policy_set

NAMESPACE = "policy-sets-test"


class HostRule(BaseModel):
    hosts: list[str]


def parse_host_rule(raw: dict[str, Any]) -> HostRule | None:
    return HostRule.model_validate(raw) if raw["type"] == "egress" else None


def raw_set(*rules: dict[str, object]) -> dict[str, object]:
    return {
        "metadata": {"name": "mixed", "namespace": NAMESPACE, "uid": "u", "generation": 1, "resourceVersion": "7"},
        "spec": {"rules": rules},
    }


def test_a_reader_keeps_its_own_rule_types_numbered_among_all_rules() -> None:
    parsed = parse_policy_set(
        raw_set(
            {"type": "kubernetes_cluster_role_binding", "clusterRole": "view"}, {"type": "egress", "hosts": ["a.test"]}
        ),
        parse_rule=parse_host_rule,
    )
    assert parsed == PolicySet(
        name="mixed", rules=(NumberedRule(number=1, rule=HostRule(hosts=["a.test"])),), generation=1
    )


def test_a_malformed_rule_of_the_readers_own_type_refuses_the_set() -> None:
    parsed = parse_policy_set(
        raw_set({"type": "egress", "hosts": ["a.test"]}, {"type": "egress"}), parse_rule=parse_host_rule
    )
    assert isinstance(parsed, RefusedPolicySet)
    assert (parsed.name, parsed.reason.startswith("rules[1]:")) == ("mixed", True)


if __name__ == "__main__":
    pytest_bazel.main()
