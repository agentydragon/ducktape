"""Canonical static Haku Agent bearer authority."""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from haku.console.identity.authorization import (
    PostgresAgentAuthority,
    StaticAgentRejectedError,
    fingerprint_static_token,
)
from haku.console.identity.fastmcp_adapter import AgentGrantAuthorityUnavailableError
from haku.console.tool_call_actor import AgentActor

_STATIC_BINDING_CREDENTIAL_PREFIX = "haku-static-binding:"


@dataclass(frozen=True, slots=True)
class StaticAgentCredentialRegistry:
    """Configured static credential fingerprints, without retaining raw bearers."""

    fingerprints: tuple[bytes, ...]

    def configured_fingerprint(self, token: str) -> bytes | None:
        try:
            presented = fingerprint_static_token(token)
        except ValueError:
            return None
        return next(
            (fingerprint for fingerprint in self.fingerprints if hmac.compare_digest(presented, fingerprint)), None
        )


@dataclass(frozen=True, slots=True)
class ResolvedAgentBearer:
    """Current Agent authority plus a stable identifier for the credential generation."""

    actor: AgentActor
    credential_id: str


class _StaticAgentBearerSource:
    def __init__(self, authority: PostgresAgentAuthority, credentials: StaticAgentCredentialRegistry) -> None:
        self._authority = authority
        self._credentials = credentials

    async def resolve(self, token: str, *, record_seen: bool = False) -> ResolvedAgentBearer | None:
        fingerprint = self._credentials.configured_fingerprint(token)
        if fingerprint is None:
            return None
        authorization = await self._authority.static_authorization_for_fingerprint(
            fingerprint=fingerprint, record_seen=record_seen
        )
        return ResolvedAgentBearer(
            actor=AgentActor(
                agent_id=authorization.agent_id,
                operator_id=authorization.operator_id,
                binding_id=authorization.binding_id,
                access_profile_id=authorization.access_profile_id,
            ),
            credential_id=f"{_STATIC_BINDING_CREDENTIAL_PREFIX}{authorization.binding_id}",
        )


class AgentBearerAuthority:
    """Authenticate raw static credentials against current durable Agent authority."""

    def __init__(self, sources: tuple[Callable[..., Awaitable[ResolvedAgentBearer | None]], ...]) -> None:
        self._sources = sources

    @property
    def configured(self) -> bool:
        return bool(self._sources)

    async def resolve(self, token: str, *, record_seen: bool = False) -> ResolvedAgentBearer | None:
        unavailable = False
        for source in self._sources:
            try:
                resolved = await source(token, record_seen=record_seen)
            except AgentGrantAuthorityUnavailableError:
                unavailable = True
                continue
            except (StaticAgentRejectedError, ValueError):
                continue
            if resolved is not None:
                return resolved
        if unavailable:
            raise AgentGrantAuthorityUnavailableError
        return None

    async def authenticate(self, token: str) -> AgentActor | None:
        resolved = await self.resolve(token)
        return None if resolved is None else resolved.actor


def build_agent_bearer_authority(
    *, agent_authority: PostgresAgentAuthority, static_credentials: StaticAgentCredentialRegistry
) -> AgentBearerAuthority:
    """Compose configured static-credential bearer authorities."""

    sources: list[Callable[..., Awaitable[ResolvedAgentBearer | None]]] = []
    if static_credentials.fingerprints:
        sources.append(_StaticAgentBearerSource(agent_authority, static_credentials).resolve)
    return AgentBearerAuthority(tuple(sources))
