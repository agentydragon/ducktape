# Airlock TODOs

## MCP OAuth spec compliance + Dynamic Client Registration

Make Airlock a compliant MCP resource server per the MCP authorization spec and RFC 9728.

### Why deferred

Authentik does not support RFC 7591 Dynamic Client Registration — no `registration_endpoint`
is advertised in its OIDC discovery document. Without DCR, interactive MCP clients like
Claude Code on the web cannot self-register and must use pre-provisioned `client_id` values.

### What full implementation would require

1. **`/.well-known/oauth-protected-resource`** (RFC 9728): add a new `Route` in `app.py`
   returning JSON with `resource`, `authorization_servers` (pointing at the Authentik
   provider's issuer URL), `bearer_methods_supported`, and `scopes_supported`.

2. **`WWW-Authenticate` on 401**: add a Starlette middleware that adds
   `WWW-Authenticate: Bearer realm="airlock", resource_metadata="<base_url>/.well-known/oauth-protected-resource"`
   to all 401 responses, enabling clients to auto-discover the auth server.

3. **DCR proxy endpoint** (the hard part): implement `POST /oauth/register` in Airlock
   that calls the Authentik admin API (`/api/v3/providers/oauth2/` + `/api/v3/core/applications/`)
   to dynamically create an OAuth2 provider+application, then returns a standard RFC 7591
   registration response (`client_id`, `redirect_uris`, etc.). Requires Airlock to hold an
   Authentik API token (Vault → ESO → env var). Optionally implement cleanup of
   stale registrations via background task or TTL-based GC.

4. **`authorization_servers` update**: once the DCR proxy is live, the
   `authorization_servers` field in the resource metadata should list Airlock's own
   DCR endpoint (or a dedicated sub-issuer), not just Authentik directly.

5. **JWTVerifier multi-issuer support**: if the DCR proxy creates providers under
   different Authentik issuers, `JWTVerifier` may need to accept tokens from multiple
   issuers (or rely on JWKS key matching alone).

### Pre-provisioned fallback (simpler, no DCR)

If DCR is not needed (e.g. Claude Code is always configured with a fixed `client_id`),
items 1 and 2 above alone provide MCP spec compliance for discovery. Add an Authentik
OAuth2 provider (`airlock-human`, public, PKCE, `propose read` or `propose read decide`
scopes) via a new blueprint in `cluster/k8s/authentik/blueprints/`.

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
