"""Operator authentication adapters for the Action Service."""

from __future__ import annotations

from agentplane.action_service.operator_oidc import OidcOperatorAuthenticator


class DisabledOperatorAuthenticator:
    """Fail closed when a deployment has not configured its operator/BFF adapter."""

    async def authenticate(self, token: str) -> None:
        del token


# The BFF/operator boundary; deliberately separate from workload auth.
OperatorAuthenticator = OidcOperatorAuthenticator | DisabledOperatorAuthenticator
