"""Dex-backed OAuth-protected MCP server for system-testing MCP OAuth linkage.

The Action Service holds the OAuth client credentials; this server needs only issuer, audience and
JWKS configuration.
"""

from __future__ import annotations

import os
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import AnyHttpUrl

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
