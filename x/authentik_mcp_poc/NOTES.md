# Architecture notes

Deep-dive findings from bringing the POC up end-to-end against the live cluster.
Supersedes the "simple JWT federation" mental model in README.md. The README is
still accurate about the 50000-foot architecture; this file captures the
non-obvious details that only surfaced once things were actually running.

## 1. The POC's original premise

```
claude.ai ─OAuth 2.1+PKCE─▶ MCP server (OIDCProxy wraps Authentik OAuth2 provider)
  user JWT ───▶  MCP server validates ─▶ tool runs
                                       └─▶ forwards user JWT as Bearer to backend
                                               ↓
                                     Authentik proxy provider outpost
                                     (jwt_federation_providers = [mcp_poc OAuth2])
                                               ↓
                                     backend: trusts X-Authentik-* headers the
                                     outpost injects after successful federation
```

The idea was that a single user-issued Authentik JWT would traverse **two
independent Authentik providers**: once the MCP server's `JWTVerifier` validates
it, once more when the proxy outpost validates it via JWT federation. The
outpost would then forward to the backend with the standard Authentik
`X-Authentik-{Username,Email,Groups,Uid}` headers.

## 2. What actually happens with OIDCProxy in the middle

`OIDCProxy` (a subclass of `OAuthProxy`) doesn't pass the upstream Authentik
token through to the MCP client. Concretely, from reading the wheel source
(fastmcp 3.1.0):

`fastmcp/server/auth/oauth_proxy/proxy.py` around line 950-985, after the
upstream token-exchange with Authentik completes:

```python
# Store encrypted upstream token under an opaque id
upstream_token_set = UpstreamTokenSet(
    upstream_token_id=upstream_token_id,
    access_token=idp_tokens["access_token"],      # ← real Authentik JWT
    refresh_token=idp_tokens.get("refresh_token"),
    scope=idp_tokens.get("scope"),
    expires_at=expires_at,
    raw_token_data=idp_tokens,
)
await self._upstream_token_store.put(
    key=upstream_token_id,
    value=upstream_token_set,
    ttl=max(refresh_expires_in or 0, expires_in, 1),
)

# Issue minimal FastMCP access token (just a reference via JTI)
fastmcp_access_token = self.jwt_issuer.issue_access_token(
    client_id=client.client_id,
    scopes=authorization_code.scopes,
    jti=access_jti,
    expires_in=expires_in,
    upstream_claims=upstream_claims,
)
```

The token returned to claude.ai is therefore:

- **Signed by FastMCP's `JWTIssuer`**, whose key is derived from the upstream
  client_secret via `derive_jwt_key` with salt
  `"fastmcp-storage-encryption-key"`.
- **A short-lived JTI reference** — it carries `jti`, `client_id`, `scopes`,
  and an `upstream_claims` blob extracted by
  `_extract_upstream_claims(idp_tokens)`, but it does **not** carry the
  upstream Authentik access token itself.
- **Not verifiable by Authentik's JWKS**. The signing key is different, the
  `iss` claim is the MCP server's `base_url`, not the Authentik issuer.

Handing that token directly to Authentik's proxy outpost fails JWT federation
by definition — the outpost is looking for a signature from the federated
`authentik_provider_oauth2.mcp_poc` key and seeing FastMCP's locally-derived
key instead. That's what produced the 401 "Unauthenticated — Due to 'Receive
header authentication' being set, no redirect is performed" from the outpost
on our first end-to-end test.

## 3. The token swap we missed — FastMCP already did the hard part

The next layer down is what `OAuthProxy.load_access_token` does on the MCP
server side when a tool request arrives. Still in
`fastmcp/server/auth/oauth_proxy/proxy.py`, lines 1384-1450:

```python
async def load_access_token(self, token: str) -> AccessToken | None:
    """Validate FastMCP JWT by swapping for upstream token.

    This implements the token swap pattern:
    1. Verify FastMCP JWT signature (proves it's our token)
    2. Look up upstream token via JTI mapping
    3. Decrypt upstream token
    4. Validate upstream token with provider (GitHub API, JWT validation, etc.)
    5. Return upstream validation result
    """
    ...
    jti_mapping = await self._jti_mapping_store.get(key=jti)
    upstream_token_set = await self._upstream_token_store.get(
        key=jti_mapping.upstream_token_id
    )
    verification_token = self._get_verification_token(upstream_token_set)
    # → upstream_token_set.access_token  (the real Authentik JWT)

    validated = await self._token_validator.verify_token(verification_token)
    ...
    if verification_token != upstream_token_set.access_token:
        validated = validated.model_copy(
            update={
                "token": upstream_token_set.access_token,   # ← swap
                "scopes": upstream_token_set.scope.split()
                if upstream_token_set.scope
                else validated.scopes,
                "expires_at": int(upstream_token_set.expires_at),
            }
        )
    return validated
```

So when MultiAuth calls `proxy.verify_token(fastmcp_jti_token)`:

1. `OAuthProvider.verify_token` delegates to
   `OAuthProxy.load_access_token` (`fastmcp/server/auth/auth.py:646-659`).
2. `load_access_token` verifies the FastMCP signature, looks up the upstream
   token via the JTI store, decrypts it, validates it against the upstream
   JWKS via the configured `TokenVerifier`, and returns an `AccessToken`
   whose **`.token` field is set to the upstream Authentik access token** —
   not the JTI reference.

In other words, FastMCP **already does** the server-side equivalent of an
RFC 8693 token exchange. `get_access_token().token` inside a tool handler is
the real Authentik JWT, signed by `authentik-mcp-poc`, exactly the shape the
proxy outpost's `jwt_federation_providers` wants.

## 4. Where our POC went wrong

`x/authentik_mcp_poc/server.py` `_extract_bearer_token` reads the raw
`Authorization` header off the incoming request and forwards **that** to the
backend:

```python
def _extract_bearer_token() -> str:
    request = get_http_request()
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise RuntimeError(f"expected Bearer token in Authorization header, got {header!r}")
    return token
```

The raw `Authorization` header is the FastMCP JTI reference token (what
claude.ai sent), **not** the upstream Authentik token. We were skipping past
the token swap entirely. That's why the proxy outpost kept rejecting it.

The fix is one function — use `get_access_token()` from
`fastmcp.server.dependencies` instead:

```python
from fastmcp.server.dependencies import get_access_token

def _extract_bearer_token() -> str:
    access = get_access_token()
    if access is None:
        raise RuntimeError("no access token in request context")
    # After OAuthProxy.load_access_token's swap, .token is the upstream
    # Authentik access token — not the JTI reference the raw Authorization
    # header would give us.
    return access.token
```

After this change the Bearer we forward to the backend is Authentik-signed,
`jwt_federation_providers = [mcp_poc]` matches, the outpost forwards to the
backend, and FastAPI sees the real `X-Authentik-*` headers.

## 5. Why we don't need RFC 8693 token exchange

Option (B) from the earlier discussion was: have the tool perform an RFC 8693
token-exchange call to Authentik to mint an audience-bound token from the
upstream token. Turns out that's what `OAuthProxy.load_access_token`
effectively already did server-side — we just weren't using its output.

We **may** still want actual RFC 8693 later for two reasons, but neither is
required for the current POC:

1. **Per-resource audience binding.** The upstream token we get back is
   audience-scoped to `authentik-mcp-poc` (the OAuth2 provider). If we wanted
   the token to be audience-scoped to `authentik-mcp-poc-backend` (the proxy
   provider) to tighten the blast radius, RFC 8693 would be the knob.
2. **Multi-backend fanout.** A tool that needs to call N different services,
   each under its own proxy provider, would need N audience-specific tokens.
   RFC 8693 lets us spawn those on demand without N separate OAuth flows.

Authentik does support RFC 8693 token exchange natively (since 2024.10), so
either extension is mechanically feasible — but we should only reach for it
when a real use case shows up, not speculatively.

## 6. Open questions after this fix lands

The fix is "one line in `server.py`". Expected post-fix behavior:

- `whoami_via_backend` tool forwards the real Authentik access token.
- Proxy outpost's `jwt_federation_providers` accepts it.
- Outpost decodes user identity from the token and rewrites to `internal_host`
  with `X-Authentik-{Username,Email,Groups,Uid}` headers set.
- FastAPI backend's `/whoami` returns those headers echoed as JSON plus the
  `secret_message`.

Things to verify once it rolls out:

- Token audience/scope on the upstream token includes whatever the outpost
  enforces. `authentik_provider_proxy.mcp_poc_backend` doesn't declare
  `required_scopes` — should be OK — but if the outpost has a minimum-scope
  check we haven't configured, we'd get another 401 with a different body.
- The `upstream_claims` embedded in the FastMCP token still contains enough
  info for the `get_access_token().claims` path the tool might use for
  logging. Not load-bearing for the call path, but worth checking.
- Token lifetime. OAuthProxy aligns the FastMCP token TTL with the upstream
  expiry, so claude.ai should refresh in sync with the underlying Authentik
  token. The refresh flow (`handle_refresh_token`, lines 1150-1200) also
  re-issues the FastMCP token; the tool code doesn't care which generation it
  is.

## 7. References

- fastmcp 3.1.0 wheel, `fastmcp/server/auth/oauth_proxy/proxy.py`:
  - `load_access_token` token-swap pattern — lines 1384-1450.
  - Upstream token storage on /token handler — lines 895-985.
  - Refresh flow (aligns FastMCP TTL with upstream) — lines 1150-1200.
- fastmcp 3.1.0 wheel, `fastmcp/server/auth/auth.py`:
  - `OAuthProvider.verify_token` → `load_access_token` delegation — lines 646-659.
  - `MultiAuth.verify_token` iterates `server` first, then `verifiers` — lines 537-556.
- mcp 1.27.0 wheel, `mcp/server/auth/provider.py`:
  - `AccessToken` model — `.token: str` is a plain string field, set by
    OAuthProxy to the upstream Authentik access token after the swap.
- fastmcp 3.1.0 wheel, `fastmcp/server/dependencies.py`:
  - `get_access_token()` — returns the `AccessToken` from request scope, so
    the swap result is what the tool sees — lines 469-540.
- Authentik RFC 8693 support — release notes for 2024.10 add
  `urn:ietf:params:oauth:grant-type:token-exchange` on the `/application/o/token/`
  endpoint. Not used by this POC (see §5) but available for future extensions.
