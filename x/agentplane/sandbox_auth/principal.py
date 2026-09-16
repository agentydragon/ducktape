"""Authenticate a Pod-bound Kubernetes bearer, and resolve the live Sandbox owning it where one must.

Everything except the final ownership step is common: TokenReview against the audience, the
ServiceAccount subject, and the Pod the token is bound to. A caller that requires a managed Sandbox
asks for `SandboxPrincipal`; one that accepts any workload in an allowed namespace -- a Deployment
named as a ServiceAccount subject -- asks for `WorkloadPrincipal` and gets the same proofs without
the ownership requirement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import AuthenticationV1Api, CoreV1Api

logger = logging.getLogger(__name__)

POD_NAME_CLAIM = "authentication.kubernetes.io/pod-name"
POD_UID_CLAIM = "authentication.kubernetes.io/pod-uid"
SANDBOX_KIND = "Sandbox"
_SERVICE_ACCOUNT_PREFIX = "system:serviceaccount:"


@dataclass(frozen=True)
class WorkloadPrincipal:
    """The Pod-bound ServiceAccount identity a bearer proves, before any ownership requirement."""

    namespace: str
    service_account_name: str
    service_account_subject: str
    pod_name: str
    pod_uid: str


@dataclass(frozen=True)
class SandboxPrincipal(WorkloadPrincipal):
    """A `WorkloadPrincipal` whose Pod is controlled by exactly one live managed Sandbox."""

    sandbox_name: str
    sandbox_uid: str


def sandbox_controller(pod: k8s_client.V1Pod) -> k8s_client.V1OwnerReference | None:
    """The one Sandbox controlling this Pod, or None: no owner, several, or an incomplete reference.

    A Pod with no Sandbox owner is not by itself a rejection -- a caller that admits plain workloads
    reads this as "not a Sandbox" and decides on the ServiceAccount instead.
    """
    metadata = pod.metadata
    owners = [
        owner
        for owner in (metadata.owner_references or [] if metadata is not None else [])
        if owner.kind == SANDBOX_KIND and owner.controller is True
    ]
    if len(owners) != 1:
        return None
    return owners[0] if owners[0].name and owners[0].uid else None


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
    """Resolve Pod-bound workload tokens only from ServiceAccounts in the allowed namespaces."""

    def __init__(
        self,
        *,
        authentication: AuthenticationV1Api,
        core_v1: CoreV1Api,
        audience: str,
        allowed_service_account_namespaces: frozenset[str],
    ) -> None:
        if not audience:
            raise ValueError("audience must not be empty")
        if not allowed_service_account_namespaces or any(
            not namespace for namespace in allowed_service_account_namespaces
        ):
            raise ValueError("allowed_service_account_namespaces must contain at least one non-empty namespace")
        self._authentication = authentication
        self._core_v1 = core_v1
        self._audience = audience
        self._allowed_service_account_namespaces = allowed_service_account_namespaces

    async def resolve(self, token: str) -> SandboxPrincipal:
        """Return only the destination-safe principal; never infer identity from request metadata."""
        principal, _ = await self.resolve_with_pod(token)
        return principal

    async def resolve_with_pod(self, token: str) -> tuple[SandboxPrincipal, k8s_client.V1Pod]:
        """Also return the authoritative live Pod for egress-only source-address correlation."""
        workload, pod = await self.resolve_workload_with_pod(token)
        owner = sandbox_controller(pod)
        if owner is None:
            raise SandboxPrincipalRejectedError(
                RejectionReason.SANDBOX_UNKNOWN, f"Pod {workload.pod_name} is not controlled by exactly one Sandbox"
            )
        return (
            SandboxPrincipal(
                namespace=workload.namespace,
                service_account_name=workload.service_account_name,
                service_account_subject=workload.service_account_subject,
                pod_name=workload.pod_name,
                pod_uid=workload.pod_uid,
                sandbox_name=owner.name,
                sandbox_uid=owner.uid,
            ),
            pod,
        )

    async def resolve_workload_with_pod(self, token: str) -> tuple[WorkloadPrincipal, k8s_client.V1Pod]:
        """Everything a bearer proves short of ownership: audience, ServiceAccount, and the bound Pod.

        A caller that accepts any workload in an allowed namespace stops here. One that requires a
        managed Sandbox uses `resolve_with_pod`, which adds the ownership check to this.
        """
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
        try:
            pod = await self._core_v1.read_namespaced_pod(pod_name, namespace)
        except k8s_client.ApiException as error:
            logger.warning(
                "Kubernetes workload authentication failed: operation=read_namespaced_pod status=%d", error.status
            )
            if error.status == 404:
                raise SandboxPrincipalRejectedError(RejectionReason.POD_MISMATCH, f"Pod {pod_name} is gone") from error
            raise
        metadata = pod.metadata
        if metadata is None or metadata.name != pod_name or metadata.namespace != namespace or metadata.uid != pod_uid:
            raise SandboxPrincipalRejectedError(
                RejectionReason.POD_MISMATCH, f"Pod {pod_name} no longer matches the bearer binding"
            )
        return (
            WorkloadPrincipal(
                namespace=namespace,
                service_account_name=service_account,
                service_account_subject=subject,
                pod_name=pod_name,
                pod_uid=pod_uid,
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
