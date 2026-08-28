"""Typed vocabulary for temporary Kubernetes grants.

Capability scope/rules remain separate from the shared grant envelope
(`haku.console.grants.envelope`): this module owns only the granted *what* — conservative
RBAC-like rules inside an explicit scope.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_serializer,
    field_validator,
    model_validator,
)

from haku.console.grants.envelope import NON_EMPTY, GrantEnvelope, GrantStatus


class KubernetesGrantScopeKind(StrEnum):
    NAMESPACES = "namespaces"
    ALL_NAMESPACES = "all_namespaces"
    CLUSTER = "cluster"
    NON_RESOURCE = "non_resource"


def _clean_values(value: Iterable[str], field_name: str, *, allow_empty: bool = False) -> frozenset[str]:
    values = tuple(item.strip() for item in value)
    if not allow_empty and any(not item for item in values):
        raise ValueError(f"{field_name} must not contain empty values")
    return frozenset(values)


class KubernetesRule(BaseModel):
    """One conservative Kubernetes RBAC-like rule.

    Resource rules use ``api_groups``/``resources``/``verbs`` and optionally
    ``resource_names``.  Non-resource rules use ``non_resource_urls`` and ``verbs``.  The shape
    uses the same concepts as Kubernetes PolicyRule while remaining independent of that transport.
    Any Kubernetes wire-format translation belongs at the transport boundary.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_groups: frozenset[str] = Field(default_factory=frozenset)
    resources: frozenset[str] = Field(default_factory=frozenset)
    verbs: frozenset[NON_EMPTY] = Field(min_length=1)
    resource_names: frozenset[str] = Field(default_factory=frozenset)
    non_resource_urls: frozenset[str] = Field(default_factory=frozenset)

    @field_validator("api_groups", "resources", "resource_names", "non_resource_urls")
    @classmethod
    def normalize_values(cls, value: frozenset[str], info: ValidationInfo) -> frozenset[str]:
        assert info.field_name is not None
        return _clean_values(value, info.field_name, allow_empty=info.field_name == "api_groups")

    @field_validator("verbs")
    @classmethod
    def normalize_verbs(cls, value: frozenset[str]) -> frozenset[str]:
        return _clean_values(value, "verbs")

    @field_serializer("api_groups", "resources", "verbs", "resource_names", "non_resource_urls")
    def serialize_values(self, value: frozenset[str]) -> list[str]:
        return sorted(value)

    @model_validator(mode="after")
    def validate_kind(self) -> KubernetesRule:
        if self.non_resource_urls:
            if self.api_groups or self.resources or self.resource_names:
                raise ValueError("a Kubernetes rule cannot mix resource and non-resource URL fields")
        elif not self.resources:
            raise ValueError("a Kubernetes resource rule must contain resources")
        return self


class KubernetesNamespacesGrantScope(BaseModel):
    """One or more exact namespaces in which resource rules may apply."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[KubernetesGrantScopeKind.NAMESPACES] = KubernetesGrantScopeKind.NAMESPACES
    namespaces: frozenset[NON_EMPTY] = Field(min_length=1)

    @field_validator("namespaces")
    @classmethod
    def normalize_namespaces(cls, value: frozenset[str]) -> frozenset[str]:
        namespaces = _clean_values(value, "namespaces")
        if "*" in namespaces:
            raise ValueError("use all_namespaces instead of a namespace wildcard")
        return namespaces

    @field_serializer("namespaces")
    def serialize_namespaces(self, value: frozenset[str]) -> list[str]:
        return sorted(value)


class KubernetesAllNamespacesGrantScope(BaseModel):
    """All namespaced resources, excluding cluster-scoped resources."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[KubernetesGrantScopeKind.ALL_NAMESPACES] = KubernetesGrantScopeKind.ALL_NAMESPACES


class KubernetesClusterGrantScope(BaseModel):
    """Cluster-scoped resources only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[KubernetesGrantScopeKind.CLUSTER] = KubernetesGrantScopeKind.CLUSTER


class KubernetesNonResourceGrantScope(BaseModel):
    """Kubernetes non-resource URLs only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[KubernetesGrantScopeKind.NON_RESOURCE] = KubernetesGrantScopeKind.NON_RESOURCE


KubernetesGrantScope = Annotated[
    KubernetesNamespacesGrantScope
    | KubernetesAllNamespacesGrantScope
    | KubernetesClusterGrantScope
    | KubernetesNonResourceGrantScope,
    Field(discriminator="kind"),
]


def validate_grant_scope_rules(scope: KubernetesGrantScope, rules: Iterable[KubernetesRule]) -> None:
    """Reject scope/rule combinations that Kubernetes cannot interpret consistently."""

    non_resource = tuple(bool(rule.non_resource_urls) for rule in rules)
    if scope.kind is KubernetesGrantScopeKind.NON_RESOURCE:
        if not non_resource or not all(non_resource):
            raise ValueError("non_resource scope requires only non-resource URL rules")
    elif any(non_resource):
        raise ValueError(f"{scope.kind.value} scope requires only resource rules")


class KubernetesGrant(GrantEnvelope):
    """Durable grant returned by the service: the shared envelope plus scope/rules coverage.

    This domain now writes the envelope's end facts, but its readers still trust the stored
    ``status`` plus ``ended_at``: rows ended by a pre-facts replica during the roll carry no
    facts, so deriving status from them would fail open. The #4883 contract step (staged at
    `KubernetesGrantRow`) flips readers onto ``derive_status`` and retires both fields.
    """

    scope: KubernetesGrantScope
    rules: tuple[KubernetesRule, ...] = Field(min_length=1)
    status: GrantStatus
    ended_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_scope_rules(self) -> KubernetesGrant:
        validate_grant_scope_rules(self.scope, self.rules)
        return self

    @model_validator(mode="after")
    def validate_terminal_fields(self) -> KubernetesGrant:
        if self.status is GrantStatus.ACTIVE:
            if self.ended_at is not None or self.end_reason is not None:
                raise ValueError("an active grant cannot have terminal fields")
        elif self.ended_at is None or not self.end_reason or not self.end_reason.strip():
            raise ValueError("a terminal grant requires ended_at and a non-empty end_reason")
        return self

    @model_validator(mode="after")
    def validate_end_fact_coherence(self) -> KubernetesGrant:
        # One direction only: a pre-facts replica's end writes no fact, so a terminal status
        # without a fact stays valid until the #4883 contract step.
        if self.released_at is not None and self.status is not GrantStatus.RELEASED:
            raise ValueError("released_at requires a released stored status")
        if self.revoked_at is not None and self.status is not GrantStatus.REVOKED:
            raise ValueError("revoked_at requires a revoked stored status")
        return self


class KubernetesGrantSpec(BaseModel):
    """One exact scope/rule item requested by a grant-creation ToolCall."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: KubernetesGrantScope
    rules: tuple[KubernetesRule, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_scope_rules(self) -> KubernetesGrantSpec:
        validate_grant_scope_rules(self.scope, self.rules)
        return self


class KubernetesGrantDecision(BaseModel):
    """Result of matching a request against one Agent's currently active grants."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    grant_id: UUID | None = None
    expires_at: datetime.datetime | None = None
    reason: str | None = None
