"""Resolve the Kubernetes permissions declared by synthesized cdk8s resources."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import product
from typing import Any


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
    """Resolve declared RBAC from the dictionaries returned by ``Testing.synth``.

    This is not an apiserver authorizer: admission, impersonation, egress, and access obtained
    through exec/node proxy are outside this declarative contract. Aggregated roles fail closed.
    """

    def __init__(self, resources: Iterable[dict[str, Any]]):
        self.roles: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.bindings: list[dict[str, Any]] = []
        for resource in resources:
            kind = resource["kind"]
            metadata = resource["metadata"]
            if kind in {"Role", "ClusterRole"}:
                key = (kind, metadata.get("namespace", ""), metadata["name"])
                if key in self.roles:
                    assert self.roles[key]["rules"] == resource["rules"], key
                self.roles[key] = resource
            elif kind in {"RoleBinding", "ClusterRoleBinding"}:
                self.bindings.append(resource)

    def rules(self, kind: str, name: str, namespace: str | None) -> set[Permission]:
        assert kind in ("Role", "ClusterRole"), kind
        assert kind != "Role" or namespace is not None, (kind, name, namespace)
        role = self.roles[(kind, (namespace or "") if kind == "Role" else "", name)]
        assert role.get("aggregationRule") is None, f"Resolve aggregated Role explicitly: {name}"
        permissions: set[Permission] = set()
        for rule in role["rules"]:
            non_resource_urls = rule.get("nonResourceURLs", [])
            resources = rule.get("resources", [])
            api_groups = rule.get("apiGroups", [])
            resource_names = rule.get("resourceNames", [])
            if non_resource_urls:
                assert not resources, rule
                assert not api_groups, rule
                assert not resource_names, rule
                # Non-resource URL rules have no effect in a namespaced RoleBinding.
                if namespace is None:
                    permissions.update(
                        Permission(None, "", "", verb, url=url)
                        for verb, url in product(rule["verbs"], non_resource_urls)
                    )
            else:
                assert resources, rule
                assert api_groups, rule
                names: tuple[str | None, ...] = tuple(resource_names) or (None,)
                permissions.update(
                    Permission(namespace, group, resource, verb, name)
                    for group, resource, verb, name in product(api_groups, resources, rule["verbs"], names)
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
            if any(
                (subject["kind"], subject["name"], subject.get("namespace", "")) in subjects
                for subject in binding.get("subjects", [])
            ):
                role_ref = binding["roleRef"]
                assert role_ref.get("apiGroup") == "rbac.authorization.k8s.io", binding
                assert role_ref["kind"] in ("Role", "ClusterRole"), binding
                namespace = binding["metadata"].get("namespace") if binding["kind"] == "RoleBinding" else None
                permissions.update(self.rules(role_ref["kind"], role_ref["name"], namespace))
        return permissions

    def policy_set_permissions(
        self, policy_sets: dict[str, list[dict[str, Any]]], names: Iterable[str]
    ) -> set[Permission]:
        permissions: set[Permission] = set()
        for rule in (rule for name in names for rule in policy_sets[name]):
            if rule["type"] == "kubernetes_role_binding":
                for namespace in rule["namespaces"]:
                    permissions.update(self.rules(rule["role"]["kind"], rule["role"]["name"], namespace))
            elif rule["type"] == "kubernetes_cluster_role_binding":
                permissions.update(self.rules("ClusterRole", rule["clusterRole"], None))
        return permissions

    def managed(
        self, config: dict[str, Any], policy_sets: dict[str, list[dict[str, Any]]], preset: str, *, namespace: str
    ) -> set[Permission]:
        """`preset`'s Kubernetes access: its PolicySets' (name -> spec rules) Kubernetes rules."""
        # Fresh sandbox SAs also inherit standing group bindings. Do not mistake
        # the selectable catalog for their entire declarative authority.
        permissions = self._subjects(
            {
                ("Group", "system:authenticated", ""),
                ("Group", "system:serviceaccounts", ""),
                ("Group", f"system:serviceaccounts:{namespace}", ""),
            }
        )
        names = config["sandbox_presets"][preset]["policy_sets"]
        assert len(names) == len(set(names)), names
        permissions.update(self.policy_set_permissions(policy_sets, names))
        return permissions
