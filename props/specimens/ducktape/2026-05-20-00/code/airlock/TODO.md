# Airlock TODOs

## OIDCProxy follow-ups

MCP OAuth via `MultiAuth(OIDCProxy + JWTVerifier)` is implemented. Remaining items:

### Per-user isolation

Track the JWT `sub` claim on actions for multi-user separation. Currently only `client_id`
is stored. When multiple Authentik users access airlock, their actions should be scoped
so each user only sees their own. Low priority while only one user (agentydragon) exists.

### Client identity in Svelte UI

Show `client_id` on actions in the operator SPA. The field is stored in the DB and
returned by the REST API — the frontend just needs to render it.

### Well-known protected resource metadata

`/.well-known/oauth-protected-resource` returns 404 under the `/mcp` mount. The ASM
endpoint (`/.well-known/oauth-authorization-server`) works. Investigate whether this is
a FastMCP routing issue or if the path needs to be different. Claude.ai may not need it
(it follows the `resource_metadata` URL from the 401 `WWW-Authenticate` header).

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
