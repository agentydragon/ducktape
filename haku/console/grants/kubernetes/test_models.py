"""Domain contracts for temporary Kubernetes grants."""

from __future__ import annotations

import pytest
import pytest_bazel
from pydantic import TypeAdapter, ValidationError

from haku.console.grants.kubernetes.models import (
    GrantScope,
    NamespacesGrantScope,
    NonResourceGrantScope,
    Rule,
    validate_grant_scope_rules,
)


def resource_rule() -> Rule:
    return Rule(verbs={"get"}, api_groups={""}, resources={"pods"})


def test_rule_rejects_kubernetes_wire_names_inside_the_domain() -> None:
    # Empty `resource_names` means all names, so a silently dropped `resourceNames` would widen the rule.
    with pytest.raises(ValidationError, match="resourceNames"):
        Rule.model_validate({"api_groups": [""], "resources": ["pods"], "verbs": ["get"], "resourceNames": ["pod-a"]})


def test_rule_models_rbac_collections_as_sets_and_serializes_stably() -> None:
    rule = Rule(api_groups={"apps", ""}, resources={"pods", "deployments"}, verbs={"list", "get"})

    assert rule.verbs == frozenset({"get", "list"})
    assert rule.model_dump(mode="json")["verbs"] == ["get", "list"]
    assert rule.model_dump(mode="json")["api_groups"] == ["", "apps"]


def test_rule_rejects_mixed_or_empty_shape() -> None:
    with pytest.raises(ValidationError, match="must contain resources"):
        Rule(verbs={"get"})
    with pytest.raises(ValidationError, match="must contain resources"):
        Rule(api_groups={"apps"}, verbs={"get"})
    with pytest.raises(ValidationError, match="must contain resources"):
        Rule(resource_names={"pod-a"}, verbs={"get"})
    with pytest.raises(ValidationError, match="at least 1 item"):
        Rule(api_groups={""}, resources={"pods"}, verbs=frozenset())
    with pytest.raises(ValidationError, match="cannot mix"):
        Rule(api_groups={""}, resources={"pods"}, verbs={"get"}, non_resource_urls={"/healthz"})


def test_scope_is_a_discriminated_union_consistent_with_rule_kind() -> None:
    adapter: TypeAdapter[GrantScope] = TypeAdapter(GrantScope)
    with pytest.raises(ValidationError, match="at least 1 item"):
        adapter.validate_python({"kind": "namespaces", "namespaces": []})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        adapter.validate_python({"kind": "cluster", "namespaces": ["default"]})
    with pytest.raises(ValidationError, match="use all_namespaces"):
        NamespacesGrantScope(namespaces={"*"})
    with pytest.raises(ValueError, match="requires only non-resource"):
        validate_grant_scope_rules(NonResourceGrantScope(), (resource_rule(),))


if __name__ == "__main__":
    pytest_bazel.main()
