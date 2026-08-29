"""Application service for owned, principal-scoped Kubernetes grants and rule matching."""

from __future__ import annotations

import datetime
from collections.abc import Callable, Sequence, Set
from typing import Protocol
from uuid import UUID

from haku.console.grants.envelope import (
    GrantNotFoundError,
    aware_now,
    validate_grant_window,
    validated_end_batch,
    validated_grant_set,
)
from haku.console.grants.kubernetes.models import (
    Grant,
    GrantDecision,
    GrantScope,
    GrantScopeKind,
    GrantSpec,
    NamespacesGrantScope,
    Rule,
    validate_grant_scope_rules,
)
from haku.console.grants.principal import GrantPrincipal, RequestPrincipal, grant_principal_applies_to


class GrantRepository(Protocol):
    async def create(
        self,
        *,
        owner_agent_id: UUID,
        grant_principal: GrantPrincipal,
        source_tool_call_id: str,
        scope: GrantScope,
        rules: Sequence[Rule],
        created_at: datetime.datetime,
        expires_at: datetime.datetime,
    ) -> Grant: ...

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


def _wildcard_covers(granted: Set[str], requested: Set[str]) -> bool:
    """Whether a grant's values cover all values in the request.

    ``*`` is the only wildcard accepted here.  Empty requested values are treated as the
    canonical empty Kubernetes value (for example, the core API group), not as "all".
    """

    if "*" in granted:
        return True
    return requested <= granted


def _resource_names_cover(granted: Set[str], requested: Set[str]) -> bool:
    # Kubernetes RBAC's empty resourceNames means all names. A named request cannot be covered by
    # a grant listing only a different subset. Conversely, a list/watch request has no finite name
    # set and therefore requires an all-names grant.
    if not requested:
        return not granted
    return not granted or requested <= granted


def _non_resource_url_covers(granted: str, requested: str) -> bool:
    if granted in {"*", requested}:
        return True
    # Kubernetes supports a trailing wildcard for a URL path. Do not normalize paths or accept
    # arbitrary glob syntax: exact path and a single terminal /* are deliberately conservative.
    return granted.endswith("/*") and requested.startswith(granted[:-1])


def rule_covers(granted: Rule, requested: Rule) -> bool:
    """Return true only when one grant rule covers the entire required rule."""

    granted_is_non_resource = bool(granted.non_resource_urls)
    requested_is_non_resource = bool(requested.non_resource_urls)
    if granted_is_non_resource != requested_is_non_resource:
        return False
    if not _wildcard_covers(granted.verbs, requested.verbs):
        return False
    if requested_is_non_resource:
        return all(
            any(_non_resource_url_covers(granted_url, requested_url) for granted_url in granted.non_resource_urls)
            for requested_url in requested.non_resource_urls
        )
    return (
        _wildcard_covers(granted.api_groups, requested.api_groups)
        and _wildcard_covers(granted.resources, requested.resources)
        and _resource_names_cover(granted.resource_names, requested.resource_names)
    )


def rules_cover(granted: Sequence[Rule], required: Sequence[Rule]) -> bool:
    """Every required rule must be covered by one grant rule; no partial matches are accepted."""

    return bool(required) and all(any(rule_covers(candidate, needed) for candidate in granted) for needed in required)


def scope_covers(granted: GrantScope, required: GrantScope) -> bool:
    """Whether one grant scope covers the complete required request scope."""

    if required.kind is GrantScopeKind.NAMESPACES:
        if granted.kind is GrantScopeKind.ALL_NAMESPACES:
            return True
        return (
            isinstance(granted, NamespacesGrantScope)
            and isinstance(required, NamespacesGrantScope)
            and required.namespaces <= granted.namespaces
        )
    return granted.kind is required.kind


class GrantService:
    """Own grant lifecycle separately from trusted applicability matching.

    No method reads a ContextVar, ambient request, global caller, or tool arguments to determine
    identity. Operator lifecycle methods require an explicit owner; Agent-facing lifecycle and
    authorization methods require a complete trusted ``RequestPrincipal``.
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

    async def create_grant(
        self,
        *,
        owner_agent_id: UUID,
        grant_principal: GrantPrincipal,
        source_tool_call_id: str,
        scope: GrantScope,
        rules: Sequence[Rule],
        expires_at: datetime.datetime,
    ) -> Grant:
        """Create one owned, principal-scoped grant and retain source-call provenance."""

        rules = tuple(rules)
        if not rules:
            raise ValueError("rules must not be empty")
        grants = await self.create_grants(
            owner_agent_id=owner_agent_id,
            grant_principal=grant_principal,
            source_tool_call_id=source_tool_call_id,
            grants=(GrantSpec(scope=scope, rules=rules),),
            expires_at=expires_at,
        )
        return grants[0]

    async def create_grants(
        self,
        *,
        owner_agent_id: UUID,
        grant_principal: GrantPrincipal,
        source_tool_call_id: str,
        grants: Sequence[GrantSpec],
        expires_at: datetime.datetime,
    ) -> tuple[Grant, ...]:
        """Atomically create exact grants with one source call and shared timestamps."""

        now = self._now()
        grants = validated_grant_set(source_tool_call_id, grants)
        for grant in grants:
            validate_grant_scope_rules(grant.scope, grant.rules)
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
        ended = [
            await self._repository.end(
                owner_agent_ids=frozenset({owner_agent_id}), grant_id=grant_id, reason=reason, now=now
            )
            for grant_id in grant_ids
        ]
        return tuple(ended)

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

    async def match_request(
        self, *, request_principal: RequestPrincipal, required_scope: GrantScope, required_rules: Sequence[Rule]
    ) -> GrantDecision:
        """Match one request against active grants and return the earliest expiry bound."""

        now = self._now()
        required = tuple(required_rules)
        if not required:
            raise ValueError("required_rules must not be empty")
        validate_grant_scope_rules(required_scope, required)
        matching = [
            grant
            for grant in await self._repository.active_for_request_principal(
                request_principal=request_principal, now=now
            )
            if scope_covers(grant.scope, required_scope) and rules_cover(grant.rules, required)
        ]
        if matching:
            grant = min(matching, key=lambda item: item.expires_at)
            return GrantDecision(
                allowed=True,
                grant_id=grant.grant_id,
                expires_at=grant.expires_at,
                reason="active Kubernetes grant covers the request",
            )
        return GrantDecision(allowed=False, reason="no active Kubernetes grant covers the request")
