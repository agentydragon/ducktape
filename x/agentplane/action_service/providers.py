"""The DecisionProvider seam: what a provider is given at admission, and what it answers with."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from x.agentplane.action_service.catalog import ActionIdentity
from x.agentplane.action_service.models import ProviderOutcome, SandboxCaller, ServiceAccountCaller
from x.agentplane.action_service.policy_resources import ActionPolicyBinding, ActionPolicySet


class ResolvedBinding(BaseModel):
    """One unexpired, valid binding naming the caller, with the valid sets it names that exist."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    binding: ActionPolicyBinding
    policy_sets: tuple[ActionPolicySet, ...]


class DecisionContext(BaseModel):
    """Trusted evaluation input for a DecisionProvider.

    Deliberately excludes `origin`/`correlation`: the caller is the authenticated Sandbox or the
    Connection's ServiceAccount, and the bindings are the ones the policy informer held for it at
    admission — never anything the request claimed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    action: ActionIdentity
    arguments: dict[str, JsonValue]
    caller: SandboxCaller | ServiceAccountCaller
    bindings: tuple[ResolvedBinding, ...] = Field(
        description="Empty until the informer has synced, and for a caller nothing names: human-only."
    )


class DecisionProvider(Protocol):
    """A synchronous non-human policy adapter; its outcome is authoritative within the provider."""

    @property
    def name(self) -> str: ...

    async def decide(self, context: DecisionContext) -> ProviderOutcome: ...
