"""The proxy's door onto workload authentication: a refusal becomes the `DenyReason` a client sees.

What `WorkloadPrincipalAuthenticator` is for an HTTP route, this is for a proxied connection: the
same resolver, the same verdict, translated into the protocol the caller speaks.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from x.agentplane.egress.policy import DenyReason
from x.agentplane.workload_auth.principal import (
    WorkloadPrincipal,
    WorkloadPrincipalRejectedError,
    WorkloadPrincipalResolver,
)

logger = logging.getLogger(__name__)


class IdentityRejectedError(Exception):
    """The token does not prove a workload identity; `reason` is what the client sees."""

    def __init__(self, reason: DenyReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


@dataclass(frozen=True)
class WorkloadIdentityVerifier:
    resolver: WorkloadPrincipalResolver

    async def identify(self, token: str) -> WorkloadPrincipal:
        try:
            return await self.resolver.resolve_workload(token)
        except WorkloadPrincipalRejectedError as error:
            raise IdentityRejectedError(DenyReason.TOKEN_REJECTED, str(error)) from error


@dataclass(frozen=True)
class ProjectedTokenVerifier:
    """The audiences this proxy will substitute a sidecar's projected token for, and the review each one costs.

    An audience with no resolver is one no operator configured, so a credential naming it resolves
    to nothing however well-formed the token presented for it was. The review is what makes the
    substituted value the caller's own: the API server answers for the Pod the token is bound to,
    and only a token bound to the very Pod that authenticated this hop is kept. Without it the proxy
    would spend whatever token it was handed on a destination the presenter cannot reach directly,
    which is the confused deputy this exists to refuse.
    """

    resolvers: Mapping[str, WorkloadPrincipalResolver]

    async def verified(self, presented: Mapping[str, str], identity: WorkloadPrincipal) -> dict[str, str]:
        """The presented tokens that prove the same Pod as `identity`, by audience.

        One that does not is dropped rather than raised: it denies the rule that names it with
        `credential-unavailable` at the point of use, and leaves traffic that never wanted it alone.
        """
        verified: dict[str, str] = {}
        for audience, token in presented.items():
            resolver = self.resolvers.get(audience)
            if resolver is None:
                logger.warning("no resolver configured for projected token audience %r", audience)
                continue
            try:
                principal = await resolver.resolve_workload(token)
            except WorkloadPrincipalRejectedError as error:
                logger.warning("projected token for audience %r rejected: %s", audience, error)
                continue
            if principal != identity:
                logger.warning("projected token for audience %r is not this hop's Pod", audience)
                continue
            verified[audience] = token
        return verified
