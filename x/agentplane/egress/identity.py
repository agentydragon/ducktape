"""The proxy's door onto workload authentication: a refusal becomes the `DenyReason` a client sees.

What `WorkloadPrincipalAuthenticator` is for an HTTP route, this is for a proxied connection: the
same resolver, the same verdict, translated into the protocol the caller speaks.
"""

from __future__ import annotations

from dataclasses import dataclass

from x.agentplane.egress.policy import DenyReason
from x.agentplane.sandbox_auth.principal import (
    SandboxPrincipalRejectedError,
    SandboxPrincipalResolver,
    WorkloadPrincipal,
)


class IdentityRejectedError(Exception):
    """The token does not prove a workload identity; `reason` is what the client sees."""

    def __init__(self, reason: DenyReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


@dataclass(frozen=True)
class WorkloadIdentityVerifier:
    resolver: SandboxPrincipalResolver

    async def identify(self, token: str) -> WorkloadPrincipal:
        try:
            return await self.resolver.resolve_workload(token)
        except SandboxPrincipalRejectedError as error:
            raise IdentityRejectedError(DenyReason.TOKEN_REJECTED, str(error)) from error
