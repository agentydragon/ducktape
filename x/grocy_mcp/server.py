"""FastMCP server for auth-aware Grocy.

Authenticates remote MCP clients via Authentik (OIDCProxy) and then uses the
caller's identity to drive Grocy's REST API, whose ingress sits behind an
Authentik proxy provider outpost.

This is deliberately a thin wrapper: `FastMCP.from_openapi` generates the
tool surface directly from Grocy's OpenAPI 3.1 spec. The only custom wiring
is `AuthentikExchangeAuth` (an `httpx.Auth` subclass that swaps the MCP
user's upstream Authentik JWT for a Grocy-proxy-scoped JWT per request) and
`TOOL_OVERRIDES` which controls naming, descriptions, enablement, and whether
a route is exposed as a tool or an MCP resource.

See <x/grocy_mcp/README.md> for the architecture and end-to-end flow;
<x/authentik_mcp_poc/NOTES.md> §5-§6 for why each piece of the token
exchange is load-bearing.
"""

from __future__ import annotations

import json
import logging
import sys

import httpx
import uvicorn
from fastmcp import FastMCP
from fastmcp.server.auth import MultiAuth
from fastmcp.server.auth.auth import AuthProvider
from fastmcp.server.auth.oidc_proxy import OIDCProxy
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.providers.openapi import MCPType, OpenAPIResource, OpenAPIResourceTemplate, OpenAPITool, RouteMap
from fastmcp.utilities.openapi import HTTPRoute
from pydantic.networks import AnyUrl

from util.bazel.runfiles import get_required_path
from x.grocy_mcp.auth import AuthentikExchangeAuth
from x.grocy_mcp.config import ServerSettings
from x.grocy_mcp.tool_metadata import TOOL_OVERRIDES

logger = logging.getLogger(__name__)

# All routes become tools by default; TOOL_OVERRIDES controls which are
# enabled, disabled, or promoted to MCP resources.
ROUTE_MAPS = [RouteMap(pattern=r".*", mcp_type=MCPType.TOOL)]


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
    assert proxy.client_registration_options is not None
    proxy.client_registration_options.valid_scopes = ["openid", "email", "profile"]
    return MultiAuth(server=proxy, verifiers=[JWTVerifier(jwks_uri=f"{issuer}/.well-known/jwks", issuer=issuer)])


def _load_openapi_spec() -> dict[str, object]:
    """Return the pre-fixed Grocy OpenAPI 3.1 spec.

    The raw upstream spec has issues (empty enums, dangling $refs for
    entity path parameters) that are fixed at build time by the
    :grocy_openapi_fixed genrule. See <fix_openapi_spec.py>.
    """
    spec_path = get_required_path("_main/x/grocy_mcp/grocy.openapi.fixed.json")
    result: dict[str, object] = json.loads(spec_path.read_text())
    return result


def build_mcp(settings: ServerSettings, *, client: httpx.AsyncClient | None = None) -> FastMCP:
    """Build the FastMCP instance with generated Grocy tools, but no auth.

    Args:
        settings: Server configuration.
        client: Optional pre-configured httpx client for Grocy API calls.
            If not provided, creates an Authentik-exchange-wrapped client
            (production path). Tests can pass a plain client to bypass auth.
    """
    spec = _load_openapi_spec()

    if client is None:
        exchange_client = httpx.AsyncClient(timeout=10.0)
        client = httpx.AsyncClient(
            base_url=f"{settings.grocy_url.rstrip('/')}/api",
            auth=AuthentikExchangeAuth(settings, exchange_client),
            timeout=30.0,
        )

    def _filter_disabled(route: HTTPRoute, mcp_type: MCPType) -> MCPType | None:
        override = TOOL_OVERRIDES.get((route.method, route.path))
        if override is not None and not override.enabled:
            return MCPType.EXCLUDE
        if override is not None and override.resource:
            return MCPType.RESOURCE
        return mcp_type

    def _customize_component(
        route: HTTPRoute, component: OpenAPITool | OpenAPIResource | OpenAPIResourceTemplate
    ) -> None:
        override = TOOL_OVERRIDES.get((route.method, route.path))
        if override is None:
            raise ValueError(
                f"No tool override for {route.method} {route.path} — add it to TOOL_OVERRIDES in tool_metadata.py"
            )
        component.name = override.name
        if isinstance(component, OpenAPIResource):
            component.uri = AnyUrl(f"resource://{override.name}")
        if override.extra_description:
            component.description = f"{component.description}\n\n{override.extra_description}"

    return FastMCP.from_openapi(
        openapi_spec=spec,
        client=client,
        name="Grocy",
        route_maps=ROUTE_MAPS,
        route_map_fn=_filter_disabled,
        mcp_component_fn=_customize_component,
    )


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
