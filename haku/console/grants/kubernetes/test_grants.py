"""Domain contracts for temporary Kubernetes grants."""

from __future__ import annotations

import datetime
from uuid import UUID

import pytest
import pytest_bazel
from pydantic import TypeAdapter, ValidationError

from haku.console.grants.envelope import GrantStatus
from haku.console.grants.kubernetes.models import (
    KubernetesAllNamespacesGrantScope,
    KubernetesClusterGrantScope,
    KubernetesGrant,
    KubernetesGrantScope,
    KubernetesNamespacesGrantScope,
    KubernetesNonResourceGrantScope,
    KubernetesRule,
    validate_grant_scope_rules,
)
from haku.console.grants.kubernetes.service import rule_covers, rules_cover, scope_covers
from haku.console.grants.principal import AgentGrantPrincipal


def resource_rule(**kwargs: object) -> KubernetesRule:
    return KubernetesRule(verbs=("get",), api_groups=("",), resources=("pods",), **kwargs)


def test_resource_rule_canonicalizes_values() -> None:
    rule = KubernetesRule.model_validate(
        {"api_groups": [""], "resources": ["pods"], "verbs": ["get"], "resource_names": ["pod-a"]}
    )

    assert rule.api_groups == frozenset({""})
    assert rule.resources == frozenset({"pods"})
    assert rule.resource_names == frozenset({"pod-a"})
    assert rule.model_dump(mode="json")["resource_names"] == ["pod-a"]


def test_rule_rejects_kubernetes_wire_names_inside_the_domain() -> None:
    with pytest.raises(ValidationError, match="apiGroups"):
        KubernetesRule.model_validate({"apiGroups": [""], "resources": ["pods"], "verbs": ["get"]})


def test_rule_models_rbac_collections_as_sets_and_serializes_stably() -> None:
    rule = KubernetesRule(api_groups=("apps", ""), resources=("pods", "deployments"), verbs=("list", "get"))

    assert rule.verbs == frozenset({"get", "list"})
    assert rule.model_dump(mode="json")["verbs"] == ["get", "list"]
    assert rule.model_dump(mode="json")["api_groups"] == ["", "apps"]


def test_rule_rejects_scalar_strings_for_collection_fields() -> None:
    with pytest.raises(ValidationError, match="valid frozenset"):
        KubernetesRule(api_groups=("",), resources=("pods",), verbs="get")
    with pytest.raises(ValidationError, match="valid frozenset"):
        KubernetesRule(api_groups="", resources=("pods",), verbs=("get",))


def test_rule_rejects_mixed_or_empty_shape() -> None:
    with pytest.raises(ValidationError, match="must contain resources"):
        KubernetesRule(verbs=("get",))
    with pytest.raises(ValidationError, match="must contain resources"):
        KubernetesRule(api_groups=("apps",), verbs=("get",))
    with pytest.raises(ValidationError, match="must contain resources"):
        KubernetesRule(resource_names=("pod-a",), verbs=("get",))
    with pytest.raises(ValidationError, match="at least 1 item"):
        KubernetesRule(api_groups=("",), resources=("pods",), verbs=())
    with pytest.raises(ValidationError, match="cannot mix"):
        KubernetesRule(api_groups=("",), resources=("pods",), verbs=("get",), non_resource_urls=("/healthz",))


def test_scope_supports_exact_or_all_namespaces_without_implying_cluster_scope() -> None:
    exact = KubernetesNamespacesGrantScope(namespaces=("diagnostics", "public-coder-agent"))
    requested = KubernetesNamespacesGrantScope(namespaces=("diagnostics",))
    other = KubernetesNamespacesGrantScope(namespaces=("default",))
    all_namespaces = KubernetesAllNamespacesGrantScope()
    cluster = KubernetesClusterGrantScope()

    assert scope_covers(exact, requested)
    assert not scope_covers(exact, other)
    assert scope_covers(all_namespaces, requested)
    assert not scope_covers(all_namespaces, cluster)


def test_scope_is_a_discriminated_union_consistent_with_rule_kind() -> None:
    adapter: TypeAdapter[KubernetesGrantScope] = TypeAdapter(KubernetesGrantScope)
    with pytest.raises(ValidationError, match="at least 1 item"):
        adapter.validate_python({"kind": "namespaces", "namespaces": []})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        adapter.validate_python({"kind": "cluster", "namespaces": ["default"]})
    with pytest.raises(ValidationError, match="use all_namespaces"):
        KubernetesNamespacesGrantScope(namespaces=("*",))
    with pytest.raises(ValueError, match="requires only non-resource"):
        validate_grant_scope_rules(KubernetesNonResourceGrantScope(), (resource_rule(),))


def test_agent_grant_principal_must_belong_to_lifecycle_owner() -> None:
    with pytest.raises(ValidationError, match="must belong to the lifecycle owner"):
        KubernetesGrant(
            grant_id=UUID(int=1),
            owner_agent_id=UUID(int=2),
            principal=AgentGrantPrincipal(agent_id=UUID(int=3)),
            source_tool_call_id="tc_source",
            scope=KubernetesNamespacesGrantScope(namespaces=("demo",)),
            rules=(resource_rule(),),
            status=GrantStatus.ACTIVE,
            created_at=datetime.datetime(2026, 8, 21, tzinfo=datetime.UTC),
            expires_at=datetime.datetime(2026, 8, 21, 1, tzinfo=datetime.UTC),
        )


def test_matching_is_conservative_about_resource_names() -> None:
    all_pods = resource_rule()
    one_pod = resource_rule(resource_names=("pod-a",))
    other_pod = resource_rule(resource_names=("pod-b",))

    assert rule_covers(all_pods, one_pod)
    assert not rule_covers(one_pod, other_pod)
    assert not rule_covers(one_pod, all_pods)


def test_matching_allows_only_explicit_wildcards() -> None:
    granted = KubernetesRule(api_groups=("*",), resources=("*",), verbs=("*",))
    requested = KubernetesRule(api_groups=("apps",), resources=("deployments/status",), verbs=("patch",))

    assert rule_covers(granted, requested)
    assert not rule_covers(
        KubernetesRule(api_groups=("apps",), resources=("deployments",), verbs=("patch",)), requested
    )


def test_non_resource_urls_use_exact_or_terminal_prefix_matching() -> None:
    granted = KubernetesRule(verbs=("get",), non_resource_urls=("/version", "/api/*"))

    assert rule_covers(granted, KubernetesRule(verbs=("get",), non_resource_urls=("/version", "/api/v1")))
    assert not rule_covers(granted, KubernetesRule(verbs=("get",), non_resource_urls=("/apis",)))


def test_rules_cover_requires_every_request_rule() -> None:
    granted = (resource_rule(),)
    requested = (resource_rule(), KubernetesRule(verbs=("list",), api_groups=("",), resources=("pods",)))

    assert not rules_cover(granted, requested)


if __name__ == "__main__":
    pytest_bazel.main()
