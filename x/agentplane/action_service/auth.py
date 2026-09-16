"""Distinct workload and operator authentication adapters for the Action Service."""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from typing import Protocol

from x.agentplane.action_service.models import Principal, PrincipalRole, SandboxCaller, ServiceAccountRef
from x.agentplane.sandbox_auth.principal import SandboxPrincipal, WorkloadPrincipal


class OperatorAuthenticator(Protocol):
    """Replaceable BFF/operator boundary; deliberately separate from SandboxPrincipal auth."""

    async def authenticate(self, token: str) -> Principal | None: ...


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

    async def authenticate(self, token: str) -> Principal | None:
        presented = hashlib.sha256(token.encode()).digest()
        if not hmac.compare_digest(presented, self._token_digest):
            return None
        return Principal(issuer="configured-operator", subject=self._subject, role=PrincipalRole.OPERATOR)


def workload_principal(principal: WorkloadPrincipal) -> Principal:
    """Derive durable ownership from what the token actually proved.

    A Pod a live Sandbox controls is owned by that Sandbox, keyed by its UID so a replacement
    Sandbox of the same name is a different caller. A Pod no Sandbox controls -- an agent running
    as a plain Deployment -- is owned by the ServiceAccount it runs as, which is the same principal
    an external OAuth grant acting as that ServiceAccount resolves to.
    """
    if isinstance(principal, SandboxPrincipal):
        return SandboxCaller(namespace=principal.namespace, sandbox_uid=principal.sandbox_uid).principal()
    return ServiceAccountRef(namespace=principal.namespace, name=principal.service_account_name).principal()
