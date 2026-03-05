# Airlock TODOs

## MCP OAuth spec compliance + Dynamic Client Registration

Make Airlock a compliant MCP resource server per the MCP authorization spec and RFC 9728.

### Why deferred

Authentik does not support RFC 7591 Dynamic Client Registration — no `registration_endpoint`
is advertised in its OIDC discovery document. Without DCR, interactive MCP clients like
Claude Code on the web cannot self-register and must use pre-provisioned `client_id` values.

### FastMCP already has the building blocks

FastMCP 3.0.2 ships `OIDCProxy` (`fastmcp.server.auth.oidc_proxy.OIDCProxy`) which is
purpose-built for this exact situation: it acts as a local OAuth authorization server that
accepts DCR from clients and proxies the actual auth flow to an upstream OIDC provider
(Authentik). It also auto-provides `/.well-known/oauth-authorization-server` and
`/.well-known/oauth-protected-resource` endpoints (RFC 8414 / RFC 9728).

The implementation is therefore much simpler than building a DCR proxy from scratch.

### What full implementation would require

1. **Replace `JWTVerifier` with `OIDCProxy`** in `app.py`:

   ```python
   from fastmcp.server.auth.oidc_proxy import OIDCProxy

   # Instead of:
   auth = JWTVerifier(jwks_uri=discovery["jwks_uri"])

   # Use:
   auth = OIDCProxy(
       upstream=settings.oidc_issuer,   # Authentik provider URL
       issuer=settings.public_base_url, # Airlock itself becomes the OAuth AS
       # ... scope mappings, client storage, etc.
   )
   ```

   `OIDCProxy` accepts DCR from clients (e.g. Claude Code), bridges the auth code + PKCE
   flow to Authentik, issues its own short-lived JWTs, and handles token refresh. The
   `/.well-known/oauth-protected-resource` and `/.well-known/oauth-authorization-server`
   endpoints are exposed automatically.

2. **Remove the manual OIDC discovery fetch** from `app.py` (`_fetch_oidc_discovery`) —
   `OIDCProxy` handles upstream discovery internally.

3. **Client storage**: `OIDCProxy` needs a store for dynamically registered clients. Use
   an in-memory store for simplicity or a SQLite-backed store (reuse `airlock.db` or a
   separate file) for persistence across restarts.

4. **Scope mapping**: configure `OIDCProxy` to map Authentik's upstream scopes
   (`propose`, `decide`, `read`) through to the tokens it issues, so `require_scopes()`
   in `proxy_server.py` continues to work unchanged.

5. **OpenClaw auth proxy**: the `auth_proxy/` sidecar currently fetches tokens directly
   from Authentik via `client_credentials`. If Airlock's issuer changes to itself, the
   sidecar's `TOKEN_URL` must point at Airlock's token endpoint instead. Alternatively,
   keep `JWTVerifier` as a second verifier (FastMCP supports `MultiAuth`) so both
   Authentik-issued and OIDCProxy-issued tokens are accepted during transition.

### Pre-provisioned fallback (simpler, no DCR)

If DCR is not needed (e.g. Claude Code is always configured with a fixed `client_id`),
skip `OIDCProxy` and just add:

- An Authentik OAuth2 provider (`airlock-human`, public, PKCE, `propose read` scopes)
  via a new blueprint in `cluster/k8s/authentik/blueprints/`.
- A `/.well-known/oauth-protected-resource` `Route` in `app.py` pointing at Authentik.

## Capability token grant system

Allow agents to request temporary or permanent capability grants, which are approved
by the human operator and encoded as tokens the agent can present on subsequent calls.

### Flow

1. Agent calls a special airlock tool (e.g. `airlock_request_grant`) with a description
   of the capability it wants (e.g. "run commands matching `kubectl get *`").
2. Airlock queues the grant request for operator approval (same UI as normal actions).
3. Operator approves → airlock issues a signed token encoding the granted capability.
4. Agent receives the token and presents it in future tool calls (e.g. via a
   `grant_token` parameter or a dedicated header/field).
5. Predicate/policy layer checks the token and returns `Approved` without requiring
   another human decision.

### Token design options

- **Signed JWT claims**: Token is a JWT signed with an airlock server secret.
  Claims encode the capability scope (namespace, tool pattern, argument constraints),
  issuing time, and optional expiry. Stateless — airlock just verifies signature + claims.
- **Stateful grant records**: Airlock stores grant records in the DB (like actions).
  Token is an opaque ID referencing the record. Supports explicit revocation.
- **Hybrid**: JWT with a JTI claim; DB stores revoked JTIs for revocation support.

### Capability scope representation

Grants should be able to express:

- Which backend namespace(s) and tool(s) the grant applies to.
- Optional argument-level constraints (e.g. read-only commands, specific paths).
- Temporal constraints: expiry time, or "one-shot" (consumed on first use).
- Optional human-readable label shown in the approval UI.

### Predicate integration

The predicate function (or a new pre-predicate hook) receives the presented grant
token alongside `(server_namespace, tool_name, arguments)` and can return `Approved`
if the token matches and is valid. The existing `NeedsHumanDecision` / `Denied` paths
are unchanged for calls without a valid token.

### Implementation sketch

- New DB table `grants` (id, created_at, expires_at, scope_json, revoked).
- New MCP tool `airlock_request_grant(description, scope)` exposed to agents.
- `proxy_server.py`: extract optional `grant_token` from tool call arguments before
  forwarding; validate token against DB / JWT signature; short-circuit to `Approved`
  if valid.
- Frontend: grant requests appear in the action queue; approval issues the token and
  returns it to the waiting agent call.
- Config: `grant_signing_secret` (for JWT mode) or toggle between stateful/JWT modes.
