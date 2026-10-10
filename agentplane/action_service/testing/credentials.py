"""Bearers for exercising the Action Service's workload and operator boundaries in tests."""

from __future__ import annotations

from dataclasses import dataclass

from agentplane.action_service.models import OperatorPrincipal

# The public placeholder a sandbox sends; central egress substitutes the real workload bearer.
WORKLOAD_CREDENTIAL_PLACEHOLDER = "agentplane-credential-agentplane-workload"


@dataclass(frozen=True)
class StaticOperatorAuthenticator:
    """Accepts exactly one operator bearer, as `principal`."""

    token: str
    principal: OperatorPrincipal

    async def authenticate(self, token: str) -> OperatorPrincipal | None:
        return self.principal if token == self.token else None
