"""FastMCP server for the Authentik MCP POC.

Wires OIDCProxy + JWTVerifier against Authentik (see `_build_auth` for the
exact shape) and exposes a single tool, `whoami_via_backend`, that forwards
the caller's Bearer token to the proxy-outpost-protected whoami backend.

See <x/authentik_mcp_poc/README.md> for the end-to-end flow.
"""

from __future__ import annotations

import base64
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

    `OAuthProxy.load_access_token` performs a server-side swap (see
    <NOTES.md> §3) that gives us the Authentik-signed JWT that was issued
    by our user-login OAuth2 provider. `get_access_token().token` is that
    token — NOT the FastMCP JTI reference token the raw `Authorization`
    header would contain.

    On its own, this token is NOT what the backend outpost wants: the
    outpost's introspection is scoped to the backend's OWN OAuth2 provider
    (see <NOTES.md> §5). We use this token as the `client_assertion` in a
    JWT-bearer token exchange to mint a new token the outpost will accept —
    see `_exchange_token_for_backend`.
    """
    access = get_access_token()
    if access is None:
        raise RuntimeError("no authenticated access token in request context")
    token = access.token
    _log_token_shape("upstream user token", token)
    return token


async def _exchange_token_for_backend(user_token: str, settings: ServerSettings, client: httpx.AsyncClient) -> str:
    """Trade the user's upstream token for one the backend outpost will accept.

    Background: Authentik's proxy outpost validates incoming Bearer tokens
    via RFC 7662 introspection, scoped to the outpost's OWN client_id
    (i.e., the backend proxy provider's auto-generated OAuth2 client). So
    the outpost only accepts tokens issued by the BACKEND provider — not
    tokens from a different provider, even one listed in the backend
    provider's `jwt_federation_providers`. See <NOTES.md> §5 for the full
    trace through `authentik/providers/oauth2/views/introspection.py`.

    What `jwt_federation_providers` IS for: the `/application/o/token/`
    endpoint's `__post_init_client_credentials_jwt` path (RFC 7521
    client-credentials with `client_assertion_type=...:jwt-bearer`). The
    backend provider accepts JWT assertions signed by any of its federated
    providers and, on success, MINTS A NEW ACCESS TOKEN scoped to the
    backend provider. That new token IS stored as an `AccessToken` row
    with `provider=<backend_provider>`, so the outpost's introspection
    (filtered by that same provider) finds and accepts it.

    The exchange:

        POST /application/o/token/
            grant_type         = client_credentials
            client_id          = <backend_proxy_provider_client_id>
            client_assertion_type = urn:ietf:params:oauth:client-assertion-type:jwt-bearer
            client_assertion   = <user's upstream Authentik JWT>
            scope              = openid

    The user identity is preserved because Authentik uses the federated
    token's `user` field when minting the new token.
    """
    response = await client.post(
        settings.authentik_token_endpoint(),
        data={
            "grant_type": "client_credentials",
            "client_id": settings.backend_oidc_client_id,
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": user_token,
            "scope": "openid",
        },
    )
    content_type = response.headers.get("content-type")
    body_preview = response.text[:500] + ("..." if len(response.text) > 500 else "")
    if response.status_code != 200:
        raise RuntimeError(
            f"token exchange failed: status={response.status_code} content_type={content_type!r} body={body_preview!r}"
        )
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "token exchange returned non-JSON: "
            f"status={response.status_code} content_type={content_type!r} body={body_preview!r}"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("access_token"), str):
        raise RuntimeError(f"token exchange response missing access_token: {payload!r}")
    backend_token: str = payload["access_token"]
    _log_token_shape("backend-scoped token", backend_token)
    return backend_token


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _log_token_shape(label: str, token: str) -> None:
    """Decode a JWT's header + payload (no signature verification) and log key claims.

    DEBUG-level diagnostics for development — logs iss/aud/sub/jti/scope so
    we can tell which token we're forwarding during bringup. Kept around
    because this POC has already bit us twice in exactly this spot (see
    <NOTES.md> §2 and §3). Production operators should not enable DEBUG
    for this logger unless they explicitly want identity metadata in logs.
    """
    if not logger.isEnabledFor(logging.DEBUG):
        return
    parts = token.split(".")
    if len(parts) < 2:
        logger.debug("%s: non-JWT token (%d parts)", label, len(parts))
        return
    try:
        header = json.loads(_b64url_decode(parts[0]))
        payload = json.loads(_b64url_decode(parts[1]))
    except (ValueError, json.JSONDecodeError) as exc:
        logger.debug("%s: could not decode JWT: %s", label, exc)
        return
    logger.debug(
        "%s: header=%s iss=%r aud=%r sub=%r jti=%r scope=%r",
        label,
        header,
        payload.get("iss"),
        payload.get("aud"),
        payload.get("sub"),
        payload.get("jti"),
        payload.get("scope"),
    )


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
        """Call the Authentik-proxy-protected whoami backend as the current user.

        Flow (see <NOTES.md> §5 for why this is more complicated than it looks):

        1. Pull the user's upstream Authentik token out of FastMCP's request
           context (already swapped from the FastMCP JTI reference by
           `OAuthProxy.load_access_token`).
        2. Exchange that token for one scoped to the backend's Authentik
           proxy provider via an RFC 7521 JWT-bearer client-credentials
           grant — this is what `jwt_federation_providers` actually enables.
        3. Forward the new, backend-scoped token to `/whoami`. The outpost's
           RFC 7662 introspection finds it (same provider), accepts it,
           rewrites to `internal_host`, and injects the `X-Authentik-*`
           identity headers the backend echoes back.

        The tool returns the backend's response plus the HTTP status so the
        user can see the exact wire-level behaviour.
        """
        user_token = _extract_bearer_token()
        async with httpx.AsyncClient(timeout=10.0) as client:
            backend_token = await _exchange_token_for_backend(user_token, settings, client)
            response = await client.get(
                f"{settings.backend_url.rstrip('/')}/whoami",
                headers={"Authorization": f"Bearer {backend_token}"},
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
