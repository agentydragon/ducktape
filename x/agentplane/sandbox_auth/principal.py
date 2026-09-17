"""Authenticate a Pod-bound Kubernetes bearer as the ServiceAccount its Pod runs as."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import AuthenticationV1Api

from x.agentplane.subjects import ServiceAccountRef

logger = logging.getLogger(__name__)

POD_NAME_CLAIM = "authentication.kubernetes.io/pod-name"
POD_UID_CLAIM = "authentication.kubernetes.io/pod-uid"
_SERVICE_ACCOUNT_PREFIX = "system:serviceaccount:"
_CACHE_TTL = timedelta(seconds=60)
_CACHE_SWEEP_SIZE = 256


@dataclass(frozen=True)
class WorkloadPrincipal:
    """The Pod-bound ServiceAccount identity a bearer proves."""

    namespace: str
    service_account_name: str
    service_account_subject: str
    pod_name: str
    pod_uid: str

    @property
    def account(self) -> ServiceAccountRef:
        """The subject a binding names. Whatever else owns the Pod -- a Sandbox, a Deployment,
        nothing -- is not the caller: an agent this cluster does not host has no owner to follow,
        and every sandbox runs as an account of its own, so following one would name a subset."""
        return ServiceAccountRef(namespace=self.namespace, name=self.service_account_name)


class RejectionReason(StrEnum):
    TOKEN_REJECTED = "token-rejected"
    POD_MISMATCH = "pod-mismatch"


class SandboxPrincipalRejectedError(Exception):
    """The bearer does not prove a workload identity; no bearer value is retained."""

    def __init__(self, reason: RejectionReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


@dataclass(frozen=True)
class _CachedPrincipal:
    principal: WorkloadPrincipal
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


class SandboxPrincipalResolver:
    """Resolve Pod-bound workload tokens only from ServiceAccounts in the allowed namespaces.

    An accepted verdict is kept against a digest of the bearer for the shorter of the token's
    remaining life and a minute. Every door in front of this -- a proxied connection, an MCP
    request, an HTTP route -- would otherwise spend a TokenReview per call re-learning what the
    previous call just learned, on the caller's critical path and against one shared API server.
    The cost is that a bearer that stops being valid keeps working until its entry lapses; a
    refusal is not kept, being cheap to repeat and wrong to hold against a token that has since
    been bound.
    """

    def __init__(
        self, *, authentication: AuthenticationV1Api, audience: str, allowed_service_account_namespaces: frozenset[str]
    ) -> None:
        if not audience:
            raise ValueError("audience must not be empty")
        if not allowed_service_account_namespaces or any(
            not namespace for namespace in allowed_service_account_namespaces
        ):
            raise ValueError("allowed_service_account_namespaces must contain at least one non-empty namespace")
        self._authentication = authentication
        self._audience = audience
        self._allowed_service_account_namespaces = allowed_service_account_namespaces
        self._cache: dict[str, _CachedPrincipal] = {}

    async def resolve_workload(self, token: str) -> WorkloadPrincipal:
        """Everything a bearer proves by itself: audience, ServiceAccount, and the Pod it is bound to."""
        key = hashlib.sha256(token.encode()).hexdigest()
        cached = self._cache.get(key)
        if cached is not None and cached.expires_at > time.monotonic():
            return cached.principal
        principal = await self._review(token)
        self._remember(key, principal, token)
        return principal

    def _remember(self, key: str, principal: WorkloadPrincipal, token: str) -> None:
        ttl = _CACHE_TTL.total_seconds()
        if (expiry := token_expiry(token)) is not None:
            ttl = min(ttl, (expiry - datetime.now(UTC)).total_seconds())
        if ttl <= 0:
            return
        now = time.monotonic()
        if len(self._cache) >= _CACHE_SWEEP_SIZE:
            self._cache = {k: v for k, v in self._cache.items() if v.expires_at > now}
        self._cache[key] = _CachedPrincipal(principal=principal, expires_at=now + ttl)

    async def _review(self, token: str) -> WorkloadPrincipal:
        """The Pod claims come from the TokenReview, which the API server answers by validating the
        token's bound object -- so a deleted or replaced Pod fails here, with nothing read."""
        try:
            review = await self._authentication.create_token_review(
                k8s_client.V1TokenReview(spec=k8s_client.V1TokenReviewSpec(token=token, audiences=[self._audience]))
            )
        except k8s_client.ApiException as error:
            logger.warning(
                "Kubernetes workload authentication failed: operation=create_token_review status=%d", error.status
            )
            raise
        status = review.status
        if status is None or not status.authenticated:
            raise SandboxPrincipalRejectedError(RejectionReason.TOKEN_REJECTED, "TokenReview rejected the bearer")
        if self._audience not in (status.audiences or []):
            raise SandboxPrincipalRejectedError(RejectionReason.TOKEN_REJECTED, "bearer has the wrong audience")
        subject = status.user.username if status.user is not None else None
        namespace, service_account = self._service_account(subject)
        extra = status.user.extra or {}
        pod_name = self._one_claim(extra.get(POD_NAME_CLAIM), "Pod name")
        pod_uid = self._one_claim(extra.get(POD_UID_CLAIM), "Pod UID")
        return WorkloadPrincipal(
            namespace=namespace,
            service_account_name=service_account,
            service_account_subject=subject,
            pod_name=pod_name,
            pod_uid=pod_uid,
        )

    def _service_account(self, subject: str | None) -> tuple[str, str]:
        if not isinstance(subject, str) or not subject.startswith(_SERVICE_ACCOUNT_PREFIX):
            raise SandboxPrincipalRejectedError(
                RejectionReason.TOKEN_REJECTED, "bearer subject is not a ServiceAccount"
            )
        remainder = subject.removeprefix(_SERVICE_ACCOUNT_PREFIX)
        parts = remainder.split(":")
        if len(parts) != 2 or not all(parts):
            raise SandboxPrincipalRejectedError(
                RejectionReason.TOKEN_REJECTED, "bearer has an invalid ServiceAccount subject"
            )
        namespace, service_account = parts
        if namespace not in self._allowed_service_account_namespaces:
            raise SandboxPrincipalRejectedError(RejectionReason.TOKEN_REJECTED, "bearer namespace is not accepted here")
        return namespace, service_account

    @staticmethod
    def _one_claim(values: list[str] | None, label: str) -> str:
        if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], str) or not values[0]:
            raise SandboxPrincipalRejectedError(
                RejectionReason.TOKEN_REJECTED, f"bearer is not bound to exactly one {label}"
            )
        return values[0]
