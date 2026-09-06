"""FastAPI dependency for destination-side SandboxPrincipal authentication."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Never

from fastapi import HTTPException, Request, status

from x.agentplane.sandbox_auth.principal import (
    SandboxPrincipal,
    SandboxPrincipalRejectedError,
    SandboxPrincipalResolver,
)

_BEARER = re.compile(r"Bearer +([A-Za-z0-9._~+/=-]+)", re.IGNORECASE)


@dataclass(frozen=True)
class SandboxPrincipalAuthenticator:
    """Resolve the request's sole ordinary Authorization bearer or fail closed with 401."""

    resolver: SandboxPrincipalResolver

    async def __call__(self, request: Request) -> SandboxPrincipal:
        values = request.headers.getlist("authorization")
        match = _BEARER.fullmatch(values[0]) if len(values) == 1 else None
        if match is None:
            self._reject()
        try:
            return await self.resolver.resolve(match.group(1))
        except SandboxPrincipalRejectedError:
            self._reject()

    @staticmethod
    def _reject() -> Never:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "invalid workload bearer", headers={"WWW-Authenticate": "Bearer"}
        )
