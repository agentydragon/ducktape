"""Domain contracts for temporary Kubernetes grants."""

from __future__ import annotations

import datetime
from uuid import UUID

import pytest
import pytest_bazel
from pydantic import TypeAdapter, ValidationError

from haku.console.grants.kubernetes.models import (
    Grant,
    GrantScope,
    NamespacesGrantScope,
    NonResourceGrantScope,
    Rule,
    validate_grant_scope_rules,
)
from haku.console.grants.principal import AgentGrantPrincipal


def resource_rule(**kwargs: object) -> Rule:
    return Rule(verbs=("get",), api_groups=("",), resources=("pods",), **kwargs)


def test_resource_rule_canonicalizes_values() -> None:
    rule = Rule.model_validate(
        {"api_groups": [""], "resources": ["pods"], "verbs": ["get"], "resource_names": ["pod-a"]}
    )

    assert rule.api_groups == frozenset({""})
    assert rule.resources == frozenset({"pods"})
    assert rule.resource_names == frozenset({"pod-a"})
    assert rule.model_dump(mode="json")["resource_names"] == ["pod-a"]


def test_rule_rejects_kubernetes_wire_names_inside_the_domain() -> None:
    with pytest.raises(ValidationError, match="apiGroups"):
        Rule.model_validate({"apiGroups": [""], "resources": ["pods"], "verbs": ["get"]})


def test_rule_models_rbac_collections_as_sets_and_serializes_stably() -> None:
    rule = Rule(api_groups=("apps", ""), resources=("pods", "deployments"), verbs=("list", "get"))

    assert rule.verbs == frozenset({"get", "list"})
    assert rule.model_dump(mode="json")["verbs"] == ["get", "list"]
    assert rule.model_dump(mode="json")["api_groups"] == ["", "apps"]


def test_rule_rejects_scalar_strings_for_collection_fields() -> None:
    with pytest.raises(ValidationError, match="valid frozenset"):
        Rule(api_groups=("",), resources=("pods",), verbs="get")
    with pytest.raises(ValidationError, match="valid frozenset"):
        Rule(api_groups="", resources=("pods",), verbs=("get",))


def test_rule_rejects_mixed_or_empty_shape() -> None:
    with pytest.raises(ValidationError, match="must contain resources"):
        Rule(verbs=("get",))
    with pytest.raises(ValidationError, match="must contain resources"):
        Rule(api_groups=("apps",), verbs=("get",))
    with pytest.raises(ValidationError, match="must contain resources"):
        Rule(resource_names=("pod-a",), verbs=("get",))
    with pytest.raises(ValidationError, match="at least 1 item"):
        Rule(api_groups=("",), resources=("pods",), verbs=())
    with pytest.raises(ValidationError, match="cannot mix"):
        Rule(api_groups=("",), resources=("pods",), verbs=("get",), non_resource_urls=("/healthz",))


def test_scope_is_a_discriminated_union_consistent_with_rule_kind() -> None:
    adapter: TypeAdapter[GrantScope] = TypeAdapter(GrantScope)
    with pytest.raises(ValidationError, match="at least 1 item"):
        adapter.validate_python({"kind": "namespaces", "namespaces": []})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        adapter.validate_python({"kind": "cluster", "namespaces": ["default"]})
    with pytest.raises(ValidationError, match="use all_namespaces"):
        NamespacesGrantScope(namespaces=("*",))
    with pytest.raises(ValueError, match="requires only non-resource"):
        validate_grant_scope_rules(NonResourceGrantScope(), (resource_rule(),))


def test_agent_grant_principal_may_differ_from_lifecycle_owner() -> None:
    grant = Grant(
        grant_id=UUID(int=1),
        owner_agent_id=UUID(int=2),
        principal=AgentGrantPrincipal(agent_id=UUID(int=3)),
        source_tool_call_id="tc_source",
        scope=NamespacesGrantScope(namespaces=("demo",)),
        rules=(resource_rule(),),
        created_at=datetime.datetime(2026, 8, 21, tzinfo=datetime.UTC),
        expires_at=datetime.datetime(2026, 8, 21, 1, tzinfo=datetime.UTC),
    )

    assert grant.principal == AgentGrantPrincipal(agent_id=UUID(int=3))


if __name__ == "__main__":
    pytest_bazel.main()
