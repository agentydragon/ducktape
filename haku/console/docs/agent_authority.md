# Canonical Agent authority and enrollment

The durable Postgres graph for Console Agent identities and the authorization records retained from
the retired MCP OAuth flow. The Console's Agent-facing MCP protocol endpoint and FastMCP adapter
have been removed; the remaining identity code supports configured Agents and the operator-owned
approval/audit ledger. The operator browser login remains at `/auth/*`.

Alembic revision `0081` is the single forward-only database baseline. It directly installs one
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
- An Agent stores only a nullable `access_profile_id`, naming one reviewed deployment-config
  capability bundle. Its authority dimensions are independent and default-deny: an auto-approval
  policy decides whether a permitted call skips review; `in_process_server_ids` selects
  credential-free Console-held servers; `allowed_harnesses` selects launchable harness kinds.
  In-process server access does not imply other capabilities, and neither does auto-approval.
  `can_read_profiles` is the reviewed, acyclic
  conversation-visibility graph: `conversation_read_access` derives each caller's transitive read
  closure and the `haku_conversations` drilldown enforces it against the conversation's pinned
  `access_profile_id`. It grants information visibility only, never any
  other capability. Credential bindings authenticate an Agent and never select any of these
  capabilities. A null or removed profile is fail-closed.

The Console Recall integration and `recall_index_ids` profile field were removed.

## Retired MCP OAuth admission

The Console no longer accepts MCP OAuth authorization, callback, or tool requests. Existing Agent,
credential-binding, Kubernetes grant, and tool-call rows remain durable; this rollout does not
delete stored identity or tool-call data. Kubernetes grants are no longer listed, revoked, or used
for authorization. The old DCR/token-state table also remains until after outgoing replicas have
left, since they can still initialize it during the rolling deployment.

## The runtime actor

Identity splits across four roles the tool-call domain keeps apart; reading the wrong one for an
authorization or audit decision is the failure this boundary prevents. In one sentence: an actor is
a request principal plus the accountability identities (owning Operator and exact credential
binding); the tool-call principal row is the durable submitter provenance revalidated from that
actor. The four, at their definitions:

- **Authentication context** — `RuntimeActor` (`OperatorActor | AgentActor`, `tool_call_actor.py`):
  the actor.
- **Request principal** — `RequestPrincipal` (`mcp/principal.py`): the Agent and access profile
  projected from the authenticated actor, dropping accountability identities.
- **Submitter provenance** — `McpToolCallPrincipal` (`database_schema.py`; wire in
  `haku/shared/haku/console/tool_calls.py`): the durable audit record the actor is revalidated from.
- **Runtime actor / execution** — `McpExecutionCaller` and `McpExecutionContext`
  (`mcp/execution.py`): the trusted identity a Console-owned in-process tool reads at execution.

The runtime caller is `RuntimeActor` (`OperatorActor | AgentActor`). Only the authority constructs an
`AgentActor(agent_id, operator_id, binding_id, access_profile_id)` from durable state. Operators
may change an OAuth Agent's profile later under Settings; configured static Agent profiles remain
owned by deployment configuration so the manually approved public Coder identity cannot be granted
standing authority through the UI. Profile selection is required for every new enrollment,
reconnection, Settings mutation, and static-Agent definition. Only pre-migration durable Agents may
have a null assignment, which fails closed until an Operator selects a profile. Agents can
submit/read only their own calls; Operators can read/decide all and only calls they own; Agents
never approve themselves. Repository operations have no unscoped or `None` actor mode.

## The credential boundary

Client-side credential deletion is generally invisible to Haku. `last_seen_at` is observation, not
connection state; an Operator-owned revoke/disable action is the authoritative product control.
Postgres, Valkey for generic consumers, Kubernetes Secrets, and their backups/admin readers are the
accepted private credential boundary. Extra application encryption is optional defense-in-depth,
while tokens, codes, secrets, callback queries, and raw OAuth forms must never enter logs or API
models.
