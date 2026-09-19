"""Distinct workload and operator authentication adapters for the Action Service."""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from typing import Protocol

from agentplane.action_service.models import CallerPrincipal, OperatorPrincipal
from agentplane.workload_auth.principal import WorkloadPrincipal


class OperatorAuthenticator(Protocol):
    """Replaceable BFF/operator boundary; deliberately separate from workload auth."""

    async def authenticate(self, token: str) -> OperatorPrincipal | None: ...


class DisabledOperatorAuthenticator:
    """Fail closed when a deployment has not configured its operator/BFF adapter."""

    async def authenticate(self, token: str) -> None:
        del token


class ConfiguredOperatorBearerAuthenticator:
    """Minimal v0 adapter for one explicitly configured BFF bearer.

    This is not workload identity and does not map Kubernetes ServiceAccount subject lists. The raw
    bearer is read once from a mounted file and only its digest is retained. Replace this adapter
    with the BFF's authoritative session/JWT verifier without changing the Action Service domain.
    """

    def __init__(self, *, token_digest: bytes, subject: str) -> None:
        if not token_digest:
            raise ValueError("token_digest must not be empty")
        if not subject:
            raise ValueError("subject must not be empty")
        self._token_digest = token_digest
        self._subject = subject

    @classmethod
    def from_file(cls, path: Path, *, subject: str) -> ConfiguredOperatorBearerAuthenticator:
        token = path.read_bytes().strip()
        if not token:
            raise ValueError("operator bearer file must not be empty")
        return cls(token_digest=hashlib.sha256(token).digest(), subject=subject)

    async def authenticate(self, token: str) -> OperatorPrincipal | None:
        presented = hashlib.sha256(token.encode()).digest()
        if not hmac.compare_digest(presented, self._token_digest):
            return None
        return OperatorPrincipal(issuer="configured-operator", subject=self._subject)


def workload_principal(principal: WorkloadPrincipal) -> CallerPrincipal:
    return CallerPrincipal(account=principal.account)
