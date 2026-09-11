"""Self-contained OAuth-protected MCP server for acceptance-testing MCP OAuth linkage.

Combines an in-memory OAuth 2.1 authorization server (fastmcp's ``InMemoryOAuthProvider``) with
one streamable-HTTP MCP tool, so the whole OAuth dance -- protected-resource/authorization-server
discovery, PKCE authorize, token exchange -- happens against this one process. There is no
external identity provider to stand up: a single client is pre-registered at startup (dynamic
client registration would mint an unpredictable client_id, and Agentplane's ``McpOAuthServer``
configuration needs a fixed one), with the token exchange requiring no client secret (PKCE only).
"""

from __future__ import annotations

import os

from fastmcp import FastMCP
from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

CLIENT_ID = "agentplane-acceptance-client"
SCOPE = "mcp"
PATH = "/mcp"


def build_app(*, base_url: str, redirect_uri: str) -> FastMCP:
    auth = InMemoryOAuthProvider(base_url=base_url, required_scopes=[SCOPE])
    auth.clients[CLIENT_ID] = OAuthClientInformationFull(
        client_id=CLIENT_ID,
        # Gotcha: the authorize handler parses the redirect_uri query parameter as `AnyUrl`, and
        # compares it against this list with `==`; pydantic's `AnyUrl`/`AnyHttpUrl` do not compare
        # equal even for an identical string, so registering with `AnyHttpUrl` here always 400s.
        redirect_uris=[AnyUrl(redirect_uri)],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
        scope=SCOPE,
    )
    mcp = FastMCP("agentplane-oauth-fixture", auth=auth)

    @mcp.tool
    def echo(message: str) -> str:
        return f"Echo: {message}"

    return mcp


if __name__ == "__main__":
    build_app(base_url=os.environ["OAUTH_FIXTURE_BASE_URL"], redirect_uri=os.environ["OAUTH_FIXTURE_REDIRECT_URI"]).run(
        transport="http", host="0.0.0.0", port=8080, path=PATH
    )
