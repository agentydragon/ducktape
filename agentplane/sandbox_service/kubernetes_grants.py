"""Approved Kubernetes binding templates: the deployment catalog's role and scope for each name."""

from __future__ import annotations

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
