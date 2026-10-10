"""Operator authentication adapters for the Action Service."""

from __future__ import annotations

from typing import Protocol

from agentplane.action_service.models import OperatorPrincipal


class OperatorAuthenticator(Protocol):
    """Replaceable BFF/operator boundary; deliberately separate from workload auth."""

    async def authenticate(self, token: str) -> OperatorPrincipal | None: ...


class DisabledOperatorAuthenticator:
    """Fail closed when a deployment has not configured its operator/BFF adapter."""

    async def authenticate(self, token: str) -> None:
        del token
