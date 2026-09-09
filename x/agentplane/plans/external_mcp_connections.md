# External MCP connections and static identities

Status: **planned product track; design choices below remain open, no implementation claimed.**
The initial consumers are Claude.ai and independently running harnesses such as Claude Code on
the operator's own machines (for example, wyrm2), connecting to the Action Service's remote MCP
frontend through OAuth, including Dynamic Client Registration (DCR). An operator binds each Connection to a configured static
identity. The first slice uses human approval; configurable per-Identity auto-approval follows later. This is the
first external-client slice toward replacing Haku Console's MCP server. Scheduling and dependencies
live in [the task DAG](task_dag.md): `POLICYBIND`, `EID`, `MCPOAUTH`, `CALLERPOLICY`, `MCPFRONT`, and `EXTERNALMCP`.

**Operator priority:** working deployed Claude.ai MCP access (`CLAUDEAI`) first, transcript
search/lookup (`T3`) next. This orders work without a technical dependency between those features.
The broader `EXTERNALMCP` milestone additionally proves local Claude Code.

**Authentication before configurable policies:** `EID`/`MCPOAUTH` require stable caller identity,
runtime Connection/grant bookkeeping, consent and revocation, but not `POLICYBIND`. Prove real
external Action submission, human allow/deny, and result recovery first. Existing service safety
constraints remain in force; omitting configurable auto-approval never grants automatic execution.
Policy definitions, assignments, inheritance, and preset composition can be designed independently.

Use **Identity** (provisionally, static identity) for the configured authority and **Connection** for
the runtime, operator-named client enrollment and its current Identity binding.
A **Thread** is execution/conversation state and is not an identity. These are product terms;
the exact implementation types remain to be designed. Do not import Haku's overloaded `Agent`
concept for this surface.

This slice is **single-operator**. The existing operator authentication protects configuration and
consent, but multiple-operator management, per-operator Identity ownership, tenant isolation, and
operator-to-operator delegation are out of scope. This does not weaken isolation between Identities.

Harnesses running in Threads inside Sandboxes also call the Action Service through existing workload
authentication, including through this same MCP frontend using Sandbox bearer tokens.
[Configured Action policies](action_policies.md) covers both per-Identity deciders and
auto-approval through concrete Sandbox policy bindings; hosted callers do not need DCR.
SandboxPreset belongs only to the integration app, which resolves defaults and per-Sandbox additions
into each subsystem's own bindings. Neither Actions nor egress interprets preset names.

## Two authentication paths, one MCP surface

Accept external OAuth access tokens and Sandbox workload bearer tokens on the same generic MCP
surface. Resolve external tokens to their authorized Connection/Identity and workload tokens through
the existing `SandboxPrincipalAuthenticator`/resolver, preserving TokenReview, audience and live
Pod/Sandbox-owner checks. Reuse the existing egress placeholder/substitution path for hosted harnesses;
accepting a bearer at the service does not require exposing the real workload token to the runner.

Both paths use the same catalog, submission, bounded waits, receipt/events, and canonical Action
authorization. Workload callers retain namespace/Sandbox UID ownership and Actions-owned policy
bindings; no external OAuth Connection or DCR is required. Sharing an ActionPolicySet does
not merge ownership or grant operator access. Define unambiguous fail-closed token-verifier routing;
unverified token claims may only select validation, never confer identity or authority. Invalid or
wrong-audience credentials must not become an anonymous or more privileged caller through fallback.
Keep operator/BFF authentication separate from this caller-facing MCP surface.

The common frontend and Sandbox-authenticated path can be implemented and tested before external
Identity/OAuth support. Claude.ai acceptance still requires `EID`/`MCPOAUTH`; do not make those a
technical prerequisite for serving MCP to an already-authenticated Sandbox.

## Connection workflow

1. The operator configures a stable identity, for example `claude-personal`, independent of
   conversations and credentials; static does not mean a static bearer. Initially external requests
   use human approval. Later, an explicit policy association may let `claude-wyrm2` reference the same
   ActionPolicySet as hosted Sandboxes without sharing caller ownership or copying policies.
2. The operator adds the remote MCP URL in Claude.ai or configures it in a local Claude Code
   installation. Discovery and registration begin the client's OAuth flow, including the DCR path.
   Registration alone grants no Action access and creates no privileged identity.
3. During the registration/authorization experience, the authenticated operator assigns the client
   a Connection name, selects an existing configured Identity, and sees the human-approval requirement
   (or its configured policy once that feature exists). The server
   records the runtime Connection and binds the resulting authorization grant to that Identity.
   Client names, redirect metadata, requested scopes, and supplied identity
   names cannot authorize that choice. Unbound or denied connections cannot submit Actions.
4. The client uses its access token to discover and invoke the MCP Action surface. The Action
   Service resolves the static identity and exact connection binding from trusted authentication,
   applies service authorization and the human-review path, and records ownership plus binding/client provenance. The caller
   cannot choose a decider or impersonate an identity through tool arguments or provenance fields.
5. A permitted request needing human review returns its durable pending receipt; Claude can resume
   polling after the operator decides through the existing BFF. A prohibited request cannot be
   rescued by another provider's allow or by the ordinary review path. Later configured auto-approval
   must preserve this same canonical Decision and at-most-one-Execution lifecycle.
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
lifecycle. Choose the minimum runtime auth store under `MCPOAUTH`; the later policy-binding storage
gate must not block it. Do not treat runtime associations as implicitly Git-owned.

The single operator uses the **integration app** for enrollment and Connection management. Its
existing browser login and BFF are the operator-facing surface for:

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

## OAuth enrollment through the integration app

**Chosen UI location, open protocol composition.** DCR itself is a client-to-server registration
request and has no operator GUI. Naming and Identity selection happen during the subsequent OAuth
authorization interaction, presented by the integration app:

1. Claude discovers the protected MCP resource and its authorization server, then registers a client
   through DCR (or uses the supported alternative registration mechanism). Registration grants no
   Action authority and need not create a named Connection yet.
2. Claude opens the authorization endpoint with its registered redirect URI, resource, scopes,
   state, and PKCE challenge. The authorization server validates these and persists a bounded
   pending transaction before handing the browser to an integration-app enrollment page.
3. The app uses its existing operator login, returning to that enrollment after login if needed.
   It shows the validated client/redirect information, Connection name/reconnect choice, configured
   Identity, and referenced ActionPolicySet. Consent or denial is an authenticated, CSRF-protected
   app action tied to this exact transaction.
4. The app's BFF records that choice through the canonical enrollment/Connection authority under
   authenticated operator authorization. The authorization server consumes the decision once and
   binds code issuance to the selected Identity and exact Connection/binding revision. The browser
   cannot authorize this by submitting an Identity name or a guessed transaction ID alone.
5. The authorization server resumes the original OAuth flow, redirecting an authorization code and
   the client's state to Claude's registered callback. Claude.ai's hosted callback and local Claude
   Code's native callback are client-specific; neither is the integration app's operator-login callback.
6. Claude redeems the code with its PKCE verifier for its own resource-bound token, then uses the
   MCP frontend as the bound Identity. The operator session/token never becomes Claude's credential.

Keep the external client's OAuth transaction and the app's operator-login transaction separate:
each retains its own state/PKCE/redirect context. Use an opaque, expiring enrollment handle for the
handoff, bound to the authenticated browser interaction before mutation. Persist canonical pending
state across login/service restart and reject expired, replayed, substituted, or cancelled consent.
Concurrent browser tabs must not exchange enrollment decisions. Completion retries must not create
duplicate Connections or grants; issuance must recheck the selected binding is still authorized.

The existing [operator federation](../docs/operator_federation.md) and
[`FederatedOperatorActions`](../app/action_federation.py) already provide an app-session-to-Action
operator path with destination-side JWT verification. That is a reuse candidate for enrollment and
management BFF calls, not evidence that these endpoints or their enrollment handoff already exist.
Assess the required destination audience/permissions and current operator subject mapping before
extending it. Its Authentik JWT-bearer exchange is distinct from the external client's authorization
code flow; a shared browser cookie or a caller-supplied identity header is not federation.

The authorization-server protocol endpoints may be co-located with the integration app or run in
another service which delegates consent UI to it. Decide ownership of pending transactions,
Connection mutations, token issuance, and the authenticated completion API together; use one owner
for each fact. The GUI location does not select Kubernetes versus PostgreSQL storage and does not
make the app another Action policy/Decision authority. Later rename/unbind/rebind operations use
the same authenticated BFF-to-authority boundary, without replaying DCR for a rename.

## Authority boundaries

**Preserve the specific submitting OAuth client:** every externally submitted Action retains its
owning Identity plus the exact runtime Connection, OAuth client registration (`client_id` and its
authorization-server namespace), and authorizing grant/binding revision. Resolve these from verified
authentication and persist them as canonical submission provenance, never from caller-supplied
`origin`/`correlation`. Several clients sharing an Identity may share its caller scope, but must remain
distinguishable in the Action audit. Rename, unbind/rebind, refresh, registration cleanup, or removal
must not rewrite or erase the historical client linkage; retain enough immutable registration and
binding history to resolve it. Connection display names are mutable presentation, not audit keys.
Replay with the same Identity-scoped submission key returns the original Action and original
submitting-client provenance, not attribution to the client that happened to retry it.

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

When configurable policies are added, the shared [Action policy plan](action_policies.md) owns mandatory bounds, decider composition, and
policy-change/dispatch consistency. Apply those rules to the resolved Identity and retain its exact
submitting Connection; pending work must not silently inherit a replacement Connection's authority.
Its reusable policy sets let an external Identity and a Sandbox type share Action permissions without
duplicating policy or conflating Identity with type. Enrollment selects the Identity; it does not
copy the referenced policy configuration into the new Connection.

## Existing pieces and implementation baseline

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
  `mcp_infra` auth/persistence, for reusable pieces. Build on the operator-approved assumption that
  Haku Console's DCR works; additional compatibility probes and live Claude.ai proof are not
  prerequisites for implementation. Reuse pinned FastMCP/Authlib registration, PKCE, callback, token,
  and persistence machinery, including narrowly isolated and regression-tested private hooks where
  existing Haku behavior requires them. Do not import its conversation lifecycle or multi-operator
  graph. Test new authority/consent boundaries during implementation; perform real browser/client
  acceptance afterward and fix compatibility gaps found there. This assumption is permission to
  proceed, not a claim that Agentplane's new integration is already verified.

## Initial MCP tools: generic Actions

**Placement selected:** the MCP server lives in the Action Service process, sharing canonical
authentication, catalog, request/Decision/Execution services, and lifecycle-owned resources. A
separate pod or sidecar is not required for the initial frontend. Use FastMCP for consistency;
name the common frontend for Actions, not workloads, since both caller kinds use it. This selects the resource-server
placement, not the still-open OAuth authorization-server owner or integration-app enrollment handoff.

**Selected first-slice direction:** expose a small fixed set of tools which treat Actions as data.
Do not mirror each Action or mounted upstream MCP tool into a separate exposed MCP tool. Changing
the canonical Action catalog changes discovery results, not the frontend's tool roster. Per-Action
MCP projection is an optional future experiment only if evidence from generic-tool use justifies it;
it may never be needed and is not a scheduled follow-on or migration prerequisite.

Working tool names and contracts (final wire schemas remain implementation work):

| Tool                         | Input                                                                                                                               | Result / purpose                                                                                                                    |
| ---------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `list_actions`               | Optional group/filter, bounded pagination, and `include_fields`.                                                                    | Discover compact published Action identifiers; return detailed fields only when explicitly requested.                               |
| `get_action`                 | Action group/name and `include_fields`.                                                                                             | Read compact metadata for one definition, optionally requesting its existing input schema or full description.                      |
| `request_action`             | Structured Action group/name, arguments, caller-scoped idempotency key, optional untrusted provenance, and bounded polling options. | Submit once; return the durable request ID and current receipt/state immediately or after the requested bounded wait.               |
| `get_action_request`         | Request ID and bounded polling options.                                                                                             | Read this caller's durable request state, Decision, and safe Execution result/error, optionally waiting for decision or completion. |
| `cancel_action_request`      | Request ID; no expected version.                                                                                                    | Cancel as the owning caller through the canonical service; return its typed cancellation outcome and current receipt.               |
| `list_action_request_events` | Request ID, `after_sequence`, bounded page size.                                                                                    | Read canonical ordered events and resume from the last received sequence.                                                           |

Here an **Action** is a catalog definition and an **ActionRequest** is a particular submission.
Keep that distinction in tool names and descriptions so `get_action` cannot be mistaken for polling
execution. The ordinary workflow is discover → inspect schema → request → read receipt/events as
needed; the client can reuse known Action definitions. Discovery is read-only and does not imply
permission to execute. The service validates arguments and evaluates the authenticated caller's
policy on submission; no tool accepts an authoritative Identity, policy-set choice, or decider.

**Context budget:** omission of `include_fields` (or an empty list) returns compact identifiers and
minimal metadata, excluding input/output schemas and full descriptions. Explicitly request only the
fields needed, for example `get_action(group="everything", name="echo",
include_fields=["input_schema"])`. Initially allowlist the existing `input_schema` and `description`
(the full description); apply the same selection to bounded `list_actions` pages.
Do not insert omitted fields through nested group details, request receipts, or the generic tools'
own MCP schemas/descriptions. Their schemas must not inline the Action catalog or enumerate every
Action as parameter alternatives. Reject unsupported field names with an actionable error naming
the supported choices; never synthesize unavailable metadata.

The current canonical catalog carries full descriptions and input schemas, but no output-schema
field. **Defer adding output schemas or other new Action metadata until after the working MCP
frontend.** MCP's [`outputSchema` is optional](https://modelcontextprotocol.io/specification/2025-11-25/server/tools#tool),
and a generic frontend does not require per-Action output schemas. If added later, `output_schema`
would be another opt-in field describing the safe caller-visible result, which may differ from the
raw upstream MCP result. This is neither an `MCPFRONT` nor a `CLAUDEAI` acceptance requirement.
Selective presentation does not weaken server-side input validation, policy enforcement, or result redaction.

**Bounded waits and cancellation:** reuse the landed
[bounded-receipt wait contract](../action_service/SPEC.md#bounded-receipt-waits) on both submission
and request reads. `wait_seconds` defaults to zero and is capped at 30; `wait_until` selects
`decision` or `terminal` (default). The shared implementation waits on committed notifications,
including cross-process updates, not periodic database/API queries. MCP owns transport integration
and disconnect cleanup, not another subscription mechanism. Disconnect cancels only the wait.

Expose [canonical cancellation](../action_service/SPEC.md#cancellation) through
`cancel_action_request`: owner-only, no expected version, dispatch claim is the cutoff, no executor
stop/kill propagation. Preserve typed outcomes, audit, original-key replay, and waiter wakeup.
These foundations are implemented; their MCP tools and protocol-level tests remain frontend work.

All tools present the existing catalog and Action lifecycle. Submission waits only when explicitly
requested and never indefinitely for human approval; reads never submit or retry execution. On an ambiguous submission response the caller must
retain and reuse the same idempotency key, not create a replacement request. Get/events responses
preserve canonical caller scope, redaction, and shared decision notes. Human decisions and Connection
management stay in the integration app's operator surface. Define actionable errors and bounded
discovery results, and test that both clients can obtain the schema and complete this workflow using
only the generic tools. Exact names, pagination, and idempotency-key ergonomics remain to be settled.

## Design choices to settle before implementation

- **Configuration and consent:** where configured Identities/policies live, how the single operator
  selects an Identity in the integration app, and which component owns authorization grants and token lifecycle. Compare shared
  OAuth infrastructure with a small Action-facing adapter; keep the Action Service authoritative
  for Action authorization. Do not assume Authentik alone supplies DCR or custom identity selection.
  `POLICYBIND` in the [Action policy plan](action_policies.md) owns policy-binding storage/model choices;
  a Connection binds an OAuth grant to an Identity, while a policy binding associates authority with policy.
- **Connection granularity:** whether several grants may share one named Connection, whether several
  Connections may share an Identity, and the consent/rebinding UI. Caller-own reads and idempotency are identity-scoped;
  sharing an identity therefore shares that scope unless a narrower contract is deliberately added.
  Reconnection must not merge identities by client name, username, or a fresh DCR `client_id`.
- **Later policy integration:** resolve this Connection's Identity into the trusted context used by
  [configured Action policies](action_policies.md); retain distinct external and Sandbox ownership.
  This is not a prerequisite for the human-approved external slice.
- **Generic tool ergonomics:** finalize the schemas above against actual Claude.ai and local
  Claude Code use. Keep canonical request IDs and retry semantics; pending human review must return
  within the requested bounded wait (immediately by default) and remain queryable. Decide how a client retains the same submission key after an
  ambiguous tool response. Do not assume either client will poll or wake autonomously after the
  conversation stops.
- **Lifecycle:** storage/refresh/revoke behavior, consistency at dispatch, cleanup of abandoned
  registrations, and explicit behavior after reconnect. Agree on these before schema/API work.

## Acceptance and migration

First prove a credentialless fixture through both a real Claude.ai custom connection and Claude Code
running independently on an operator machine such as wyrm2. Exercise each client's discovery,
registration, redirect/callback, token refresh, and reconnect behavior; a hosted-browser callback
does not prove a native-client flow. Independently check canonical Action records rather than prose.

Record Claude.ai's deployed result separately as `CLAUDEAI`, the operator's higher-priority outcome.
The combined evidence for both clients satisfies `EXTERNALMCP`. Required scenarios:

- separately prove the shared frontend with a real Sandbox caller via workload bearer authentication
  and existing egress substitution: generic discovery/submission, bounded waits, safe results, and
  receipt/event recovery without DCR. Preserve per-Sandbox ownership and idempotency; reject expired,
  invalid/wrong-audience credentials and forged workload identity, prevent cross-caller receipt reads,
  and verify neither authentication path grants operator methods. Configured Sandbox-type auto-approval
  is covered by `SBPOLICY`, not a prerequisite for this frontend authentication check;
- discovery → DCR → operator consent/identity selection → token exchange → authenticated tool use;
- use the fixed generic tools to list/inspect an Action, request it, and read its receipt/events;
  adding or changing a catalog Action updates discovery without creating an exposed per-Action tool;
- with long descriptions and large schemas in the catalog, default list/get results omit those
  fields; selecting `input_schema` returns only that extra field, and explicit full-description
  requests return that data without unrelated catalog expansion. Unsupported fields fail clearly;
  new Action metadata, including output schemas, is not required for this milestone;
- both submission and request reads support immediate/default and bounded waits: already-complete
  requests return immediately, decision-only waits return on allow/deny, completion waits span queued
  and running execution, and deadlines return pending receipts. Exercise state changes racing wait
  setup, denial/failure, disconnect/reconnect, and duplicate submission with the same idempotency key
  without losing a transition or creating another Execution;
- prove idle waits perform no periodic state queries, committed updates wake waiters across process
  boundaries, and setup races, duplicate notifications, channel loss, and waiter cleanup are handled;
- the real integration-app enrollment/login round trip for each client: bind the decision to the
  correct pending OAuth transaction, return to the client's validated callback, and demonstrate
  rename/unbind/rebind from app management. Reject forged/expired/replayed enrollment, CSRF, and
  wrong-audience or unauthenticated BFF completion; exercise concurrent tabs and restart between
  login, consent, and token exchange without duplicate grants or lost Identity selection;
- identity A submits a permitted Action and receives a pending receipt; no execution starts until
  human approval. Denial produces no execution. Identity B cannot read or cancel A's requests;
- forged identity metadata, unbound registration, wrong-resource tokens, and a failing existing
  safety constraint cannot execute; clients cannot invoke operator Decisions;
- human allow/deny uses the existing BFF; request, Decision, and Execution retain the resolved
  identity/binding/client provenance and safe result;
- the deployed integration-app Actions page displays Claude.ai's pending request, exact arguments,
  owning Identity and specific authenticated client/Connection; its Allow/Deny controls drive the
  canonical Decision and Claude.ai recovers the receipt/result. Basic display and controls already
  exist; extend their presentation for external-client provenance and prove the real browser flow;
- duplicate submission and reconnect/restart reuse durable receipts without another Execution;
  B cannot read A's requests/events; refresh retains identity without copying operator authority;
- two distinct OAuth clients bound to the same Identity submit distinct Actions: both have the same
  owner but retain their exact authenticated client/Connection/grant linkage. Rename/rebind/revoke
  and registration cleanup preserve that history; a cross-client retry of the same submission key
  cannot rewrite the original provenance, and forged caller metadata cannot select it;
- runtime naming and management: rename preserves authority, unbind rejects old-token Action access,
  and rebind A → B enforces the chosen token/reauthorization contract without transferring A's
  pending work or historical provenance to B. A fresh DCR enrollment can reconnect an existing named
  Connection only through explicit operator authorization;
- connection revoke and Identity disable enforce the chosen bounds, including
  queued work and old tokens. Names, bindings, and their changes survive service restart. Tokens,
  codes, and backend credentials stay out of logs/results.

Configurable auto-approval, policy-miss behavior, and policy tightening are accepted separately under
`CALLERPOLICY`/`SBPOLICY`; they do not gate `CLAUDEAI` or the two-client `EXTERNALMCP` proof.

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
