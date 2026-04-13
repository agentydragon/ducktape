"""FastMCP server for the Authentik MCP POC.

Wires OIDCProxy + JWTVerifier against Authentik (see `_build_auth` for the
exact shape) and exposes a single tool, `whoami_via_backend`, that forwards
the caller's Bearer token to the proxy-outpost-protected whoami backend.

See <x/authentik_mcp_poc/README.md> for the end-to-end flow.
"""

from __future__ import annotations

import logging
import sys

import httpx
import uvicorn
from fastmcp import FastMCP
from fastmcp.server.auth import MultiAuth
from fastmcp.server.auth.auth import AuthProvider
from fastmcp.server.auth.oidc_proxy import OIDCProxy
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token

from x.authentik_mcp_poc.config import ServerSettings

logger = logging.getLogger(__name__)


def _build_auth(settings: ServerSettings) -> AuthProvider:
    """OIDCProxy (for DCR + MCP OAuth endpoints) + JWTVerifier (for tool calls).

    Modeled on airlock/app.py::_build_auth, with one important difference:
    airlock wraps FastMCP under FastAPI and `app.mount("/mcp", mcp_app)`s it,
    so airlock's FastMCP internal path is "/" and `base_url` includes "/mcp"
    (the FastAPI mount adds the prefix externally). We serve uvicorn directly
    on `mcp.http_app(path="/mcp")`, so FastMCP's internal path IS "/mcp" and
    `base_url` must NOT include "/mcp" — otherwise:

      - `_get_resource_url(mcp_path)` doubles to `<base_url>/mcp/mcp`
      - AS metadata `authorization_endpoint` becomes `<base_url>/authorize` =
        `https://server/mcp/authorize`, but the actual route is at root
        `/authorize` (FastMCP mounts auth routes flat, not under streamable_http_path)

    With `base_url = settings.public_base_url` (no /mcp), both the resource
    URL and the OAuth endpoint URLs collapse to the right thing.
    """
    issuer = settings.normalized_issuer()
    proxy = OIDCProxy(
        config_url=f"{issuer}/.well-known/openid-configuration",
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        base_url=settings.normalized_public_base_url(),
        require_authorization_consent=True,
    )
    # OIDCProxy's DCR endpoint rejects scopes it doesn't know about; allow the
    # standard OIDC scopes the TF module registers on the Authentik provider.
    assert proxy.client_registration_options is not None
    proxy.client_registration_options.valid_scopes = ["openid", "email", "profile"]
    return MultiAuth(server=proxy, verifiers=[JWTVerifier(jwks_uri=f"{issuer}/.well-known/jwks", issuer=issuer)])


def _extract_bearer_token() -> str:
    """Return the upstream Authentik access token for the current request.

    Earlier versions of this function read the raw `Authorization` header off
    the request, which gave us the FastMCP JTI-reference token (signed by
    OIDCProxy's derived key, not by the Authentik OAuth2 provider). Forwarded
    to the Authentik proxy outpost, that token fails JWT federation by
    definition and the outpost returns 401 "Receive header authentication".

    `OAuthProxy.load_access_token` already performs a server-side token swap
    (see <NOTES.md> §3 for the full trace) that verifies the JTI reference,
    looks up the upstream Authentik token in its encrypted store, validates
    it, and populates `AccessToken.token` with the upstream access token.
    `get_access_token().token` gives us exactly that — the Authentik-signed
    JWT that `jwt_federation_providers` will accept.
    """
    access = get_access_token()
    if access is None:
        raise RuntimeError("no authenticated access token in request context")
    return access.token


def build_server(settings: ServerSettings) -> FastMCP:
    mcp: FastMCP = FastMCP(
        name="Authentik MCP POC",
        instructions=(
            "POC MCP server for Authentik-authenticated remote MCP. "
            "Call whoami_via_backend to see your identity flow through an "
            "Authentik proxy outpost to a downstream service."
        ),
        auth=_build_auth(settings),
    )

    @mcp.tool
    async def whoami_via_backend() -> dict[str, object]:
        """Forward the caller's Authentik JWT to the whoami backend and return its response.

        The backend sits behind an Authentik Proxy Provider. This tool proves
        the Bearer token received by this MCP server is also accepted by a
        second, independently-configured Authentik-protected service — i.e.,
        that the user identity really flows end-to-end through Authentik's
        JWT federation, not just through this server's own auth layer.
        """
        token = _extract_bearer_token()
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{settings.backend_url.rstrip('/')}/whoami",
                headers={"Authorization": f"Bearer {token}"},
                follow_redirects=False,
            )
        return {
            "backend_status": response.status_code,
            "backend_url": str(response.request.url),
            "backend_response": response.json()
            if response.headers.get("content-type", "").startswith("application/json")
            else response.text,
        }

    return mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    settings = ServerSettings()
    mcp = build_server(settings)
    # path="/mcp" matches both OIDCProxy's base_url (settings.public_base_url + "/mcp")
    # and the Deployment's advertised remote MCP URL. Leaving this unset works too
    # (FastMCP's streamable_http_path defaults to "/mcp") but we're explicit so the
    # routing is visible at the call site.
    app = mcp.http_app(path="/mcp")
    logger.info("authentik-mcp-poc listening on %s:%d", settings.host, settings.port)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
