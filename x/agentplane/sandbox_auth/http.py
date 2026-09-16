"""FastAPI dependencies for destination-side workload authentication."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Never

from fastapi import HTTPException, Request, status

from x.agentplane.sandbox_auth.principal import (
    SandboxPrincipal,
    SandboxPrincipalRejectedError,
    SandboxPrincipalResolver,
    WorkloadPrincipal,
)

_BEARER = re.compile(r"Bearer +([A-Za-z0-9._~+/=-]+)", re.IGNORECASE)


@dataclass(frozen=True)
class WorkloadPrincipalAuthenticator:
    """As `SandboxPrincipalAuthenticator`, for a route that serves either workload kind.

    Returns a `SandboxPrincipal` where a live Sandbox controls the Pod and a plain
    `WorkloadPrincipal` otherwise, so the route decides on the kind rather than being refused one.
    """

    resolver: SandboxPrincipalResolver

    async def __call__(self, request: Request) -> WorkloadPrincipal:
        token = _sole_bearer(request)
        try:
            return await self.resolver.resolve_caller(token)
        except SandboxPrincipalRejectedError:
            _reject()


@dataclass(frozen=True)
class SandboxPrincipalAuthenticator:
    """Resolve the request's sole ordinary Authorization bearer or fail closed with 401."""

    resolver: SandboxPrincipalResolver

    async def __call__(self, request: Request) -> SandboxPrincipal:
        token = _sole_bearer(request)
        try:
            return await self.resolver.resolve(token)
        except SandboxPrincipalRejectedError:
            _reject()


def _sole_bearer(request: Request) -> str:
    values = request.headers.getlist("authorization")
    match = _BEARER.fullmatch(values[0]) if len(values) == 1 else None
    if match is None:
        _reject()
    return match.group(1)


def _reject() -> Never:
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid workload bearer", headers={"WWW-Authenticate": "Bearer"})
