# Canonical Agent authority

The Console owns canonical Operator and Agent identity for the browser-managed Agent catalog. The
browser login remains at `/auth/*`; `/api/agent-enrollment/agents` lists Agents for the authenticated
Operator and permits profile changes only for eligible Agents. Static Agent identities and their
profile assignments come from deployment configuration.

Alembic revision `0081` is the forward-only database baseline. The live identity graph is:

```text
Operator -> IdentityAnchor -> OidcIdentity
Operator -> Agent -> AgentNameReservation
Agent -> CredentialBinding -> AuthorizationGrant | StaticCredential
```

## Durable identity rules

- `Operator`, `Agent`, and authority-bearing relationships use local immutable UUIDs. An exact
  verified `(issuer, subject)` identifies an OIDC identity; the configured Authentik trust domain
  determines how that identity maps to an Operator. Username is presentation only.
- OAuth `client_id` describes client software or registration metadata. It is never the Agent,
  Operator, grant, or credential binding.
- Every Agent has a normalized non-empty display name, globally unique by its normalized key. The
  name is presentation owned by the canonical Agent; it is never a credential or identity key.
- A `CredentialBinding` owns credential kind, generation, predecessor, and lifecycle. An OAuth
  `AuthorizationGrant` owns client software, authorizing identity, scopes, and token-family evidence.
  Static rotation and OAuth reconnect create successor bindings instead of mutating Agent identity.
- An Agent stores a nullable `access_profile_id`. The deploy catalog validates profile IDs and
  static Agent references; a profile is an identity label only and does not enable tool execution,
  auto-approval, or a Console-held server.

## Retained tool-call and grant data

The historical `mcp_tool_calls`, `mcp_tool_call_principals`, and Kubernetes grant tables, enum,
ORM mappings, and migrations remain in Postgres. The Console no longer accepts, executes, lists, or
decides tool calls, and its application has no data readers or writers for those tables. Keeping the
schema and rows is a data-retention boundary: this retirement does not drop or rewrite stored audit,
identity, or grant data. The generic Agent Sandbox constructs remain available to other workspaces.

## Credential boundary

Client-side credential deletion is generally invisible to Haku. `last_seen_at` is observation, not
connection state, and must not be treated as an implicit revoke or disable signal. Postgres, Valkey
for generic consumers, Kubernetes Secrets, and their backups/admin readers are the accepted private
credential boundary. Extra application encryption is optional defense-in-depth, while tokens,
codes, secrets, callback queries, and raw OAuth forms must never enter logs or API models.
