"""Approved Kubernetes binding templates and a Sandbox launch's concrete selections.

The deployment catalog chooses the role and scope. A launch supplies catalog names only;
the app always supplies the subject from the Sandbox it actually created.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

DnsName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", min_length=1, max_length=63)]


class RoleRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["Role", "ClusterRole"]
    name: DnsName


class ClusterRoleRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["ClusterRole"]
    name: DnsName


class RoleBindingGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["RoleBinding"]
    namespace: DnsName
    role_ref: RoleRef


class ClusterRoleBindingGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["ClusterRoleBinding"]
    role_ref: ClusterRoleRef


KubernetesGrant = Annotated[RoleBindingGrant | ClusterRoleBindingGrant, Field(discriminator="kind")]


class KubernetesGrantView(BaseModel):
    """An enabled choice with its fixed target, for the operator's launch picker."""

    model_config = ConfigDict(extra="forbid")

    name: DnsName
    kind: Literal["RoleBinding", "ClusterRoleBinding"]
    namespace: DnsName | None = None
    role_ref: RoleRef | ClusterRoleRef


class ResolvedGrant(BaseModel):
    """Snapshot of the exact choice; catalog changes never retarget an existing Sandbox."""

    model_config = ConfigDict(extra="forbid")

    name: DnsName
    grant: KubernetesGrant


class UnknownKubernetesGrantError(ValueError):
    def __init__(self, names: list[str]) -> None:
        super().__init__(f"unknown Kubernetes grants: {', '.join(names)}")
        self.names = names


class DuplicateKubernetesGrantError(ValueError):
    def __init__(self) -> None:
        super().__init__("duplicate Kubernetes grant names")


def resolve_grants(names: list[str], catalog: Mapping[str, KubernetesGrant]) -> list[ResolvedGrant]:
    """Resolve once, before creating anything, with one binding per distinct choice."""
    if unknown := sorted(set(names) - catalog.keys()):
        raise UnknownKubernetesGrantError(unknown)
    if len(names) != len(set(names)):
        raise DuplicateKubernetesGrantError
    return [ResolvedGrant(name=name, grant=catalog[name]) for name in names]


def grant_views(catalog: Mapping[str, KubernetesGrant]) -> list[KubernetesGrantView]:
    return [
        KubernetesGrantView(
            name=name,
            kind=grant.kind,
            namespace=grant.namespace if isinstance(grant, RoleBindingGrant) else None,
            role_ref=grant.role_ref,
        )
        for name, grant in sorted(catalog.items())
    ]
