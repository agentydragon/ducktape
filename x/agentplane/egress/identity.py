"""Who is calling: the sidecar's Pod-bound token to the live Pod to the Sandbox that owns it.

The token is a projected ServiceAccount token with the proxy's audience. TokenReview proves it and
names the Pod it is bound to; the Pod is then read live so a replaced Pod (same name, new UID) or a
token presented from another address (copied out of its Pod) is refused; the Pod's controller owner
is the Sandbox. The verdict is cached, keyed by a digest of the token, for the shorter of the token's
remaining life and a bound, and the source-address check runs on every call regardless.
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

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import AuthenticationV1Api, CoreV1Api

from x.agentplane.egress.policy import DenyReason
from x.agentplane.egress.resources import SANDBOX_KIND

logger = logging.getLogger(__name__)

POD_NAME_CLAIM = "authentication.kubernetes.io/pod-name"
POD_UID_CLAIM = "authentication.kubernetes.io/pod-uid"
_CACHE_SWEEP_SIZE = 256


@dataclass(frozen=True)
class PodIdentity:
    pod_name: str
    pod_uid: str
    pod_ip: str
    sandbox_name: str
    sandbox_uid: str


class IdentityRejectedError(Exception):
    """The token does not prove a live Sandbox Pod at this address; `reason` is what the client sees."""

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
    except (binascii.Error, ValueError):
        return None
    expiry = claims.get("exp") if isinstance(claims, dict) else None
    return datetime.fromtimestamp(expiry, tz=UTC) if isinstance(expiry, int | float) else None


class PodIdentityVerifier:
    def __init__(
        self,
        *,
        authentication: AuthenticationV1Api,
        core_v1: CoreV1Api,
        namespace: str,
        audience: str,
        cache_seconds: float,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._authentication = authentication
        self._core_v1 = core_v1
        self._namespace = namespace
        self._audience = audience
        self._cache_seconds = cache_seconds
        self._clock = clock
        self._cache: dict[str, _CachedIdentity] = {}

    async def identify(self, token: str, source_ip: str) -> PodIdentity:
        key = hashlib.sha256(token.encode()).hexdigest()
        cached = self._cache.get(key)
        if cached is None or cached.expires_at <= time.monotonic():
            identity = await self._verify(token)
            self._remember(key, identity, token)
        else:
            identity = cached.identity
        if identity.pod_ip != source_ip:
            raise IdentityRejectedError(
                DenyReason.POD_MISMATCH, f"token bound to Pod {identity.pod_name} presented elsewhere"
            )
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
        review = await self._authentication.create_token_review(
            k8s_client.V1TokenReview(spec=k8s_client.V1TokenReviewSpec(token=token, audiences=[self._audience]))
        )
        status = review.status
        if status is None or not status.authenticated:
            raise IdentityRejectedError(
                DenyReason.TOKEN_REJECTED, f"TokenReview rejected the token: {status and status.error}"
            )
        if self._audience not in (status.audiences or []):
            raise IdentityRejectedError(DenyReason.TOKEN_REJECTED, "token audience is not the proxy's")
        user = status.user
        if user is None or not (user.username or "").startswith(f"system:serviceaccount:{self._namespace}:"):
            raise IdentityRejectedError(
                DenyReason.TOKEN_REJECTED, "token is not a ServiceAccount of the proxy's namespace"
            )
        extra = user.extra or {}
        pod_names, pod_uids = extra.get(POD_NAME_CLAIM, []), extra.get(POD_UID_CLAIM, [])
        if len(pod_names) != 1 or len(pod_uids) != 1:
            raise IdentityRejectedError(DenyReason.TOKEN_REJECTED, "token is not bound to one Pod")
        pod_name, pod_uid = pod_names[0], pod_uids[0]
        try:
            pod = await self._core_v1.read_namespaced_pod(pod_name, self._namespace)
        except k8s_client.ApiException as error:
            if error.status == 404:
                raise IdentityRejectedError(DenyReason.POD_MISMATCH, f"Pod {pod_name} is gone") from error
            raise
        if pod.metadata.uid != pod_uid:
            raise IdentityRejectedError(
                DenyReason.POD_MISMATCH, f"Pod {pod_name} was replaced since the token was issued"
            )
        pod_ip = pod.status.pod_ip if pod.status is not None else None
        if not pod_ip:
            raise IdentityRejectedError(DenyReason.POD_MISMATCH, f"Pod {pod_name} has no address yet")
        owners = [
            owner
            for owner in pod.metadata.owner_references or []
            if owner.kind == SANDBOX_KIND and owner.controller is True
        ]
        if len(owners) != 1:
            raise IdentityRejectedError(DenyReason.SANDBOX_UNKNOWN, f"Pod {pod_name} is not controlled by one Sandbox")
        return PodIdentity(
            pod_name=pod_name, pod_uid=pod_uid, pod_ip=pod_ip, sandbox_name=owners[0].name, sandbox_uid=owners[0].uid
        )
