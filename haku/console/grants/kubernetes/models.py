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
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    computed_field,
    field_serializer,
    field_validator,
    model_validator,
)

from haku.console.grants.envelope import NON_EMPTY, GrantEnvelope, GrantStatus, derive_status


class GrantScopeKind(StrEnum):
    NAMESPACES = "namespaces"
    ALL_NAMESPACES = "all_namespaces"
    CLUSTER = "cluster"
    NON_RESOURCE = "non_resource"


def _clean_values(value: Iterable[str], field_name: str, *, allow_empty: bool = False) -> frozenset[str]:
    values = tuple(item.strip() for item in value)
    if not allow_empty and any(not item for item in values):
        raise ValueError(f"{field_name} must not contain empty values")
    return frozenset(values)


class Rule(BaseModel):
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
    def validate_kind(self) -> Rule:
        if self.non_resource_urls:
            if self.api_groups or self.resources or self.resource_names:
                raise ValueError("a Kubernetes rule cannot mix resource and non-resource URL fields")
        elif not self.resources:
            raise ValueError("a Kubernetes resource rule must contain resources")
        return self


class NamespacesGrantScope(BaseModel):
    """One or more exact namespaces in which resource rules may apply."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[GrantScopeKind.NAMESPACES] = GrantScopeKind.NAMESPACES
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


class AllNamespacesGrantScope(BaseModel):
    """All namespaced resources, excluding cluster-scoped resources."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[GrantScopeKind.ALL_NAMESPACES] = GrantScopeKind.ALL_NAMESPACES


class ClusterGrantScope(BaseModel):
    """Cluster-scoped resources only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[GrantScopeKind.CLUSTER] = GrantScopeKind.CLUSTER


class NonResourceGrantScope(BaseModel):
    """Kubernetes non-resource URLs only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[GrantScopeKind.NON_RESOURCE] = GrantScopeKind.NON_RESOURCE


GrantScope = Annotated[
    NamespacesGrantScope | AllNamespacesGrantScope | ClusterGrantScope | NonResourceGrantScope,
    Field(discriminator="kind"),
]


def validate_grant_scope_rules(scope: GrantScope, rules: Iterable[Rule]) -> None:
    """Reject scope/rule combinations that Kubernetes cannot interpret consistently."""

    non_resource = tuple(bool(rule.non_resource_urls) for rule in rules)
    if scope.kind is GrantScopeKind.NON_RESOURCE:
        if not non_resource or not all(non_resource):
            raise ValueError("non_resource scope requires only non-resource URL rules")
    elif any(non_resource):
        raise ValueError(f"{scope.kind.value} scope requires only resource rules")


class Grant(GrantEnvelope):
    """Durable grant returned by the service: the shared envelope plus scope/rules coverage.

    ``status`` is computed from the envelope's recorded end facts and the clock at access
    time (`haku.console.grants.envelope.derive_status`) — never stored and never a field, so
    it cannot disagree with the facts.
    """

    scope: GrantScope
    rules: tuple[Rule, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_scope_rules(self) -> Grant:
        validate_grant_scope_rules(self.scope, self.rules)
        return self

    # The ignore is pydantic's documented mypy accommodation for computed_field-on-property
    # (mypy's prop-decorator limitation), not a silenced finding.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def status(self) -> GrantStatus:
        return derive_status(
            ended_at=self.ended_at, expires_at=self.expires_at, now=datetime.datetime.now(datetime.UTC)
        )


class GrantSpec(BaseModel):
    """One exact scope/rule item requested by a grant-creation ToolCall."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: GrantScope
    rules: tuple[Rule, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_scope_rules(self) -> GrantSpec:
        validate_grant_scope_rules(self.scope, self.rules)
        return self


class GrantDecision(BaseModel):
    """Result of matching a request against one Agent's currently active grants."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    grant_id: UUID | None = None
    expires_at: datetime.datetime | None = None
    reason: str | None = None
