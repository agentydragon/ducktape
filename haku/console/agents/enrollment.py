"""Typed application boundary for the browser half of Agent enrollment."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from haku.console.agents.models import AgentStatus, CredentialBindingStatus, CredentialKind


@dataclass(frozen=True, slots=True)
class EnrollmentBrowserSession:
    operator_id: UUID
    identity_id: UUID
    browser_session_id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class ReconnectableAgent:
    agent_id: UUID
    display_name: str


@dataclass(frozen=True, slots=True)
class EnrollmentPage:
    client_software: str
    redirect_host: str
    requested_scopes: tuple[str, ...]
    suggested_agent_name: str
    reconnectable_agents: tuple[ReconnectableAgent, ...]
    form_token: str
    upstream_authorization_url: str


@dataclass(frozen=True, slots=True)
class CreateAgentDecision:
    form_token: str
    display_name: str


@dataclass(frozen=True, slots=True)
class ReconnectAgentDecision:
    form_token: str
    agent_id: UUID


@dataclass(frozen=True, slots=True)
class DenyEnrollmentDecision:
    form_token: str


type EnrollmentDecision = CreateAgentDecision | ReconnectAgentDecision | DenyEnrollmentDecision


@dataclass(frozen=True, slots=True)
class EnrollmentAllowed:
    upstream_authorization_url: str


@dataclass(frozen=True, slots=True)
class EnrollmentDenied:
    pass


type EnrollmentDecisionResult = EnrollmentAllowed | EnrollmentDenied


class EnrollmentInteractionNotFoundError(LookupError):
    pass


class EnrollmentInteractionExpiredError(Exception):
    pass


class EnrollmentBrowserBindingError(PermissionError):
    pass


class EnrollmentDecisionConflictError(RuntimeError):
    pass


class AgentNameUnavailableError(ValueError):
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


class AgentEnrollmentService(Protocol):
    async def list_agents(self, *, operator_id: UUID) -> tuple[OperatorAgent, ...]: ...

    async def open_interaction(
        self,
        *,
        interaction_id: UUID,
        browser_nonce: str | None,
        interaction_cookie: str | None,
        browser: EnrollmentBrowserSession,
    ) -> EnrollmentPage: ...

    async def decide(
        self,
        *,
        interaction_id: UUID,
        browser: EnrollmentBrowserSession,
        interaction_cookie: str,
        decision: EnrollmentDecision,
    ) -> EnrollmentDecisionResult: ...
