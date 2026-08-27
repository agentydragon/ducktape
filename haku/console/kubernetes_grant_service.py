"""Application service for owned, principal-scoped Kubernetes grants and rule matching."""

from __future__ import annotations

import datetime
from collections.abc import Callable, Sequence, Set
from typing import Protocol
from uuid import UUID

from haku.console.grant_principal import GrantPrincipal, RequestPrincipal, grant_principal_applies_to
from haku.console.kubernetes_grant_models import (
    KubernetesGrant,
    KubernetesGrantDecision,
    KubernetesGrantNotFoundError,
    KubernetesGrantScope,
    KubernetesGrantScopeKind,
    KubernetesGrantSpec,
    KubernetesGrantStatus,
    KubernetesNamespacesGrantScope,
    KubernetesRule,
    validate_grant_scope_rules,
)


class KubernetesGrantRepository(Protocol):
    async def create(
        self,
        *,
        owner_agent_id: UUID,
        grant_principal: GrantPrincipal,
        source_tool_call_id: str,
        scope: KubernetesGrantScope,
        rules: Sequence[KubernetesRule],
        created_at: datetime.datetime,
        expires_at: datetime.datetime,
    ) -> KubernetesGrant: ...

    async def create_many(
        self,
        *,
        owner_agent_id: UUID,
        grant_principal: GrantPrincipal,
        source_tool_call_id: str,
        grants: Sequence[KubernetesGrantSpec],
        created_at: datetime.datetime,
        expires_at: datetime.datetime,
    ) -> tuple[KubernetesGrant, ...]: ...

    async def list(self, *, owner_agent_id: UUID, include_terminal: bool = True) -> tuple[KubernetesGrant, ...]: ...

    async def get(self, *, owner_agent_id: UUID, grant_id: UUID) -> KubernetesGrant: ...

    async def release(
        self, *, owner_agent_id: UUID, grant_id: UUID, reason: str, ended_at: datetime.datetime
    ) -> KubernetesGrant: ...

    async def revoke(
        self, *, owner_agent_id: UUID, grant_id: UUID, reason: str, ended_at: datetime.datetime
    ) -> KubernetesGrant: ...

    async def revoke_source(
        self, *, owner_agent_id: UUID, source_tool_call_id: str, reason: str, ended_at: datetime.datetime
    ) -> tuple[KubernetesGrant, ...]: ...

    async def expire(self, *, now: datetime.datetime, owner_agent_id: UUID | None = None) -> int: ...

    async def list_for_request_principal(
        self, *, request_principal: RequestPrincipal, include_terminal: bool = True
    ) -> tuple[KubernetesGrant, ...]: ...

    async def active_for_request_principal(
        self, *, request_principal: RequestPrincipal, now: datetime.datetime
    ) -> tuple[KubernetesGrant, ...]: ...


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


def rule_covers(granted: KubernetesRule, requested: KubernetesRule) -> bool:
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


def rules_cover(granted: Sequence[KubernetesRule], required: Sequence[KubernetesRule]) -> bool:
    """Every required rule must be covered by one grant rule; no partial matches are accepted."""

    return bool(required) and all(any(rule_covers(candidate, needed) for candidate in granted) for needed in required)


def scope_covers(granted: KubernetesGrantScope, required: KubernetesGrantScope) -> bool:
    """Whether one grant scope covers the complete required request scope."""

    if required.kind is KubernetesGrantScopeKind.NAMESPACES:
        if granted.kind is KubernetesGrantScopeKind.ALL_NAMESPACES:
            return True
        return (
            isinstance(granted, KubernetesNamespacesGrantScope)
            and isinstance(required, KubernetesNamespacesGrantScope)
            and required.namespaces <= granted.namespaces
        )
    return granted.kind is required.kind


class KubernetesGrantService:
    """Own grant lifecycle separately from trusted applicability matching.

    No method reads a ContextVar, ambient request, global caller, or tool arguments to determine
    identity. Operator lifecycle methods require an explicit owner; Agent-facing lifecycle and
    authorization methods require a complete trusted ``RequestPrincipal``.
    """

    def __init__(
        self,
        repository: KubernetesGrantRepository,
        *,
        max_lifetime: datetime.timedelta,
        clock: Callable[[], datetime.datetime] = _utcnow,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._max_lifetime = max_lifetime
        if max_lifetime <= datetime.timedelta():
            raise ValueError("max_lifetime must be positive")

    async def create_grant(
        self,
        *,
        owner_agent_id: UUID,
        grant_principal: GrantPrincipal,
        source_tool_call_id: str,
        scope: KubernetesGrantScope,
        rules: Sequence[KubernetesRule],
        expires_at: datetime.datetime,
    ) -> KubernetesGrant:
        """Create one owned, principal-scoped grant and retain source-call provenance."""

        rules = tuple(rules)
        if not rules:
            raise ValueError("rules must not be empty")
        grants = await self.create_grants(
            owner_agent_id=owner_agent_id,
            grant_principal=grant_principal,
            source_tool_call_id=source_tool_call_id,
            grants=(KubernetesGrantSpec(scope=scope, rules=rules),),
            expires_at=expires_at,
        )
        return grants[0]

    async def create_grants(
        self,
        *,
        owner_agent_id: UUID,
        grant_principal: GrantPrincipal,
        source_tool_call_id: str,
        grants: Sequence[KubernetesGrantSpec],
        expires_at: datetime.datetime,
    ) -> tuple[KubernetesGrant, ...]:
        """Atomically create exact grants with one source call and shared timestamps."""

        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("grant service clock must return a timezone-aware datetime")
        if not source_tool_call_id:
            raise ValueError("source_tool_call_id must not be empty")
        grants = tuple(grants)
        if not grants:
            raise ValueError("grants must not be empty")
        if len(grants) > 32:
            raise ValueError("at most 32 grants may be created by one ToolCall")
        for grant in grants:
            validate_grant_scope_rules(grant.scope, grant.rules)
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

    async def list_grants(self, *, owner_agent_id: UUID, include_terminal: bool = True) -> tuple[KubernetesGrant, ...]:
        await self.expire_grants(owner_agent_id=owner_agent_id)
        return await self._repository.list(owner_agent_id=owner_agent_id, include_terminal=include_terminal)

    async def list_applicable_grants(
        self, *, request_principal: RequestPrincipal, include_terminal: bool = True
    ) -> tuple[KubernetesGrant, ...]:
        """List only grants this authenticated request principal may exercise."""

        await self.expire_grants(owner_agent_id=request_principal.agent_id)
        return await self._repository.list_for_request_principal(
            request_principal=request_principal, include_terminal=include_terminal
        )

    async def get_grant(self, *, owner_agent_id: UUID, grant_id: UUID) -> KubernetesGrant:
        grant = await self._repository.get(owner_agent_id=owner_agent_id, grant_id=grant_id)
        now = self._clock()
        if grant.status is KubernetesGrantStatus.ACTIVE and grant.expires_at <= now:
            await self._repository.expire(owner_agent_id=owner_agent_id, now=now)
            grant = await self._repository.get(owner_agent_id=owner_agent_id, grant_id=grant_id)
        return grant

    async def get_applicable_grant(self, *, request_principal: RequestPrincipal, grant_id: UUID) -> KubernetesGrant:
        grant = await self.get_grant(owner_agent_id=request_principal.agent_id, grant_id=grant_id)
        if not grant_principal_applies_to(grant.principal, request_principal):
            raise KubernetesGrantNotFoundError(str(grant_id))
        return grant

    async def release_grants(
        self, *, owner_agent_id: UUID, grant_ids: Sequence[UUID], reason: str = "released"
    ) -> tuple[KubernetesGrant, ...]:
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
    ) -> tuple[KubernetesGrant, ...]:
        for grant_id in grant_ids:
            await self.get_applicable_grant(request_principal=request_principal, grant_id=grant_id)
        return await self.release_grants(owner_agent_id=request_principal.agent_id, grant_ids=grant_ids, reason=reason)

    async def revoke_grant(self, *, owner_agent_id: UUID, grant_id: UUID, reason: str) -> KubernetesGrant:
        return await self._repository.revoke(
            owner_agent_id=owner_agent_id, grant_id=grant_id, reason=reason, ended_at=self._clock()
        )

    async def revoke_grant_set(
        self, *, owner_agent_id: UUID, source_tool_call_id: str, reason: str
    ) -> tuple[KubernetesGrant, ...]:
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

    async def match_request(
        self,
        *,
        request_principal: RequestPrincipal,
        required_scope: KubernetesGrantScope,
        required_rules: Sequence[KubernetesRule],
    ) -> KubernetesGrantDecision:
        """Match one request against active grants and return the earliest expiry bound."""

        now = self._clock()
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
            return KubernetesGrantDecision(
                allowed=True,
                grant_id=grant.grant_id,
                expires_at=grant.expires_at,
                reason="active Kubernetes grant covers the request",
            )
        return KubernetesGrantDecision(allowed=False, reason="no active Kubernetes grant covers the request")
