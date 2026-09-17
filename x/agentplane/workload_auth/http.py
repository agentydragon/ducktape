"""FastAPI dependencies for destination-side workload authentication."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Never

from fastapi import HTTPException, Request, status

from x.agentplane.workload_auth.bearer import parse_bearer, sole_header
from x.agentplane.workload_auth.principal import (
    WorkloadPrincipal,
    WorkloadPrincipalRejectedError,
    WorkloadPrincipalResolver,
)


@dataclass(frozen=True)
class WorkloadPrincipalAuthenticator:
    """Resolve the request's sole ordinary Authorization bearer or fail closed with 401."""

    resolver: WorkloadPrincipalResolver

    async def __call__(self, request: Request) -> WorkloadPrincipal:
        token = _sole_bearer(request)
        try:
            return await self.resolver.resolve_workload(token)
        except WorkloadPrincipalRejectedError:
            _reject()


def _sole_bearer(request: Request) -> str:
    value = sole_header(request.headers.getlist("authorization"))
    token = parse_bearer(value) if value is not None else None
    if token is None:
        _reject()
    return token


def _reject() -> Never:
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid workload bearer", headers={"WWW-Authenticate": "Bearer"})
