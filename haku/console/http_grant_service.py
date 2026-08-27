"""Application service for owned, principal-scoped HTTP egress grants and origin matching."""

from __future__ import annotations

import datetime
from collections.abc import Callable, Sequence
from typing import Protocol
from uuid import UUID

from haku.console.grant_principal import GrantPrincipal, RequestPrincipal, grant_principal_applies_to
from haku.console.http_grant_models import (
    HttpGrant,
    HttpGrantDecision,
    HttpGrantNotFoundError,
    HttpGrantStatus,
    HttpOrigin,
)


class HttpGrantRepository(Protocol):
    async def create_many(
        self,
        *,
        owner_agent_id: UUID,
        grant_principal: GrantPrincipal,
        source_tool_call_id: str,
        origins: Sequence[HttpOrigin],
        created_at: datetime.datetime,
        expires_at: datetime.datetime,
    ) -> tuple[HttpGrant, ...]: ...

    async def list(self, *, owner_agent_id: UUID, include_terminal: bool = True) -> tuple[HttpGrant, ...]: ...

    async def get(self, *, owner_agent_id: UUID, grant_id: UUID) -> HttpGrant: ...

    async def release(
        self, *, owner_agent_id: UUID, grant_id: UUID, reason: str, ended_at: datetime.datetime
    ) -> HttpGrant: ...

    async def revoke(
        self, *, owner_agent_id: UUID, grant_id: UUID, reason: str, ended_at: datetime.datetime
    ) -> HttpGrant: ...

    async def revoke_source(
        self, *, owner_agent_id: UUID, source_tool_call_id: str, reason: str, ended_at: datetime.datetime
    ) -> tuple[HttpGrant, ...]: ...

    async def expire(self, *, now: datetime.datetime, owner_agent_id: UUID | None = None) -> int: ...

    async def list_for_request_principal(
        self, *, request_principal: RequestPrincipal, include_terminal: bool = True
    ) -> tuple[HttpGrant, ...]: ...

    async def active_for_request_principal(
        self, *, request_principal: RequestPrincipal, now: datetime.datetime
    ) -> tuple[HttpGrant, ...]: ...


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class HttpGrantService:
    """Own grant lifecycle separately from trusted applicability matching.

    No method reads a ContextVar, ambient request, global caller, or tool arguments to determine
    identity. Operator lifecycle methods require an explicit owner; Agent-facing lifecycle and
    authorization methods require a complete trusted ``RequestPrincipal``. ``match_request`` is
    the temporary-grant evaluator behind the Console decision endpoint: it answers one
    ``(request principal, exact origin)`` question, carries no request content, and is
    independent of the proxy adapter executing the decision.
    """

    def __init__(
        self,
        repository: HttpGrantRepository,
        *,
        max_lifetime: datetime.timedelta,
        clock: Callable[[], datetime.datetime] = _utcnow,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._max_lifetime = max_lifetime
        if max_lifetime <= datetime.timedelta():
            raise ValueError("max_lifetime must be positive")

    async def create_grants(
        self,
        *,
        owner_agent_id: UUID,
        grant_principal: GrantPrincipal,
        source_tool_call_id: str,
        origins: Sequence[HttpOrigin],
        expires_at: datetime.datetime,
    ) -> tuple[HttpGrant, ...]:
        """Atomically create exact-origin grants with one source call and shared timestamps."""

        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("grant service clock must return a timezone-aware datetime")
        if not source_tool_call_id:
            raise ValueError("source_tool_call_id must not be empty")
        origins = tuple(origins)
        if not origins:
            raise ValueError("origins must not be empty")
        if len(origins) > 32:
            raise ValueError("at most 32 grants may be created by one ToolCall")
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise ValueError("expires_at must be timezone-aware")
        if expires_at <= now:
            raise ValueError("expires_at must be in the future")
        if expires_at > now + self._max_lifetime:
            raise ValueError("expires_at exceeds the configured grant lifetime")
        return await self._repository.create_many(
            owner_agent_id=owner_agent_id,
            grant_principal=grant_principal,
            source_tool_call_id=source_tool_call_id,
            origins=origins,
            created_at=now,
            expires_at=expires_at,
        )

    async def list_grants(self, *, owner_agent_id: UUID, include_terminal: bool = True) -> tuple[HttpGrant, ...]:
        await self.expire_grants(owner_agent_id=owner_agent_id)
        return await self._repository.list(owner_agent_id=owner_agent_id, include_terminal=include_terminal)

    async def list_applicable_grants(
        self, *, request_principal: RequestPrincipal, include_terminal: bool = True
    ) -> tuple[HttpGrant, ...]:
        """List only grants this authenticated request principal may exercise."""

        await self.expire_grants(owner_agent_id=request_principal.agent_id)
        return await self._repository.list_for_request_principal(
            request_principal=request_principal, include_terminal=include_terminal
        )

    async def get_grant(self, *, owner_agent_id: UUID, grant_id: UUID) -> HttpGrant:
        grant = await self._repository.get(owner_agent_id=owner_agent_id, grant_id=grant_id)
        now = self._clock()
        if grant.status is HttpGrantStatus.ACTIVE and grant.expires_at <= now:
            await self._repository.expire(owner_agent_id=owner_agent_id, now=now)
            grant = await self._repository.get(owner_agent_id=owner_agent_id, grant_id=grant_id)
        return grant

    async def get_applicable_grant(self, *, request_principal: RequestPrincipal, grant_id: UUID) -> HttpGrant:
        grant = await self.get_grant(owner_agent_id=request_principal.agent_id, grant_id=grant_id)
        if not grant_principal_applies_to(grant.principal, request_principal):
            raise HttpGrantNotFoundError(str(grant_id))
        return grant

    async def release_grants(
        self, *, owner_agent_id: UUID, grant_ids: Sequence[UUID], reason: str = "released"
    ) -> tuple[HttpGrant, ...]:
        """Release a bounded list sequentially, retaining every durable grant ID.

        This is deliberately not an atomic database operation. If a later release fails, earlier
        releases remain effective and visible; callers can reconcile with ``list_grants``.
        """

        grant_ids = tuple(grant_ids)
        if not grant_ids:
            raise ValueError("grant_ids must not be empty")
        if len(grant_ids) > 32:
            raise ValueError("at most 32 grants may be released by one ToolCall")
        if len(set(grant_ids)) != len(grant_ids):
            raise ValueError("grant_ids must not contain duplicates")
        reason = reason.strip()
        if not reason:
            raise ValueError("grant end reason must not be empty")
        ended_at = self._clock()
        released = [
            await self._repository.release(
                owner_agent_id=owner_agent_id, grant_id=grant_id, reason=reason, ended_at=ended_at
            )
            for grant_id in grant_ids
        ]
        return tuple(released)

    async def release_applicable_grants(
        self, *, request_principal: RequestPrincipal, grant_ids: Sequence[UUID], reason: str = "released"
    ) -> tuple[HttpGrant, ...]:
        for grant_id in grant_ids:
            await self.get_applicable_grant(request_principal=request_principal, grant_id=grant_id)
        return await self.release_grants(owner_agent_id=request_principal.agent_id, grant_ids=grant_ids, reason=reason)

    async def revoke_grant(self, *, owner_agent_id: UUID, grant_id: UUID, reason: str) -> HttpGrant:
        return await self._repository.revoke(
            owner_agent_id=owner_agent_id, grant_id=grant_id, reason=reason, ended_at=self._clock()
        )

    async def revoke_grant_set(
        self, *, owner_agent_id: UUID, source_tool_call_id: str, reason: str
    ) -> tuple[HttpGrant, ...]:
        """Revoke the durable grant set sharing one approval source ToolCall."""

        if not source_tool_call_id:
            raise ValueError("source_tool_call_id must not be empty")
        return await self._repository.revoke_source(
            owner_agent_id=owner_agent_id,
            source_tool_call_id=source_tool_call_id,
            reason=reason,
            ended_at=self._clock(),
        )

    async def expire_grants(self, *, owner_agent_id: UUID | None = None) -> int:
        return await self._repository.expire(owner_agent_id=owner_agent_id, now=self._clock())

    async def match_request(self, *, request_principal: RequestPrincipal, origin: HttpOrigin) -> HttpGrantDecision:
        """Match one exact origin against active grants and return the earliest expiry bound."""

        now = self._clock()
        matching = [
            grant
            for grant in await self._repository.active_for_request_principal(
                request_principal=request_principal, now=now
            )
            if grant.origin == origin
        ]
        if matching:
            grant = min(matching, key=lambda item: item.expires_at)
            return HttpGrantDecision(
                allowed=True,
                grant_id=grant.grant_id,
                expires_at=grant.expires_at,
                reason="active HTTP grant covers the origin",
            )
        return HttpGrantDecision(allowed=False, reason="no active HTTP grant covers the origin")
