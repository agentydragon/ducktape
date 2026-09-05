"""Replaceable bearer authentication; v0 production adapter uses Kubernetes TokenReview."""

from __future__ import annotations

import logging
from typing import Protocol

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import AuthenticationV1Api

from x.agentplane.action_service.models import Principal, PrincipalRole

logger = logging.getLogger(__name__)


class Authenticator(Protocol):
    async def authenticate(self, token: str) -> Principal | None: ...


class KubernetesTokenAuthenticator:
    """Map audience-bound ServiceAccount tokens to caller or operator principals.

    A managed Sandbox mounts its projected token only into a local relay. The runner calls that
    relay without token material; the relay re-reads the projected token and calls this service.
    BFFs may use a separately configured operator ServiceAccount. A reviewed but unmapped subject
    is refused, so choosing this service's audience is not itself authorization.
    """

    def __init__(
        self,
        authentication: AuthenticationV1Api,
        *,
        audience: str,
        caller_subjects: frozenset[str],
        operator_subjects: frozenset[str],
    ) -> None:
        overlap = caller_subjects & operator_subjects
        if overlap:
            raise ValueError(f"subjects cannot be both caller and operator: {sorted(overlap)}")
        self._authentication = authentication
        self._audience = audience
        self._caller_subjects = caller_subjects
        self._operator_subjects = operator_subjects

    async def authenticate(self, token: str) -> Principal | None:
        review = await self._authentication.create_token_review(
            k8s_client.V1TokenReview(spec=k8s_client.V1TokenReviewSpec(token=token, audiences=[self._audience]))
        )
        status = review.status
        if status is None or not status.authenticated or status.user is None or not status.user.username:
            logger.info("TokenReview refused a token: %s", status and status.error)
            return None
        if self._audience not in (status.audiences or []):
            logger.info("TokenReview returned the wrong audience: %s", status.audiences)
            return None
        subject = str(status.user.username)
        if subject in self._caller_subjects:
            role = PrincipalRole.CALLER
        elif subject in self._operator_subjects:
            role = PrincipalRole.OPERATOR
        else:
            logger.warning("TokenReview authenticated an unmapped Action Service subject: %s", subject)
            return None
        return Principal(issuer="kubernetes", subject=subject, role=role)
