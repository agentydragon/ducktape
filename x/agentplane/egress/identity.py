"""Who is calling: the sidecar's Pod-bound token to the ServiceAccount its Pod runs as.

The token is a projected ServiceAccount token with the proxy's audience. TokenReview proves it and
names the Pod the API server bound it to, which is all the subject needs -- a deleted or replaced
Pod fails the TokenReview itself. The verdict is cached, keyed by a digest of the token, for the
shorter of the token's remaining life and a configured bound.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from kubernetes_asyncio.client import AuthenticationV1Api

from x.agentplane.egress.policy import DenyReason
from x.agentplane.sandbox_auth.principal import RejectionReason, SandboxPrincipalRejectedError, SandboxPrincipalResolver
from x.agentplane.subjects import ServiceAccountRef

logger = logging.getLogger(__name__)

_CACHE_SWEEP_SIZE = 256


@dataclass(frozen=True)
class PodIdentity:
    """The Pod a bearer proves, and the ServiceAccount it runs as -- the subject policy binds to."""

    namespace: str
    pod_name: str
    pod_uid: str
    service_account_name: str

    @property
    def subject(self) -> ServiceAccountRef:
        return ServiceAccountRef(namespace=self.namespace, name=self.service_account_name)


class IdentityRejectedError(Exception):
    """The token does not prove a live Pod; `reason` is what the client sees."""

    def __init__(self, reason: DenyReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


@dataclass(frozen=True)
class _CachedIdentity:
    identity: PodIdentity
    expires_at: float


def token_expiry(token: str) -> datetime | None:
    """The `exp` claim of a JWT, unverified: TokenReview is the verification, this only bounds a cache."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except binascii.Error, ValueError:
        return None
    expiry = claims.get("exp") if isinstance(claims, dict) else None
    return datetime.fromtimestamp(expiry, tz=UTC) if isinstance(expiry, int | float) else None


class PodIdentityVerifier:
    def __init__(
        self,
        *,
        authentication: AuthenticationV1Api,
        namespaces: frozenset[str],
        audience: str,
        cache_seconds: float,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._cache_seconds = cache_seconds
        self._clock = clock
        self._cache: dict[str, _CachedIdentity] = {}
        self._resolver = SandboxPrincipalResolver(
            authentication=authentication, audience=audience, allowed_service_account_namespaces=namespaces
        )

    async def identify(self, token: str) -> PodIdentity:
        key = hashlib.sha256(token.encode()).hexdigest()
        cached = self._cache.get(key)
        if cached is not None and cached.expires_at > time.monotonic():
            return cached.identity
        identity = await self._verify(token)
        self._remember(key, identity, token)
        return identity

    def _remember(self, key: str, identity: PodIdentity, token: str) -> None:
        ttl = self._cache_seconds
        if (expiry := token_expiry(token)) is not None:
            ttl = min(ttl, (expiry - self._clock()).total_seconds())
        if ttl <= 0:
            return
        now = time.monotonic()
        if len(self._cache) >= _CACHE_SWEEP_SIZE:
            self._cache = {k: v for k, v in self._cache.items() if v.expires_at > now}
        self._cache[key] = _CachedIdentity(identity=identity, expires_at=now + ttl)

    async def _verify(self, token: str) -> PodIdentity:
        """Every Pod is its ServiceAccount, whatever else owns it.

        Authorization is unaffected by what authenticates: a subject no binding names reaches no
        rule, so this is only ever the question of who is asking.
        """
        try:
            principal = await self._resolver.resolve_workload(token)
        except SandboxPrincipalRejectedError as error:
            # `resolve_workload` reads nothing beyond the TokenReview, so `SANDBOX_UNKNOWN` cannot
            # arrive and has no entry: a KeyError here would mean that stopping point moved.
            reason = {
                RejectionReason.TOKEN_REJECTED: DenyReason.TOKEN_REJECTED,
                RejectionReason.POD_MISMATCH: DenyReason.POD_MISMATCH,
            }[error.reason]
            raise IdentityRejectedError(reason, str(error)) from error
        return PodIdentity(
            namespace=principal.namespace,
            pod_name=principal.pod_name,
            pod_uid=principal.pod_uid,
            service_account_name=principal.service_account_name,
        )
