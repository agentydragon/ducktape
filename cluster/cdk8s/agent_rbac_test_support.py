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

    This is not an apiserver authorizer: admission, egress, and access obtained through exec/node
    proxy are outside this declarative contract, as is impersonation other than the egress proxy's
    acting as a rendered `kubernetesServiceAccount` credential. Aggregated roles fail closed.
    """

    def __init__(self, resources: Iterable[dict[str, Any]]):
        self.roles: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.bindings: list[dict[str, Any]] = []
        # Agentplane's catalog, by name; staging renders each in its policy namespace.
        self.policy_sets: dict[str, list[dict[str, Any]]] = {}
        self.role_sets: dict[str, list[dict[str, Any]]] = {}
        self.credentials: dict[str, dict[str, Any]] = {}
        # ServiceAccountGrant specs.
        self.grants: list[dict[str, Any]] = []
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
            elif kind == "ServiceAccountGrant":
                self.grants.append(resource["spec"])
            elif kind == "PolicySet":
                _add_unique(self.policy_sets, metadata["name"], resource["spec"]["rules"])
            elif kind == "KubernetesRoleSet":
                _add_unique(self.role_sets, metadata["name"], resource["spec"]["rules"])
            elif kind == "EgressCredential":
                _add_unique(self.credentials, metadata["name"], resource["spec"]["source"])

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
        return self.subjects(subjects)

    def subjects(self, subjects: set[tuple[str, str, str]]) -> set[Permission]:
        """What rendered bindings naming any of `subjects` ((kind, name, namespace)) grant."""
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

    def bound(self, roles: Iterable[BoundRole]) -> set[Permission]:
        return {permission for role in roles for permission in self.rules(role.kind, role.name, role.namespace)}

    def set_roles(self, name: str) -> set[BoundRole]:
        """The roles assignable set `name` binds: its `PolicySet`'s Kubernetes rules, or in the identity
        catalog its `KubernetesRoleSet`, which a holder reaches only through an account granted it."""
        if name in self.role_sets:
            return _bound_roles(self.role_sets[name])
        return _bound_roles(rule for rule in self.policy_sets[name] if rule["type"].startswith("kubernetes_"))

    def accounts(self, sets: Iterable[str]) -> set[tuple[str, str]]:
        """(namespace, name) of each ServiceAccount a holder of `sets` acts as through the egress proxy."""
        return {
            (account["namespace"], account["name"])
            for name in sets
            for rule in self.policy_sets[name]
            if "credentialRef" in rule
            # Some credentials, such as the proxy's own workload token, are not objects.
            and (account := self.credentials.get(rule["credentialRef"]["name"], {}).get("kubernetesServiceAccount"))
            is not None
        }

    def account_roles(self, namespace: str, name: str) -> set[BoundRole]:
        """The roles the Policy Controller binds for an account: what its grants' role sets expand to."""
        return {
            role
            for grant in self.grants
            if (grant["serviceAccount"]["namespace"], grant["serviceAccount"]["name"]) == (namespace, name)
            for role_set in grant["roleSets"]
            for role in _bound_roles(self.role_sets[role_set])
        }

    def held_roles(self, sets: Iterable[str]) -> set[BoundRole]:
        """The roles bound for a holder of the policy sets `sets` or for an account it acts as."""
        sets = tuple(sets)
        assert len(sets) == len(set(sets)), sets
        # A set's PolicySet binds its own Kubernetes rules for the holder; a role set is reached only
        # through an account (both catalog shapes render role sets, for launched Identities).
        roles = {
            role
            for name in sets
            for role in _bound_roles(
                rule for rule in self.policy_sets.get(name, []) if rule["type"].startswith("kubernetes_")
            )
        }
        for namespace, name in self.accounts(sets):
            roles |= self.account_roles(namespace, name)
        return roles

    def held(self, sets: Iterable[str]) -> set[Permission]:
        """A holder of the policy sets `sets`: its bound roles and the accounts' own rendered bindings."""
        sets = tuple(sets)
        permissions = self.bound(self.held_roles(sets))
        for namespace, name in self.accounts(sets):
            permissions |= self.identity("ServiceAccount", name, namespace)
        return permissions

    def launch(self, sets: Iterable[str], *, namespace: str) -> set[Permission]:
        """A sandbox launched into `namespace` holding `sets`."""
        # Fresh sandbox SAs also inherit standing group bindings. Do not mistake
        # the selectable catalog for their entire declarative authority.
        return self.held(sets) | self.subjects(
            {
                ("Group", "system:authenticated", ""),
                ("Group", "system:serviceaccounts", ""),
                ("Group", f"system:serviceaccounts:{namespace}", ""),
            }
        )


@dataclass(frozen=True)
class BoundRole:
    kind: str
    name: str
    # None is a ClusterRoleBinding.
    namespace: str | None


def _add_unique[T](by_name: dict[str, T], name: str, value: T) -> None:
    assert name not in by_name, name
    by_name[name] = value


def _bound_roles(rules: Iterable[dict[str, Any]]) -> set[BoundRole]:
    """The roles `rules` bind, written as `PolicySet` (`kubernetes_`-prefixed) or `KubernetesRoleSet` rules."""
    roles: set[BoundRole] = set()
    for rule in rules:
        kind = rule["type"].removeprefix("kubernetes_")
        if kind == "cluster_role_binding":
            roles.add(BoundRole("ClusterRole", rule["clusterRole"], None))
        else:
            assert kind == "role_binding", rule
            roles |= {BoundRole(rule["role"]["kind"], rule["role"]["name"], ns) for ns in rule["namespaces"]}
    return roles
