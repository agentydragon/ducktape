"""Resolved policy bindings shared by the app policy views."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict

from agentplane.action_service.models import PolicySource
from agentplane.action_service.policies.registry import Policy


class ResolvedPolicySet(BaseModel):
    """One valid set a binding names, with its auto-approve policies in evaluation order: a
    `PolicySet`'s `action_auto_approve` rules, each at its position in the set's rules."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    namespace: str
    name: str
    generation: int
    source: PolicySource
    policies: tuple[tuple[int, Policy], ...]


class BindingKind(StrEnum):
    POLICY_BINDING = "PolicyBinding"


class ResolvedBinding(BaseModel):
    """One `PolicyBinding` naming the caller, with the valid sets it names that exist."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: BindingKind
    namespace: str
    name: str
    resource_version: str
    expires_at: AwareDatetime | None
    policy_sets: tuple[ResolvedPolicySet, ...]
