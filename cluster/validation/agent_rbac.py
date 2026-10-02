"""Compare declared RBAC rule coverage, independently of profile/role names.

This is not an apiserver authorizer: live bindings, admission, impersonation, egress,
and access obtained through exec/node proxy are outside this declarative contract.
Unsupported aggregation and Kyverno match shapes fail closed instead of losing grants.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import product
from typing import Any

from cluster.validation.k8s import K8sResource, RbacRoleRef, RoleBindingResource, RoleResource


@dataclass(frozen=True)
class Permission:
    # None is cluster-wide binding scope; a namespace never covers cluster-wide access.
    namespace: str | None
    api_group: str
    resource: str
    verb: str
    name: str | None = None
    url: str | None = None

    def covers(self, other: Permission) -> bool:
        if self.verb not in ("*", other.verb):
            return False
        if self.url is not None or other.url is not None:
            return (
                self.url is not None
                and other.url is not None
                and (self.url == other.url or (self.url.endswith("*") and other.url.startswith(self.url[:-1])))
            )
        resource_matches = self.resource in ("*", other.resource) or (
            self.resource.startswith("*/")
            and "/" in other.resource
            and self.resource[1:] == other.resource[other.resource.index("/") :]
        )
        return (
            (self.namespace is None or self.namespace == other.namespace)
            and self.api_group in ("*", other.api_group)
            and resource_matches
            and (self.name is None or self.name == other.name)
        )


def uncovered(required: Iterable[Permission], available: Iterable[Permission]) -> set[Permission]:
    available = tuple(available)
    return {permission for permission in required if not any(grant.covers(permission) for grant in available)}


class Rbac:
    def __init__(self, resources: Iterable[K8sResource]):
        self.roles: dict[tuple[str, str, str], RoleResource] = {}
        self.bindings: list[RoleBindingResource] = []
        for resource in resources:
            if isinstance(resource, RoleResource):
                key = (resource.kind, resource.namespace, resource.name)
                if key in self.roles:
                    assert self.roles[key].rules == resource.rules, key
                self.roles[key] = resource
            elif isinstance(resource, RoleBindingResource):
                self.bindings.append(resource)

    def rules(self, ref: RbacRoleRef, namespace: str | None) -> set[Permission]:
        assert ref.api_group == "rbac.authorization.k8s.io"
        assert ref.kind in ("Role", "ClusterRole"), ref
        assert ref.kind != "Role" or namespace is not None, ref
        role = self.roles[(ref.kind, (namespace or "") if ref.kind == "Role" else "", ref.name)]
        assert role.aggregation_rule is None, f"Resolve aggregated Role explicitly: {ref.name}"
        permissions: set[Permission] = set()
        for rule in role.rules:
            if rule.non_resource_urls:
                assert not rule.resources, rule
                assert not rule.api_groups, rule
                assert not rule.resource_names, rule
                # Non-resource URL rules have no effect in a namespaced RoleBinding.
                if namespace is None:
                    permissions.update(
                        Permission(None, "", "", verb, url=url)
                        for verb, url in product(rule.verbs, rule.non_resource_urls)
                    )
            else:
                assert rule.resources, rule
                assert rule.api_groups, rule
                names: tuple[str | None, ...] = tuple(rule.resource_names) or (None,)
                permissions.update(
                    Permission(namespace, group, resource, verb, name)
                    for group, resource, verb, name in product(rule.api_groups, rule.resources, rule.verbs, names)
                )
        return permissions

    def identity(self, kind: str, name: str, namespace: str = "") -> set[Permission]:
        subjects = {(kind, name, namespace), ("Group", "system:authenticated", "")}
        if kind == "ServiceAccount":
            subjects.update(
                {
                    ("Group", "system:serviceaccounts", ""),
                    ("Group", f"system:serviceaccounts:{namespace}", ""),
                    ("User", f"system:serviceaccount:{namespace}:{name}", ""),
                }
            )
        return self._subjects(subjects)

    def _subjects(self, subjects: set[tuple[str, str, str]]) -> set[Permission]:
        permissions: set[Permission] = set()
        for binding in self.bindings:
            if any((s.kind, s.name, s.namespace) in subjects for s in binding.subjects):
                assert binding.role_ref is not None, binding
                permissions.update(
                    self.rules(binding.role_ref, binding.namespace if binding.kind == "RoleBinding" else None)
                )
        return permissions

    def managed(self, config: dict[str, Any], preset: str, *, namespace: str) -> set[Permission]:
        # Fresh sandbox SAs also inherit standing group bindings. Do not mistake
        # the selectable catalog for their entire declarative authority.
        permissions = self._subjects(
            {
                ("Group", "system:authenticated", ""),
                ("Group", "system:serviceaccounts", ""),
                ("Group", f"system:serviceaccounts:{namespace}", ""),
            }
        )
        names = config["sandbox_presets"][preset]["kubernetes_grants"]
        assert len(names) == len(set(names)), names
        for name in names:
            grant = config["kubernetes_grants"][name]
            ref = RbacRoleRef(api_group="rbac.authorization.k8s.io", **grant["role_ref"])
            permissions.update(self.rules(ref, grant.get("namespace")))
        return permissions


def namespace_readers(policy: dict[str, Any], namespaces: Iterable[K8sResource]) -> list[RoleBindingResource]:
    """Expand the real Kyverno policy's label selectors and generated binding data.

    Deliberately bounded to this policy's grammar. New conditions must extend this
    evaluator and its tests, never be ignored or approximated by a hard-coded role list.
    """
    assert policy["metadata"]["name"] == "generate-agent-diagnostics-readers"
    bindings = []
    for rule in policy["spec"]["rules"]:
        assert set(rule) <= {"name", "match", "generate", "skipBackgroundRequests"}, rule
        assert set(rule["match"]) == {"any"}, rule
        selectors = []
        for match in rule["match"]["any"]:
            assert set(match) == {"resources"}, match
            resources = match["resources"]
            assert set(resources) == {"kinds", "selector"}, resources
            assert resources["kinds"] == ["Namespace"], resources
            assert set(resources["selector"]) == {"matchLabels"}, resources
            selectors.append(resources["selector"]["matchLabels"])
        generate = rule["generate"]
        assert generate["kind"] == "RoleBinding"
        assert generate["namespace"] == "{{request.object.metadata.name}}"
        assert generate["generateExisting"]
        assert generate["synchronize"]
        for namespace in namespaces:
            assert namespace.kind == "Namespace"
            if any(all(namespace.metadata.labels.get(k) == v for k, v in selector.items()) for selector in selectors):
                data = generate["data"]
                bindings.append(
                    RoleBindingResource.model_validate(
                        {
                            **data,
                            "apiVersion": generate["apiVersion"],
                            "kind": generate["kind"],
                            "metadata": {
                                **data.get("metadata", {}),
                                "name": generate["name"],
                                "namespace": namespace.name,
                            },
                        }
                    )
                )
    return bindings
