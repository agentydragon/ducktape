"""Application service for owned, principal-scoped HTTP egress grants and request matching."""

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
    HttpGrantSpec,
    HttpMethod,
    HttpOrigin,
    HttpRequestAllowed,
    HttpRequestDenied,
)


class HttpGrantRepository(Protocol):
    async def create_many(
        self,
        *,
        owner_agent_id: UUID,
        grant_principal: GrantPrincipal,
        source_tool_call_id: str,
        grants: Sequence[HttpGrantSpec],
        created_at: datetime.datetime,
        expires_at: datetime.datetime,
    ) -> tuple[HttpGrant, ...]: ...

    async def list(
        self, *, owner_agent_id: UUID, now: datetime.datetime, include_terminal: bool = True
    ) -> tuple[HttpGrant, ...]: ...

    async def get(self, *, owner_agent_id: UUID, grant_id: UUID, now: datetime.datetime) -> HttpGrant: ...

    async def release(
        self, *, owner_agent_id: UUID, grant_id: UUID, reason: str, now: datetime.datetime
    ) -> HttpGrant: ...

    async def revoke(
        self, *, owner_agent_id: UUID, grant_id: UUID, reason: str, now: datetime.datetime
    ) -> HttpGrant: ...

    async def list_for_request_principal(
        self, *, request_principal: RequestPrincipal, now: datetime.datetime, include_terminal: bool = True
    ) -> tuple[HttpGrant, ...]: ...

    async def active_for_request_principal(
        self, *, request_principal: RequestPrincipal, now: datetime.datetime
    ) -> tuple[HttpGrant, ...]: ...


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _validated_end_batch(grant_ids: Sequence[UUID], reason: str) -> tuple[tuple[UUID, ...], str]:
    grant_ids = tuple(grant_ids)
    if not grant_ids:
        raise ValueError("grant_ids must not be empty")
    if len(grant_ids) > 32:
        raise ValueError("at most 32 grants may be ended by one call")
    if len(set(grant_ids)) != len(grant_ids):
        raise ValueError("grant_ids must not contain duplicates")
    reason = reason.strip()
    if not reason:
        raise ValueError("grant end reason must not be empty")
    return grant_ids, reason


class HttpGrantService:
    """Own grant lifecycle separately from trusted applicability matching.

    No method reads a ContextVar, ambient request, global caller, or tool arguments to determine
    identity. Operator lifecycle methods require an explicit owner; Agent-facing lifecycle and
    authorization methods require a complete trusted ``RequestPrincipal``. ``match_request`` is
    the temporary-grant evaluator behind the Console decision endpoint: it answers one
    ``(request principal, method, origin, path)`` question, carries no request bodies or headers,
    and is independent of the proxy adapter executing the decision.
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

    def _now(self) -> datetime.datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("grant service clock must return a timezone-aware datetime")
        return now

    async def create_grants(
        self,
        *,
        owner_agent_id: UUID,
        grant_principal: GrantPrincipal,
        source_tool_call_id: str,
        grants: Sequence[HttpGrantSpec],
        expires_at: datetime.datetime,
    ) -> tuple[HttpGrant, ...]:
        """Atomically create coverage grants with one source call and shared timestamps."""

        now = self._now()
        if not source_tool_call_id:
            raise ValueError("source_tool_call_id must not be empty")
        grants = tuple(grants)
        if not grants:
            raise ValueError("grants must not be empty")
        if len(grants) > 32:
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
            grants=grants,
            created_at=now,
            expires_at=expires_at,
        )

    async def list_grants(self, *, owner_agent_id: UUID, include_terminal: bool = True) -> tuple[HttpGrant, ...]:
        return await self._repository.list(
            owner_agent_id=owner_agent_id, now=self._now(), include_terminal=include_terminal
        )

    async def list_applicable_grants(
        self, *, request_principal: RequestPrincipal, include_terminal: bool = True
    ) -> tuple[HttpGrant, ...]:
        """List only grants this authenticated request principal may exercise."""

        return await self._repository.list_for_request_principal(
            request_principal=request_principal, now=self._now(), include_terminal=include_terminal
        )

    async def get_grant(self, *, owner_agent_id: UUID, grant_id: UUID) -> HttpGrant:
        return await self._repository.get(owner_agent_id=owner_agent_id, grant_id=grant_id, now=self._now())

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

        grant_ids, reason = _validated_end_batch(grant_ids, reason)
        now = self._now()
        return tuple(
            [
                await self._repository.release(owner_agent_id=owner_agent_id, grant_id=grant_id, reason=reason, now=now)
                for grant_id in grant_ids
            ]
        )

    async def release_applicable_grants(
        self, *, request_principal: RequestPrincipal, grant_ids: Sequence[UUID], reason: str = "released"
    ) -> tuple[HttpGrant, ...]:
        for grant_id in grant_ids:
            await self.get_applicable_grant(request_principal=request_principal, grant_id=grant_id)
        return await self.release_grants(owner_agent_id=request_principal.agent_id, grant_ids=grant_ids, reason=reason)

    async def revoke_grant(self, *, owner_agent_id: UUID, grant_id: UUID, reason: str) -> HttpGrant:
        return await self._repository.revoke(
            owner_agent_id=owner_agent_id, grant_id=grant_id, reason=reason, now=self._now()
        )

    async def revoke_grants(
        self, *, owner_agent_id: UUID, grant_ids: Sequence[UUID], reason: str
    ) -> tuple[HttpGrant, ...]:
        """Revoke a bounded list sequentially; same non-atomicity contract as ``release_grants``."""

        grant_ids, reason = _validated_end_batch(grant_ids, reason)
        now = self._now()
        return tuple(
            [
                await self._repository.revoke(owner_agent_id=owner_agent_id, grant_id=grant_id, reason=reason, now=now)
                for grant_id in grant_ids
            ]
        )

    async def match_request(
        self, *, request_principal: RequestPrincipal, method: HttpMethod, origin: HttpOrigin, path: str
    ) -> HttpGrantDecision:
        """Match one request against active grants and return the earliest expiry bound."""

        if not path.startswith("/"):
            raise ValueError("path must be the request URL's absolute path, starting with '/'")
        matching = [
            grant
            for grant in await self._repository.active_for_request_principal(
                request_principal=request_principal, now=self._now()
            )
            if grant.spec.origin == origin and grant.spec.covers(method=method, path=path)
        ]
        if not matching:
            return HttpRequestDenied(reason="no active HTTP grant covers the request")
        grant = min(matching, key=lambda item: item.expires_at)
        return HttpRequestAllowed(grant_id=grant.grant_id, expires_at=grant.expires_at)

    async def match_tunnel(self, *, request_principal: RequestPrincipal, origin: HttpOrigin) -> HttpGrantDecision:
        """Match a CONNECT tunnel, which has no inner request yet: any active grant at the exact
        origin admits it, and method/path coverage binds each later decrypted request through
        :meth:`match_request` instead (#4884's CONNECT scoping ruling)."""

        matching = [
            grant
            for grant in await self._repository.active_for_request_principal(
                request_principal=request_principal, now=self._now()
            )
            if grant.spec.origin == origin
        ]
        if not matching:
            return HttpRequestDenied(reason="no active HTTP grant covers the origin")
        grant = min(matching, key=lambda item: item.expires_at)
        return HttpRequestAllowed(grant_id=grant.grant_id, expires_at=grant.expires_at)
