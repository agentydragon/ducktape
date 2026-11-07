"""Typed values and service contract for operator Agent management."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from haku.console.identity.agent import AgentStatus, CredentialBindingStatus, CredentialKind


class AccessProfileUnavailableError(ValueError):
    pass


class AgentNotFoundError(LookupError):
    pass


class AgentAccessProfileManagedByDeploymentError(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class OperatorAgent:
    agent_id: UUID
    display_name: str
    status: AgentStatus
    credential_kind: CredentialKind
    credential_status: CredentialBindingStatus
    created_at: datetime.datetime
    activated_at: datetime.datetime | None
    last_seen_at: datetime.datetime | None
    # NULL is accepted only for Agents created before durable policy assignment existed.
    access_profile_id: str | None


class AgentManagementService(Protocol):
    def available_access_profiles(self) -> tuple[str, ...]: ...

    async def list_agents(self, *, operator_id: UUID) -> tuple[OperatorAgent, ...]: ...

    async def set_access_profile(
        self, *, operator_id: UUID, agent_id: UUID, access_profile_id: str
    ) -> OperatorAgent: ...
