"""FastMCP server for auth-aware Grocy.

Authenticates remote MCP clients via Authentik (OIDCProxy) and then uses the
caller's identity to drive Grocy's REST API, whose ingress sits behind an
Authentik proxy provider outpost.

This is deliberately a thin wrapper: `FastMCP.from_openapi` generates the
tool surface directly from Grocy's OpenAPI 3.1 spec. The only custom wiring
is `AuthentikExchangeAuth` (an `httpx.Auth` subclass that swaps the MCP
user's upstream Authentik JWT for a Grocy-proxy-scoped JWT per request) and
a pair of `RouteMap`s that filter the generated tool set down to the
inventory bootstrap surface (`/objects/*` + `/stock/*`).

See <x/grocy_mcp/README.md> for the architecture and end-to-end flow;
<x/authentik_mcp_poc/NOTES.md> §5-§6 for why each piece of the token
exchange is load-bearing.
"""

from __future__ import annotations

import json
import logging
import sys
from importlib.resources import files

import httpx
import uvicorn
from fastmcp import FastMCP
from fastmcp.server.auth import MultiAuth
from fastmcp.server.auth.auth import AuthProvider
from fastmcp.server.auth.oidc_proxy import OIDCProxy
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.providers.openapi import MCPType, RouteMap

from x.grocy_mcp.auth import AuthentikExchangeAuth
from x.grocy_mcp.config import ServerSettings

logger = logging.getLogger(__name__)


# Route filter: include everything the inventory bootstrap needs, exclude
# everything else explicitly so a Grocy spec refresh can't silently add
# /recipes or /chores tools. First match wins.
ROUTE_MAPS = [
    # Generic object CRUD: /objects/{entity}, /objects/{entity}/{objectId}.
    # Covers products, locations, quantity_units, product_groups, and any
    # other entity the LLM might need for inventory bootstrap.
    RouteMap(pattern=r"^/objects(/.*)?$", mcp_type=MCPType.TOOL),
    # Stock manipulation: /stock, /stock/products/{id}/{add,consume,...}.
    RouteMap(pattern=r"^/stock(/.*)?$", mcp_type=MCPType.TOOL),
    # Everything else is explicitly out.
    RouteMap(pattern=r".*", mcp_type=MCPType.EXCLUDE),
]


def _build_auth(settings: ServerSettings) -> AuthProvider:
    """OIDCProxy (DCR + MCP OAuth endpoints) + JWTVerifier (tool calls).

    Identical to <x/authentik_mcp_poc/server.py>'s `_build_auth`; see that
    module's docstring for the `base_url` (must not include `/mcp`) gotcha.
    """
    issuer = settings.normalized_issuer()
    proxy = OIDCProxy(
        config_url=f"{issuer}/.well-known/openid-configuration",
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        base_url=settings.normalized_public_base_url(),
        require_authorization_consent=True,
    )
    # OIDCProxy's DCR endpoint rejects scopes it doesn't know about; allow
    # the standard OIDC scopes the TF module registers on the Authentik
    # provider.
    assert proxy.client_registration_options is not None
    proxy.client_registration_options.valid_scopes = ["openid", "email", "profile"]
    return MultiAuth(server=proxy, verifiers=[JWTVerifier(jwks_uri=f"{issuer}/.well-known/jwks", issuer=issuer)])


def _strip_empty_enums(node: object) -> None:
    """Drop empty `enum: []` keys recursively.

    Grocy's OpenAPI spec (4.6.0) declares at least one schema with
    `"enum": []` — `ExposedEntityEditRequiresAdmin`. Empty enums are
    invalid per the OpenAPI spec, and FastMCP's pydantic-based parser
    rejects the whole document with `too_short`. Fall back to the
    plain `type: string` the schema already declares.
    """
    if isinstance(node, dict):
        if isinstance(node.get("enum"), list) and not node["enum"]:
            del node["enum"]
        for value in node.values():
            _strip_empty_enums(value)
    elif isinstance(node, list):
        for item in node:
            _strip_empty_enums(item)


def _load_openapi_spec() -> dict[str, object]:
    """Return the checked-in Grocy OpenAPI 3.1 spec as a dict."""
    spec: dict[str, object] = json.loads(files("x.grocy_mcp").joinpath("grocy_openapi.json").read_text())
    _strip_empty_enums(spec)
    return spec


def build_mcp(settings: ServerSettings) -> FastMCP:
    """Build the FastMCP instance with generated Grocy tools, but no auth.

    Split out from `build_server` so tests can exercise the OpenAPI →
    tool-surface wiring without FastMCP's `OIDCProxy` trying to reach out
    to Authentik for `.well-known/openid-configuration` at construction
    time (which fails on hermetic RBE test workers).
    """
    spec = _load_openapi_spec()

    # Separate clients by design — see AuthentikExchangeAuth docstring.
    exchange_client = httpx.AsyncClient(timeout=10.0)
    grocy_client = httpx.AsyncClient(
        base_url=f"{settings.grocy_url.rstrip('/')}/api",
        auth=AuthentikExchangeAuth(settings, exchange_client),
        timeout=30.0,
    )

    return FastMCP.from_openapi(openapi_spec=spec, client=grocy_client, name="Grocy", route_maps=ROUTE_MAPS)


def build_server(settings: ServerSettings) -> FastMCP:
    mcp = build_mcp(settings)
    mcp.auth = _build_auth(settings)
    return mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    settings = ServerSettings()
    mcp = build_server(settings)
    app = mcp.http_app(path="/mcp")
    logger.info("grocy-mcp listening on %s:%d", settings.host, settings.port)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
