# External MCP connections and static identities

Status: **planned product track; design choices below remain open, no implementation claimed.**
The first consumer is Claude.ai connecting to the Action Service's MCP frontend through OAuth
Dynamic Client Registration (DCR). An operator binds the authorized connection to a configured static
identity whose bounded DecisionProviders determine which Actions can run automatically. This is the
first external-client slice toward replacing Haku Console's MCP server. Scheduling and dependencies
live in [the task DAG](task_dag.md): `EID`, `MCPOAUTH`, `IDPOLICY`, `MCPFRONT`, and `CLAUDECONNECT`.

Use **Identity** for the configured authority and **Connection** for its OAuth grant binding.
A **Thread** is execution/conversation state and is not an identity. These are product terms;
the exact implementation types remain to be designed. Do not import Haku's overloaded `Agent`
concept for this surface.

This slice is **single-operator**. The existing operator authentication protects configuration and
consent, but multiple-operator management, per-operator Identity ownership, tenant isolation, and
operator-to-operator delegation are out of scope. This does not weaken isolation between Identities.

## Connection workflow

1. The operator configures a stable identity, for example `claude-personal`, and its permitted
   Actions and auto-approval conditions. Static means its identity and policy association are
   configured independently of conversations and credentials; it does not mean a static bearer.
2. The operator adds the remote MCP URL in Claude.ai. Discovery and DCR begin the OAuth flow.
   Registration alone grants no Action access and creates no privileged identity.
3. During the registration/authorization experience, the authenticated operator selects an existing
   configured Identity and reviews its policy. The server binds the resulting authorization
   grant to that identity. Client names, redirect metadata, requested scopes, and supplied identity
   names cannot authorize that choice. Unbound or denied connections cannot submit Actions.
4. Claude.ai uses its access token to discover and invoke the MCP Action surface. The Action
   Service resolves the static identity and exact connection binding from trusted authentication,
   applies the identity's policy, and records both ownership and binding provenance. The caller
   cannot choose a decider or impersonate an identity through tool arguments or provenance fields.
5. An in-bounds auto-allow produces the canonical Decision and at most one Execution. An otherwise
   permitted request needing human review returns its durable pending receipt; Claude can resume
   polling after the operator decides through the existing BFF. A prohibited request cannot be
   rescued by another provider's allow or by the ordinary review path.
6. Token refresh preserves identity. Reconnection may create a new binding to the same configured
   identity after authorized selection. Revoking a binding disables that connection; disabling the
   identity disables all its bindings. Neither operation rewrites historical submitter provenance.

## Authority boundaries

Keep these concepts distinct even if the first implementation stores them together:

| Concept                    | Meaning                                                                                                                |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| Static identity            | Stable configured caller and policy association; independent of Thread, Sandbox, OAuth client ID, and display name.    |
| OAuth client registration  | Client-software and redirect metadata. DCR input is not proof that the caller is Claude.ai or an authorized operator.  |
| Connection                 | Server-owned association of an authorized grant/token family with an Identity, consent evidence, and revocation state. |
| Action policy              | Exact allowed Actions and conditions, plus which bounded DecisionProviders can remove the human-review requirement.    |
| Backend credential binding | Authority an Executor uses at an upstream system; separate from the external client's inbound OAuth credential.        |

Reuse the canonical caller-own Action reads, idempotency, Decision aggregation, event cursors, safe
projections, and one-Execution contract. Carry authenticated identity into both policy evaluation
and execution context without forwarding the client's token to an upstream MCP server. The external
client remains a caller, including when its operator is the person who authorized the connection.

Bound auto-approval by exact Action group/name and validated arguments or resource selectors; add
time/use/budget bounds only where the chosen policy requires them. A policy miss may defer to a
human only inside the identity's permitted envelope. Missing identity/policy, invalid bindings, or
failure to evaluate a mandatory bound must not permit execution. The existing aggregation treats
provider errors as `no_opinion` and accepts another provider's allow: mandatory authorization bounds
therefore cannot be implemented as optional advisory providers. Define their enforcement separately
from the deciders which merely authorize automatic execution inside that envelope.

Policy edits and revocation need an explicit admission/Decision/dispatch consistency rule. Retain
the submitting binding, prevent pending work from silently inheriting a replacement connection's
authority, and revalidate the required authority before dispatch. Specify the linearization point
and bounded revocation behavior; revocation does not undo already-started external effects.

## Existing pieces and reuse probe

- [`MCPFRONT` and `MCPAGG`](task_dag.md) already describe external Action presentation and Haku
  replacement. This track supplies their concrete first client and identity/policy requirements.
- [`DecisionContext`](../action_service/models.py) already carries a trusted caller and an optional
  verified `agent_identity`; [`_auto_decide`](../action_service/service.py) currently supplies only
  the caller. [`FixtureDecisionProvider`](../action_service/fixture_policy.py) is bounded to
  Kubernetes Sandbox callers. Static-identity resolution and configurable per-identity policies
  are new work; adding a string to the request envelope would not implement them.
- Haku's [Agent authority](../../../haku/console/docs/agent_authority.md) already separates OAuth
  client registration, operator consent, identity, and credential binding. Inspect its enrollment
  tests and the [`FastMCP adapter`](../../../haku/console/identity/fastmcp_adapter.py), plus shared
  `mcp_infra` auth/persistence, before choosing reusable pieces. The first probe should map which
  existing protocol machinery can support selection of a configured identity without importing
  Haku's conversation lifecycle, multi-operator ownership graph, or version-sensitive private adapter hooks.

## Design choices to settle before implementation

- **Configuration and consent:** where configured Identities/policies live, how the single operator
  selects an Identity, and which component owns authorization grants and token lifecycle. Compare shared
  OAuth infrastructure with a small Action-facing adapter; keep the Action Service authoritative
  for Action authorization. Do not assume Authentik alone supplies DCR or custom identity selection.
- **Connection granularity:** what constitutes one revocable connection, whether several grants may
  share an identity, and the consent/rebinding UI. Caller-own reads and idempotency are identity-scoped;
  sharing an identity therefore shares that scope unless a narrower contract is deliberately added.
  Reconnection must not merge identities by client name, username, or a fresh DCR `client_id`.
- **Policy composition:** minimum typed configuration for exact Actions/conditions, precedence of
  mandatory bounds and deciders, configuration version recorded with a Decision, and behavior of
  already-pending requests when policy changes. A general policy DSL or cross-cutting capability
  profile is not required to prove one bounded policy.
- **MCP presentation:** compare schema-preserving per-Action tools with discover/submit/get/events
  tools against actual Claude.ai use. Keep canonical request IDs and retry semantics; pending human
  review must return promptly and remain queryable. Decide how a client recovers the same submission
  key after an ambiguous tool response. Do not assume a tool call remains open until approval or
  that Claude.ai will poll or wake autonomously after the conversation stops.
- **Lifecycle:** storage/refresh/revoke behavior, consistency at dispatch, cleanup of abandoned
  registrations, and explicit behavior after reconnect. Agree on these before schema/API work.

## Acceptance and migration

First prove a credentialless fixture through a real Claude.ai custom connection, independently
checking canonical Action records rather than its reported prose:

- discovery → DCR → operator consent/identity selection → token exchange → authenticated tool use;
- identity A auto-allows one precisely bounded Action, while changed arguments fall back to review
  or are denied according to configuration; identity B does not inherit A's auto-allow;
- missing policy, forged identity metadata, unbound registration, wrong-resource tokens, and a
  failing mandatory bound cannot execute; clients cannot invoke operator Decisions;
- human allow/deny uses the existing BFF; request, Decision, and Execution retain the resolved
  identity/binding, safe result, and bounded policy evidence;
- duplicate submission and reconnect/restart reuse durable receipts without another Execution;
  B cannot read A's requests/events; refresh retains identity without copying operator authority;
- connection revoke, identity disable, and policy tightening enforce the chosen bounds, including
  queued work and old tokens. Tokens, codes, and backend credentials stay out of logs/results.

Then inventory Haku's actual client/tool workflows and migrate them individually through `MCPAGG`.
The fixture milestone does not establish backend OAuth account linkage, complete tool parity, or
permission to retire Haku. `MCPAUTH` remains outbound credentialed-account work; `RETIRE_TOOLS` and
`RETIRE_AGENT` retain separate migration gates. Static external identity does not depend on the
broader `AG` model, Thread ingress, capability profiles, or hosted Sandbox acceptance (`MCP0`).

## Protocol references

[Claude connector authentication](https://claude.com/docs/connectors/building/authentication)
documents DCR support, S256 PKCE, discovery, and refresh behavior. The
[MCP authorization specification](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)
separates resource-server and authorization-server roles and favors Client ID Metadata Documents
(CIMD), retaining DCR for compatibility. Preserve the requested DCR path, but keep identity binding
independent of client-registration mechanism so CIMD can use the same consent/policy contract.
Verify the selected client/protocol versions during the implementation probe; do not substitute an
API connector test for the Claude.ai browser connection.
