"""OAuth-protected MCP server for acceptance-testing MCP OAuth linkage.

The hermetic test variant uses fastmcp's ``InMemoryOAuthProvider``. The deployed variant uses
Dex as the authorization server and only verifies Dex-issued JWTs locally.
"""

from __future__ import annotations

import os
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyHttpUrl, AnyUrl

CLIENT_ID = "agentplane-acceptance-client"
IN_MEMORY_SCOPE = "mcp"
DEX_SCOPE = "openid"
PATH = "/mcp"


def _build_mcp(auth: Any) -> FastMCP:
    # TODO: Check whether an existing MCP image (especially the Everything image) can provide
    # this tool surface while delegating OAuth discovery and token verification to Dex, allowing
    # this custom fixture image and its deployment wiring to be removed.
    mcp = FastMCP("agentplane-oauth-fixture", auth=auth)

    @mcp.tool
    def echo(message: str) -> str:
        return f"Echo: {message}"

    return mcp


def build_app(*, base_url: str, redirect_uri: str) -> FastMCP:
    auth = InMemoryOAuthProvider(base_url=base_url, required_scopes=[IN_MEMORY_SCOPE])
    auth.clients[CLIENT_ID] = OAuthClientInformationFull(
        client_id=CLIENT_ID,
        # Gotcha: the authorize handler parses the redirect_uri query parameter as `AnyUrl`, and
        # compares it against this list with `==`; pydantic's `AnyUrl`/`AnyHttpUrl` do not compare
        # equal even for an identical string, so registering with `AnyHttpUrl` here always 400s.
        redirect_uris=[AnyUrl(redirect_uri)],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
        scope=IN_MEMORY_SCOPE,
    )
    return _build_mcp(auth)


def build_dex_app(*, base_url: str, authorization_server: str, jwks_uri: str, audience: str) -> FastMCP:
    verifier = JWTVerifier(jwks_uri=jwks_uri, issuer=authorization_server, audience=audience, base_url=base_url)
    auth = RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[AnyHttpUrl(authorization_server)],
        base_url=base_url,
        scopes_supported=[DEX_SCOPE],
    )
    return _build_mcp(auth)


if __name__ == "__main__":
    build_dex_app(
        base_url=os.environ["OAUTH_FIXTURE_BASE_URL"],
        authorization_server=os.environ["OAUTH_FIXTURE_AUTHORIZATION_SERVER"],
        jwks_uri=os.environ["OAUTH_FIXTURE_JWKS_URI"],
        audience=os.environ["OAUTH_FIXTURE_AUDIENCE"],
    ).run(transport="http", host="0.0.0.0", port=8080, path=PATH)
