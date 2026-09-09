"""Authenticate Sandbox or external callers without exposing an operator credential bypass."""

from __future__ import annotations

import re
from typing import Never

from fastapi import HTTPException, Request

from x.agentplane.action_service.auth import workload_principal
from x.agentplane.action_service.models import Principal
from x.agentplane.action_service.oauth import ActionsOAuthProxy
from x.agentplane.sandbox_auth.http import SandboxPrincipalAuthenticator

_BEARER = re.compile(r"Bearer +([A-Za-z0-9._~+/=-]+)", re.IGNORECASE)


class CallerAuthenticator:
    def __init__(self, sandbox: SandboxPrincipalAuthenticator, oauth: ActionsOAuthProxy | None = None) -> None:
        self._sandbox = sandbox
        self._oauth = oauth

    async def __call__(self, request: Request) -> Principal:
        request.state.action_external_grant = None
        values = request.headers.getlist("authorization")
        match = _BEARER.fullmatch(values[0]) if len(values) == 1 else None
        if match is not None and self._oauth is not None:
            grant = await self._oauth.authenticate(match.group(1))
            if grant is not None:
                request.state.action_external_grant = grant.provenance()
                return grant.principal()
            if self._oauth.targets_issuer(match.group(1)):
                self._reject_external()
        try:
            return workload_principal(await self._sandbox(request))
        except HTTPException as error:
            if error.status_code != 401 or self._oauth is None:
                raise
            self._reject_external()

    def _reject_external(self) -> Never:
        assert self._oauth is not None
        metadata = f"{str(self._oauth.base_url).rstrip('/')}/.well-known/oauth-protected-resource/mcp"
        raise HTTPException(
            401, "valid caller bearer required", headers={"WWW-Authenticate": f'Bearer resource_metadata="{metadata}"'}
        ) from None
