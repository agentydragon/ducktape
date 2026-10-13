"""What a DecisionProvider is given at admission."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

from agentplane.action_service.catalog import ActionIdentity
from agentplane.action_service.models import PolicySource
from agentplane.action_service.policies.registry import Policy
from agentplane.subjects import ServiceAccountRef


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


class DecisionContext(BaseModel):
    """Trusted evaluation input for a DecisionProvider.

    The caller is the authenticated Sandbox or the Connection's ServiceAccount, and the bindings
    are the ones the policy informer held for it at admission — never anything the request claimed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    action: ActionIdentity
    arguments: dict[str, JsonValue]
    caller: ServiceAccountRef
    bindings: tuple[ResolvedBinding, ...] = Field(
        description="Empty while the informer's copy is not in sync, and for a caller nothing names: human-only."
    )
