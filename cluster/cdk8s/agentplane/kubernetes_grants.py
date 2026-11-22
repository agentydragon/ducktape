"""Approved Kubernetes binding templates: the deployment catalog's role and scope for each name."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

DnsName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", min_length=1, max_length=63)]


class RoleRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["Role", "ClusterRole"]
    name: DnsName


class ClusterRoleRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["ClusterRole"]
    name: DnsName


class RoleBindingGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    namespace: DnsName
    role_ref: RoleRef


class ClusterRoleBindingGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role_ref: ClusterRoleRef


type KubernetesGrant = RoleBindingGrant | ClusterRoleBindingGrant
