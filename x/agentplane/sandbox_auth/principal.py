"""Authenticate a Pod-bound Kubernetes bearer and resolve its live owning Sandbox."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import AuthenticationV1Api, CoreV1Api

POD_NAME_CLAIM = "authentication.kubernetes.io/pod-name"
POD_UID_CLAIM = "authentication.kubernetes.io/pod-uid"
SANDBOX_KIND = "Sandbox"
_SERVICE_ACCOUNT_PREFIX = "system:serviceaccount:"


@dataclass(frozen=True)
class SandboxPrincipal:
    """The live managed Sandbox identity proven by a Pod-bound ServiceAccount token."""

    namespace: str
    service_account_name: str
    service_account_subject: str
    pod_name: str
    pod_uid: str
    sandbox_name: str
    sandbox_uid: str


class RejectionReason(StrEnum):
    TOKEN_REJECTED = "token-rejected"
    POD_MISMATCH = "pod-mismatch"
    SANDBOX_UNKNOWN = "sandbox-unknown"


class SandboxPrincipalRejectedError(Exception):
    """The bearer does not prove one live managed Sandbox; no bearer value is retained."""

    def __init__(self, reason: RejectionReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


class SandboxPrincipalResolver:
    """Resolve Pod-bound Kubernetes workload tokens within an allowed namespace scope."""

    def __init__(
        self, *, authentication: AuthenticationV1Api, core_v1: CoreV1Api, audience: str, namespaces: frozenset[str]
    ) -> None:
        if not audience:
            raise ValueError("audience must not be empty")
        if not namespaces or any(not namespace for namespace in namespaces):
            raise ValueError("namespaces must contain at least one non-empty namespace")
        self._authentication = authentication
        self._core_v1 = core_v1
        self._audience = audience
        self._namespaces = namespaces

    async def resolve(self, token: str) -> SandboxPrincipal:
        """Return only the destination-safe principal; never infer identity from request metadata."""
        principal, _ = await self.resolve_with_pod(token)
        return principal

    async def resolve_with_pod(self, token: str) -> tuple[SandboxPrincipal, k8s_client.V1Pod]:
        """Also return the authoritative live Pod for egress-only source-address correlation."""
        review = await self._authentication.create_token_review(
            k8s_client.V1TokenReview(spec=k8s_client.V1TokenReviewSpec(token=token, audiences=[self._audience]))
        )
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
        try:
            pod = await self._core_v1.read_namespaced_pod(pod_name, namespace)
        except k8s_client.ApiException as error:
            if error.status == 404:
                raise SandboxPrincipalRejectedError(RejectionReason.POD_MISMATCH, f"Pod {pod_name} is gone") from error
            raise
        metadata = pod.metadata
        if metadata is None or metadata.name != pod_name or metadata.namespace != namespace or metadata.uid != pod_uid:
            raise SandboxPrincipalRejectedError(
                RejectionReason.POD_MISMATCH, f"Pod {pod_name} no longer matches the bearer binding"
            )
        owners = [
            owner
            for owner in metadata.owner_references or []
            if owner.kind == SANDBOX_KIND and owner.controller is True
        ]
        if len(owners) != 1:
            raise SandboxPrincipalRejectedError(
                RejectionReason.SANDBOX_UNKNOWN, f"Pod {pod_name} is not controlled by exactly one Sandbox"
            )
        owner = owners[0]
        if not owner.name or not owner.uid:
            raise SandboxPrincipalRejectedError(
                RejectionReason.SANDBOX_UNKNOWN, f"Pod {pod_name} has an incomplete Sandbox owner"
            )
        return (
            SandboxPrincipal(
                namespace=namespace,
                service_account_name=service_account,
                service_account_subject=subject,
                pod_name=pod_name,
                pod_uid=pod_uid,
                sandbox_name=owner.name,
                sandbox_uid=owner.uid,
            ),
            pod,
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
        if namespace not in self._namespaces:
            raise SandboxPrincipalRejectedError(RejectionReason.TOKEN_REJECTED, "bearer namespace is not accepted here")
        return namespace, service_account

    @staticmethod
    def _one_claim(values: list[str] | None, label: str) -> str:
        if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], str) or not values[0]:
            raise SandboxPrincipalRejectedError(
                RejectionReason.TOKEN_REJECTED, f"bearer is not bound to exactly one {label}"
            )
        return values[0]
