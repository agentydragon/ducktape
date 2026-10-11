"""What a DecisionProvider is given at admission."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from agentplane.action_service.catalog import ActionIdentity
from agentplane.action_service.policies.resources import ActionPolicyBinding, ActionPolicySet
from agentplane.subjects import ServiceAccountRef


class ResolvedBinding(BaseModel):
    """One unexpired, valid binding naming the caller, with the valid sets it names that exist."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    binding: ActionPolicyBinding
    policy_sets: tuple[ActionPolicySet, ...]


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
