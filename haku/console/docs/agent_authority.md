# Canonical Agent authority and enrollment

The durable Postgres graph behind `/mcp`, and the ceremony that admits an interactive OAuth client
to it. The console-side summary is <../README.md> § MCP server (`/mcp`); this is the contract in
full. Code: `agents/` (domain and routes), `mcp_agent_auth.py` (admission), `mcp_auth/`
(the FastMCP composition adapter).

Alembic revision `0010` is the single forward-only database baseline. It directly installs one
graph shared by interactive OAuth and configured static Agents:

```text
Operator -> IdentityAnchor -> OidcIdentity
Operator -> Agent -> AgentNameReservation
Agent -> CredentialBinding -> AuthorizationGrant | StaticCredential
ToolCallPrincipal -> exactly one of operator_id | binding_id
```

## The durable contract

- `Operator`, `Agent`, and every authority-bearing relationship use local immutable UUIDs. An exact
  verified `(issuer, subject)` identifies an OIDC identity; a configured Authentik trust-domain
  anchor is the only path by which identities at the browser and MCP issuers converge on one
  Operator. Username is presentation only.
- OAuth `client_id` describes client software/registration metadata. It is never the Agent,
  Operator, grant, or credential binding, and unauthenticated DCR does not create an Agent.
- Every Agent has a required normalized non-empty display name, globally unique by its normalized
  key. The name is presentation owned by the canonical Agent; it is never a credential or durable
  identity key.
- A `CredentialBinding` owns credential kind, generation, predecessor, and lifecycle. An OAuth
  `AuthorizationGrant` owns client software, authorizing identity, scopes, and token-family
  evidence. Static rotation and OAuth reconnect create successor bindings instead of mutating
  Agent identity.
- An Agent-originated tool call persists only its exact binding provenance. Agent, owning Operator,
  and display name derive through canonical joins, and approval/execution revalidate that binding
  so queued work cannot transfer to a replacement credential.

## Interactive enrollment

1. FastMCP validates client, redirect, resource, scopes, and S256 PKCE through its public
   `authorize()` path.
2. Haku reserves the exact public client/redirect/challenge tuple only as a temporal collision key
   and creates a random `EnrollmentInteraction`.
3. The browser independently logs into Haku through Authentik. Opening the page binds its verified
   identity and canonical Operator to the interaction exactly once.
4. The Operator explicitly denies or allows a required name and auto-approval policy for a new
   Agent, or reconnects an existing owned Agent with a selected policy. This records intent but
   issues no grant yet. The server chooses the unique configured `never` policy as the fail-closed
   default; its identifier has no built-in meaning.
5. FastMCP performs its untouched Authentik callback and creates the downstream code.
6. At exchange, Haku verifies the MCP-side access-token principal and requires the same active
   canonical Operator as the browser interaction. One locked transition creates the draft Agent
   when needed plus its issuing binding and grant.
7. FastMCP carries only opaque `grant_id` context through the token family. The first successfully
   verified MCP tool request atomically activates the Agent and binding.
8. Access load, transparent/explicit refresh, decisions, and execution revalidate the grant,
   binding, Agent, and Operator. Local revoke remains authoritative even if best-effort upstream or
   individual-token cleanup fails.

The enrollment cookie is only a short-lived page/CSRF binding: path-scoped, `HttpOnly`,
`SameSite=Lax`, and `Secure` in production. It contains no name, token, raw claim, or durable
authority. The trusted Console SPA receives an escaped typed view model from same-origin APIs;
decision endpoints enforce the browser binding and exact Origin before changing authority state.
The browser surfaces themselves are in <oauth_browser_surfaces.md> § Agent enrollment in Settings.

## The runtime actor

The runtime caller is `OperatorActor | AgentActor`. Only the authority constructs an
`AgentActor(agent_id, operator_id, binding_id, auto_approval_policy)` from durable state. Operators
may change an OAuth Agent's policy later under Settings; configured static Agent policies remain
owned by deployment configuration so the manually approved public Coder identity cannot be granted
standing authority through the UI. Policy selection is required for every new enrollment,
reconnection, Settings mutation, and static-Agent definition. Only pre-migration durable Agents may
have a null assignment, which fails closed until an Operator selects a policy. Agents can
submit/read only their own calls; Operators can read/decide all and only calls they own; Agents
never approve themselves. Repository operations have no unscoped or `None` actor mode.

## The FastMCP seam

Haku supports one exact FastMCP version at a time. The adapter's sole private seam is `_code_store`
read/delete during code exchange; protected claim, scope-translation, and transparent-refresh hooks
are version-pinned. It does not replace route construction, registration, transaction storage,
callback, PKCE, or token issuance. Adapter compatibility and mounted enrollment/token/refresh/
revocation tests are mandatory before a repin.

## The credential boundary

Client-side credential deletion is generally invisible to Haku. `last_seen_at` is observation, not
connection state; an Operator-owned revoke/disable action is the authoritative product control.
Postgres, Valkey for generic consumers, Kubernetes Secrets, and their backups/admin readers are the
accepted private credential boundary. Extra application encryption is optional defense-in-depth,
while tokens, codes, secrets, callback queries, and raw OAuth forms must never enter logs or API
models.
