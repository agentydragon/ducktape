"""Canonical static and exact-session Haku Agent bearer authority."""

from __future__ import annotations

import datetime
import hmac
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.agents.authorization import PostgresAgentAuthority, StaticAgentRejectedError, fingerprint_static_token
from haku.console.chat_models import SessionStatus
from haku.console.database_schema import Conversation, Session
from haku.console.mcp_auth.fastmcp_adapter import AgentGrantAuthorityUnavailableError
from haku.console.session.launch_identity import LaunchAgentRejectedError
from haku.console.tool_call_actor import AgentActor

_STATIC_BINDING_CREDENTIAL_PREFIX = "haku-static-binding:"
_CHAT_SESSION_CREDENTIAL_PREFIX = "haku-chat-session:"
_AGENT_SESSION_STATUSES = (SessionStatus.READY, SessionStatus.RESPONDING)


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


class _SessionAgentBearerSource:
    """Resolve a live Console session bearer through the canonical launch authority."""

    def __init__(self, authority: PostgresAgentAuthority, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._authority = authority
        self._sessions = sessions

    async def resolve(self, token: str, *, record_seen: bool = False) -> ResolvedAgentBearer | None:
        del record_seen
        try:
            fingerprint = fingerprint_static_token(token)
        except ValueError:
            return None
        now = datetime.datetime.now(datetime.UTC)
        try:
            async with self._sessions.begin() as db:
                row = (
                    await db.execute(
                        select(
                            Session.session_id,
                            Session.operator_id,
                            Session.agent_binding_id,
                            Conversation.agent_id,
                            Conversation.access_profile_id,
                        )
                        .join(Conversation, Conversation.conversation_id == Session.conversation_id)
                        .where(
                            Session.bridge_token_fingerprint == fingerprint,
                            Session.status.in_(_AGENT_SESSION_STATUSES),
                            Session.bridge_connected_at.is_not(None),
                            Session.lease_expires_at.is_not(None),
                            Session.lease_expires_at > now,
                        )
                    )
                ).one_or_none()
                if row is None or row.agent_binding_id is None or row.agent_id is None or row.access_profile_id is None:
                    return None
                active = await self._authority.launch_authorization(
                    db,
                    operator_id=row.operator_id,
                    agent_id=row.agent_id,
                    access_profile_id=row.access_profile_id,
                    binding_id=row.agent_binding_id,
                )
                return ResolvedAgentBearer(
                    actor=AgentActor(
                        agent_id=active.agent_id,
                        operator_id=active.operator_id,
                        binding_id=active.binding_id,
                        access_profile_id=row.access_profile_id,
                        session_id=row.session_id,
                    ),
                    credential_id=f"{_CHAT_SESSION_CREDENTIAL_PREFIX}{row.session_id}",
                )
        except (LaunchAgentRejectedError, ValueError):
            return None
        except (AgentGrantAuthorityUnavailableError, SQLAlchemyError):
            raise AgentGrantAuthorityUnavailableError from None


class AgentBearerAuthority:
    """Authenticate raw static/session bearers against current durable Agent authority."""

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
    *,
    agent_authority: PostgresAgentAuthority,
    static_credentials: StaticAgentCredentialRegistry,
    session_tokens: async_sessionmaker[AsyncSession] | None = None,
) -> AgentBearerAuthority:
    """Compose configured static and exact-session bearer authorities."""

    sources: list[Callable[..., Awaitable[ResolvedAgentBearer | None]]] = []
    if static_credentials.fingerprints:
        sources.append(_StaticAgentBearerSource(agent_authority, static_credentials).resolve)
    if session_tokens is not None:
        sources.append(_SessionAgentBearerSource(agent_authority, session_tokens).resolve)
    return AgentBearerAuthority(tuple(sources))
