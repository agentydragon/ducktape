"""Authenticate a Pod-bound Kubernetes bearer as the ServiceAccount its Pod runs as."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import AuthenticationV1Api

from x.agentplane.subjects import ServiceAccountRef

logger = logging.getLogger(__name__)

POD_NAME_CLAIM = "authentication.kubernetes.io/pod-name"
POD_UID_CLAIM = "authentication.kubernetes.io/pod-uid"
_SERVICE_ACCOUNT_PREFIX = "system:serviceaccount:"


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


class WorkloadPrincipalRejectedError(Exception):
    """The bearer does not prove a workload identity; no bearer value is retained.

    There is one way to fail: the TokenReview did not accept the bearer as a workload this
    destination serves. `detail` says which check, for the log; a caller is told only that its
    bearer was refused, never which of these it tripped.
    """


class WorkloadPrincipalResolver:
    """Resolve Pod-bound workload tokens only from ServiceAccounts in the allowed namespaces.

    Every call reviews the bearer. A TokenReview writes nothing -- it is a signature check plus an
    existence check on the object the token is bound to -- so the round trip buys a verdict that is
    true now rather than one that was true a minute ago, and a revoked bearer stops working here at
    the moment it stops working anywhere.
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

    async def resolve_workload(self, token: str) -> WorkloadPrincipal:
        """Everything a bearer proves by itself: audience, ServiceAccount, and the Pod it is bound to.

        The Pod claims come from the TokenReview, which the API server answers by validating the
        token's bound object -- so a deleted or replaced Pod fails here, with nothing read.
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
            raise WorkloadPrincipalRejectedError("TokenReview rejected the bearer")
        if self._audience not in (status.audiences or []):
            raise WorkloadPrincipalRejectedError("bearer has the wrong audience")
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
            raise WorkloadPrincipalRejectedError("bearer subject is not a ServiceAccount")
        remainder = subject.removeprefix(_SERVICE_ACCOUNT_PREFIX)
        parts = remainder.split(":")
        if len(parts) != 2 or not all(parts):
            raise WorkloadPrincipalRejectedError("bearer has an invalid ServiceAccount subject")
        namespace, service_account = parts
        if namespace not in self._allowed_service_account_namespaces:
            raise WorkloadPrincipalRejectedError("bearer namespace is not accepted here")
        return namespace, service_account

    @staticmethod
    def _one_claim(values: list[str] | None, label: str) -> str:
        if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], str) or not values[0]:
            raise WorkloadPrincipalRejectedError(f"bearer is not bound to exactly one {label}")
        return values[0]
