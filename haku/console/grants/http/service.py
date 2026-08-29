"""Application service for owned, principal-scoped HTTP egress grants and request matching."""

from __future__ import annotations

import datetime
from collections.abc import Callable, Sequence
from typing import Protocol
from uuid import UUID

from haku.console.grants.envelope import (
    GrantNotFoundError,
    aware_now,
    validate_grant_window,
    validated_end_batch,
    validated_grant_set,
)
from haku.console.grants.http.models import (
    Grant,
    GrantDecision,
    GrantSpec,
    HttpMethod,
    HttpOrigin,
    HttpRequestAllowed,
    HttpRequestDenied,
)
from haku.console.grants.principal import GrantPrincipal, RequestPrincipal, grant_principal_applies_to


class GrantRepository(Protocol):
    async def create_many(
        self,
        *,
        owner_agent_id: UUID,
        grant_principal: GrantPrincipal,
        source_tool_call_id: str,
        grants: Sequence[GrantSpec],
        created_at: datetime.datetime,
        expires_at: datetime.datetime,
    ) -> tuple[Grant, ...]: ...

    async def list(
        self, *, principal: GrantPrincipal | None, now: datetime.datetime, include_inactive: bool = False
    ) -> tuple[Grant, ...]: ...

    async def get(self, *, owner_agent_id: UUID, grant_id: UUID) -> Grant: ...

    async def end(
        self, *, owner_agent_ids: frozenset[UUID], grant_id: UUID, reason: str | None, now: datetime.datetime
    ) -> Grant: ...

    async def list_for_request_principal(
        self, *, request_principal: RequestPrincipal, now: datetime.datetime, include_inactive: bool = False
    ) -> tuple[Grant, ...]: ...

    async def active_for_request_principal(
        self, *, request_principal: RequestPrincipal, now: datetime.datetime
    ) -> tuple[Grant, ...]: ...


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class GrantService:
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
        repository: GrantRepository,
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
        return aware_now(self._clock)

    async def create_grants(
        self,
        *,
        owner_agent_id: UUID,
        grant_principal: GrantPrincipal,
        source_tool_call_id: str,
        grants: Sequence[GrantSpec],
        expires_at: datetime.datetime,
    ) -> tuple[Grant, ...]:
        """Atomically create coverage grants with one source call and shared timestamps."""

        now = self._now()
        grants = validated_grant_set(source_tool_call_id, grants)
        validate_grant_window(now=now, expires_at=expires_at, max_lifetime=self._max_lifetime)
        return await self._repository.create_many(
            owner_agent_id=owner_agent_id,
            grant_principal=grant_principal,
            source_tool_call_id=source_tool_call_id,
            grants=grants,
            created_at=now,
            expires_at=expires_at,
        )

    async def list(
        self, *, principal: GrantPrincipal | None = None, include_inactive: bool = False
    ) -> tuple[Grant, ...]:
        """List all grants, or the grants declared for one exact principal."""

        return await self._repository.list(principal=principal, now=self._now(), include_inactive=include_inactive)

    async def list_applicable_grants(
        self, *, request_principal: RequestPrincipal, include_inactive: bool = False
    ) -> tuple[Grant, ...]:
        """List only grants this authenticated request principal may exercise."""

        return await self._repository.list_for_request_principal(
            request_principal=request_principal, now=self._now(), include_inactive=include_inactive
        )

    async def get_grant(self, *, owner_agent_id: UUID, grant_id: UUID) -> Grant:
        return await self._repository.get(owner_agent_id=owner_agent_id, grant_id=grant_id)

    async def get_applicable_grant(self, *, request_principal: RequestPrincipal, grant_id: UUID) -> Grant:
        grant = await self.get_grant(owner_agent_id=request_principal.agent_id, grant_id=grant_id)
        if not grant_principal_applies_to(grant.principal, request_principal):
            raise GrantNotFoundError(str(grant_id))
        return grant

    async def end_grants(
        self, *, owner_agent_id: UUID, grant_ids: Sequence[UUID], reason: str | None = None
    ) -> tuple[Grant, ...]:
        """End a bounded list sequentially, retaining every durable grant ID.

        This is deliberately not an atomic database operation. If a later end fails, earlier ends
        remain effective and visible; callers can reconcile with ``list_grants``.
        """

        grant_ids, reason = validated_end_batch(grant_ids, reason)
        now = self._now()
        return tuple(
            [
                await self._repository.end(
                    owner_agent_ids=frozenset({owner_agent_id}), grant_id=grant_id, reason=reason, now=now
                )
                for grant_id in grant_ids
            ]
        )

    async def end_applicable_grants(
        self, *, request_principal: RequestPrincipal, grant_ids: Sequence[UUID], reason: str | None = None
    ) -> tuple[Grant, ...]:
        for grant_id in grant_ids:
            await self.get_applicable_grant(request_principal=request_principal, grant_id=grant_id)
        return await self.end_grants(owner_agent_id=request_principal.agent_id, grant_ids=grant_ids, reason=reason)

    async def end_grant(self, *, owner_agent_ids: frozenset[UUID], grant_id: UUID, reason: str | None) -> Grant:
        if not owner_agent_ids:
            raise ValueError("owner_agent_ids must not be empty")
        return await self._repository.end(
            owner_agent_ids=owner_agent_ids, grant_id=grant_id, reason=reason, now=self._now()
        )

    @staticmethod
    def _allowed(matching: Sequence[Grant]) -> HttpRequestAllowed:
        """Bound the admission by the earliest matching expiry; report every named credential."""
        grant = min(matching, key=lambda item: item.expires_at)
        return HttpRequestAllowed(
            grant_id=grant.grant_id,
            expires_at=grant.expires_at,
            credential_handles=frozenset(
                match.spec.credential_handle for match in matching if match.spec.credential_handle is not None
            ),
        )

    async def match_request(
        self,
        *,
        request_principal: RequestPrincipal,
        method: HttpMethod,
        origin: HttpOrigin,
        path: str,
        require_prohibited_address_allowance: bool = False,
    ) -> GrantDecision:
        """Match one request against active grants and return the earliest expiry bound.

        ``require_prohibited_address_allowance`` is a grant-selection constraint, not an address
        check: this domain resolves nothing. The decide oracle owns the resolved answer and sets it
        when that answer is entirely prohibited (`decide_service`), so only grants whose spec carries
        ``allow_prohibited_address`` are eligible — an unflagged grant cannot admit an internal
        destination.
        """

        if not path.startswith("/"):
            raise ValueError("path must be the request URL's absolute path, starting with '/'")
        matching = [
            grant
            for grant in await self._repository.active_for_request_principal(
                request_principal=request_principal, now=self._now()
            )
            if grant.spec.origin == origin
            and grant.spec.coverage.covers(method=method, path=path)
            and (not require_prohibited_address_allowance or grant.spec.allow_prohibited_address)
        ]
        if not matching:
            return HttpRequestDenied(reason="no active HTTP grant covers the request")
        return self._allowed(matching)

    async def match_tunnel(
        self,
        *,
        request_principal: RequestPrincipal,
        origin: HttpOrigin,
        require_prohibited_address_allowance: bool = False,
    ) -> GrantDecision:
        """Match a CONNECT tunnel, which has no inner request yet: any active grant at the exact
        origin admits it, and method/path coverage binds each later decrypted request through
        :meth:`match_request` instead (#4884's CONNECT scoping ruling).

        ``require_prohibited_address_allowance`` filters to ``allow_prohibited_address`` grants, as
        :meth:`match_request` documents — the decide oracle sets it for a fully-internal resolution.
        """

        matching = [
            grant
            for grant in await self._repository.active_for_request_principal(
                request_principal=request_principal, now=self._now()
            )
            if grant.spec.origin == origin
            and (not require_prohibited_address_allowance or grant.spec.allow_prohibited_address)
        ]
        if not matching:
            return HttpRequestDenied(reason="no active HTTP grant covers the origin")
        return self._allowed(matching)
