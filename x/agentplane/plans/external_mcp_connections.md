# External MCP connections and static identities

Status: **planned product track; design choices below remain open, no implementation claimed.**
The initial consumers are Claude.ai and independently running harnesses such as Claude Code on
the operator's own machines (for example, wyrm2), connecting to the Action Service's remote MCP
frontend through OAuth, including Dynamic Client Registration (DCR). An operator binds each Connection to a configured static
identity whose bounded DecisionProviders determine which Actions can run automatically. This is the
first external-client slice toward replacing Haku Console's MCP server. Scheduling and dependencies
live in [the task DAG](task_dag.md): `POLICYBIND`, `EID`, `MCPOAUTH`, `CALLERPOLICY`, `MCPFRONT`, and `EXTERNALMCP`.

Use **Identity** (provisionally, static identity) for the configured authority and **Connection** for
the runtime, operator-named client enrollment and its current Identity binding.
A **Thread** is execution/conversation state and is not an identity. These are product terms;
the exact implementation types remain to be designed. Do not import Haku's overloaded `Agent`
concept for this surface.

This slice is **single-operator**. The existing operator authentication protects configuration and
consent, but multiple-operator management, per-operator Identity ownership, tenant isolation, and
operator-to-operator delegation are out of scope. This does not weaken isolation between Identities.

Harnesses running in Threads inside Sandboxes also call the Action Service through existing workload
authentication. [Configured Action policies](action_policies.md) covers both per-Identity deciders and
auto-approval for configured Actions from trusted Sandbox types; hosted callers do not need DCR.

## Connection workflow

1. The operator configures a stable identity, for example `claude-personal`, and its permitted
   Actions and auto-approval conditions. Static means its identity and policy association are
   configured independently of conversations and credentials; it does not mean a static bearer.
2. The operator adds the remote MCP URL in Claude.ai or configures it in a local Claude Code
   installation. Discovery and registration begin the client's OAuth flow, including the DCR path.
   Registration alone grants no Action access and creates no privileged identity.
3. During the registration/authorization experience, the authenticated operator assigns the client
   a Connection name, selects an existing configured Identity, and reviews its policy. The server
   records the runtime Connection and binds the resulting authorization grant to that Identity.
   Client names, redirect metadata, requested scopes, and supplied identity
   names cannot authorize that choice. Unbound or denied connections cannot submit Actions.
4. The client uses its access token to discover and invoke the MCP Action surface. The Action
   Service resolves the static identity and exact connection binding from trusted authentication,
   applies the identity's policy, and records both ownership and binding provenance. The caller
   cannot choose a decider or impersonate an identity through tool arguments or provenance fields.
5. An in-bounds auto-allow produces the canonical Decision and at most one Execution. An otherwise
   permitted request needing human review returns its durable pending receipt; Claude can resume
   polling after the operator decides through the existing BFF. A prohibited request cannot be
   rescued by another provider's allow or by the ordinary review path.
6. The operator can list Connections, rename one, unbind it, or select a different configured
   Identity for it. Rename changes presentation only. Unbind removes Action authority. Rebind is
   an explicit authority change; it must preserve the original binding on prior/pending Actions.
   Disabling an Identity disables authority through all Connections bound to it.
7. Refresh preserves the authorized binding under the chosen lifecycle contract. Reconnection
   offers an explicit choice of an existing Connection or a new named Connection, with no automatic
   identity merge based on the OAuth client name or registration ID.

For the local-harness case, the harness runs on the operator's machine and consumes the Action
Service's configured upstream MCP servers, auto-approval, human approval, and durable results through
this frontend. It needs no Agentplane Sandbox or Thread, Kubernetes workload token, or copy of an
upstream service credential. Its Conversation/Thread identifiers remain optional untrusted provenance.
Calls through Action Service receive its governance; local shell/filesystem tools and other direct
connections remain governed by the external harness and host. The remote service does not become
the execution manager or sandbox for Claude Code on wyrm2.

## Runtime Connection management

DCR and consent happen at runtime, so Connections cannot require a configuration deployment to be
created. Even with Git-managed Identities and policy definitions, a runtime authority must persist
Connection names, current Identity association (or unbound state), consent, and credential/grant
lifecycle. The [binding/storage gate](action_policies.md) includes PostgreSQL and Kubernetes-backed
Connection records; do not treat these runtime associations as implicitly Git-owned.

The single operator needs an authenticated enrollment/management surface with:

- a readable list/detail view showing the operator-assigned name and bound Identity or unbound state;
- naming and Identity selection as part of the OAuth authorization experience;
- rename without changing authority or receipt ownership;
- unbind so existing connection credentials no longer authorize Actions;
- rebind to another configured Identity with explicit authority-change confirmation; and
- reconnect handling that distinguishes a fresh DCR registration from an existing named Connection.

The connection's immutable identifier, display name, OAuth `client_id`, current Identity, and exact
historical grant/binding provenance are distinct. Choose name uniqueness rules and the binding
revision model rather than using a mutable name as a key. Define whether rebind makes existing tokens
resolve to the newly approved association or requires fresh OAuth authorization, how stale tokens
and caches are handled, and what happens to pending Actions and access to old receipts. Renaming or
rebinding must never rewrite historical identity, policy evidence, or submitting binding. Unbound
Connections cannot submit or read Actions; access to past receipts remains subject to the original
caller scope and current authorization. Connection management is operator-only; a client cannot
rename/rebind itself through the MCP Action surface.

## Authority boundaries

Keep these concepts distinct even if the first implementation stores them together:

| Concept                    | Meaning                                                                                                                                  |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| Static identity            | Stable configured caller and policy association; independent of Thread, Sandbox, OAuth client ID, and display name.                      |
| OAuth client registration  | Client-software and redirect metadata. DCR input is not proof that the caller is Claude.ai or an authorized operator.                    |
| Connection                 | Runtime named client enrollment, with an immutable identifier, current Identity association or unbound state, and grant/binding history. |
| Action policy              | Exact allowed Actions and conditions, plus which bounded DecisionProviders can remove the human-review requirement.                      |
| Backend credential binding | Authority an Executor uses at an upstream system; separate from the external client's inbound OAuth credential.                          |

Reuse the canonical caller-own Action reads, idempotency, Decision aggregation, event cursors, safe
projections, and one-Execution contract. Carry authenticated identity into both policy evaluation
and execution context without forwarding the client's token to an upstream MCP server. The external
client remains a caller, including when its operator is the person who authorized the connection.

The shared [Action policy plan](action_policies.md) owns mandatory bounds, decider composition, and
policy-change/dispatch consistency. Apply those rules to the resolved Identity and retain its exact
submitting Connection; pending work must not silently inherit a replacement Connection's authority.

## Existing pieces and reuse probe

- [`MCPFRONT` and `MCPAGG`](task_dag.md) already describe external Action presentation and Haku
  replacement. This track supplies their concrete initial clients and identity/policy requirements.
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
  `POLICYBIND` in the [Action policy plan](action_policies.md) owns policy-binding storage/model choices;
  a Connection binds an OAuth grant to an Identity, while a policy binding associates authority with policy.
- **Connection granularity:** whether several grants may share one named Connection, whether several
  Connections may share an Identity, and the consent/rebinding UI. Caller-own reads and idempotency are identity-scoped;
  sharing an identity therefore shares that scope unless a narrower contract is deliberately added.
  Reconnection must not merge identities by client name, username, or a fresh DCR `client_id`.
- **Policy integration:** resolve this Connection's Identity into the trusted context used by
  [configured Action policies](action_policies.md); keep external and Sandbox-type selectors distinct.
- **MCP presentation:** compare schema-preserving per-Action tools with discover/submit/get/events
  tools against actual Claude.ai and local Claude Code use. Keep canonical request IDs and retry semantics; pending human
  review must return promptly and remain queryable. Decide how a client recovers the same submission
  key after an ambiguous tool response. Do not assume a tool call remains open until approval or
  that either client will poll or wake autonomously after the conversation stops.
- **Lifecycle:** storage/refresh/revoke behavior, consistency at dispatch, cleanup of abandoned
  registrations, and explicit behavior after reconnect. Agree on these before schema/API work.

## Acceptance and migration

First prove a credentialless fixture through both a real Claude.ai custom connection and Claude Code
running independently on an operator machine such as wyrm2. Exercise each client's discovery,
registration, redirect/callback, token refresh, and reconnect behavior; a hosted-browser callback
does not prove a native-client flow. Independently check canonical Action records rather than prose:

- discovery → DCR → operator consent/identity selection → token exchange → authenticated tool use;
- identity A auto-allows one precisely bounded Action, while changed arguments fall back to review
  or are denied according to configuration; identity B does not inherit A's auto-allow;
- missing policy, forged identity metadata, unbound registration, wrong-resource tokens, and a
  failing mandatory bound cannot execute; clients cannot invoke operator Decisions;
- human allow/deny uses the existing BFF; request, Decision, and Execution retain the resolved
  identity/binding, safe result, and bounded policy evidence;
- duplicate submission and reconnect/restart reuse durable receipts without another Execution;
  B cannot read A's requests/events; refresh retains identity without copying operator authority;
- runtime naming and management: rename preserves authority, unbind rejects old-token Action access,
  and rebind A → B enforces the chosen token/reauthorization contract without transferring A's
  pending work or historical provenance to B. A fresh DCR enrollment can reconnect an existing named
  Connection only through explicit operator authorization;
- connection revoke, Identity disable, and policy tightening enforce the chosen bounds, including
  queued work and old tokens. Names, bindings, and their changes survive service restart. Tokens,
  codes, and backend credentials stay out of logs/results.

Then inventory Haku's actual client/tool workflows and migrate them individually through `MCPAGG`.
Include the required mounted upstream MCP servers, policy assignment, pending human approvals, and
result recovery from both client kinds; reaching only the deterministic fixture is not tool parity.
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
API connector test for the Claude.ai browser connection or an Agentplane-hosted harness for the
independently running Claude Code client.
