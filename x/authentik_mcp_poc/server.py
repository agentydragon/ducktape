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
from fastmcp.server.dependencies import get_http_request

from x.authentik_mcp_poc.config import ServerSettings

logger = logging.getLogger(__name__)


def _build_auth(settings: ServerSettings) -> AuthProvider:
    """OIDCProxy (for DCR + MCP OAuth endpoints) + JWTVerifier (for tool calls).

    Modeled on airlock/app.py::_build_auth.
    """
    issuer = settings.normalized_issuer()
    proxy = OIDCProxy(
        config_url=f"{issuer}/.well-known/openid-configuration",
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        base_url=f"{settings.normalized_public_base_url()}/mcp",
        require_authorization_consent=True,
    )
    # OIDCProxy's DCR endpoint rejects scopes it doesn't know about; allow the
    # standard OIDC scopes the TF module registers on the Authentik provider.
    assert proxy.client_registration_options is not None
    proxy.client_registration_options.valid_scopes = ["openid", "email", "profile"]
    return MultiAuth(server=proxy, verifiers=[JWTVerifier(jwks_uri=f"{issuer}/.well-known/jwks", issuer=issuer)])


def _extract_bearer_token() -> str:
    """Return the raw JWT from the current request's Authorization header.

    FastMCP has already validated the token via JWTVerifier by the time the
    tool body runs, so we don't re-validate here; we just need to forward it
    to the backend unchanged so the Authentik outpost can validate it against
    `jwt_federation_providers`.
    """
    request = get_http_request()
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise RuntimeError(f"expected Bearer token in Authorization header, got {header!r}")
    return token


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
    app = mcp.http_app(path="/")
    logger.info("authentik-mcp-poc listening on %s:%d", settings.host, settings.port)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
