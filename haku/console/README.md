# haku/console — Haku's interactive console

A FastAPI service serving the trusted Haku console as a React single-page app over JSON and WebSockets.
The console is the operator-owned shell around Haku's cross-origin UI, the approval/audit ledger
for privileged tool calls, and the home of Agent identity used by the Kubernetes proxy.

It runs in its own `haku-console` namespace, outside Haku's `haku-sandbox` authority and egress
fence. That separation lets it hold credentials Haku may use only through reviewed, operator-gated
surfaces. The complete threat model and enforcement inventory are in <../docs/security.md>.

## Where each contract lives

This README is the component map, not a second copy of every contract:

- <docs/agent_authority.md> — canonical Agent identity, credentials, enrollment, profiles, and
  actor-scoped authority.
- <docs/containment.md> — iframe isolation, trusted chrome, Agent UI bridge actions, consent, and browser-side
  exfiltration bounds.
- <docs/oauth_browser_surfaces.md> — account-link and Agent-enrollment browser boundaries.
- <../docs/security.md> — threat model and security invariants.
- <../../cluster/k8s/haku/console/README.md> — deployment topology, migration release work, routing,
  credentials, and one-time connection bootstrap.
- <frontend/README.md> — the SPA module map and frontend build/test commands.

## The capability tier — privileged actions, operator-gated

`capabilities.py` exposes the remaining bespoke capability route,
`POST /api/capabilities/launch-routine`. It uses a console-only bearer and is protected by exact
Origin admission, a tiny reviewed allowlist, trusted-shell confirmation showing the prompt
verbatim, and audit logging in a namespace Haku cannot read. The framed Haku UI can only request a
launch through the Agent UI bridge; it cannot call the route or render the deciding control.

This remains the routine launch path after removing the inactive `haku_routine` MCP wrapper. Once
haku-ui uses the capability route, the Agent UI bridge action and bespoke capability router can
retire. There is no low-privilege console write tier: haku-ui writes its own state.

## Tool-call approval ledger

The operator REST API (`/api/approvals/pending`, `/api/tool-calls`, and
`POST /api/tool-calls/{tool_call_id}/decision`) remains active. It lists retained call records and
allows already-pending work to be approved or denied; approved rows still execute through the
configured in-process backend and record their result. The event WebSocket is only a lossy
invalidation channel: REST remains authoritative.

The Console's Agent-facing MCP endpoint, Operator browser MCP client, and OAuth discovery handlers
are retired. New MCP calls cannot enter the ledger. Existing rows and their operator decisions stay
in Postgres for audit and to let pending work drain safely.

### Canonical Agent authority and enrollment

The canonical contract is <docs/agent_authority.md>. In short: `Operator`, `Agent`, credential
bindings, grants, names, profiles, and tool-call principals are durable local identities; every
Agent call record keeps exact binding provenance; and browser enrollment must converge with the
upstream Agent OAuth principal before a binding becomes active. Access profiles own auto-approval
and in-process-server grants; missing assignments fail closed.

### In-process execution backend

`mcp/in_process_servers.py` still registers the `grants` backend so previously approved rows can
finish while the ledger drains. The Console no longer reflects or refreshes server catalogs, serves
MCP requests, or makes browser-session reads through `/mcp`. The routine uses its separate audited
`POST /api/capabilities/launch-routine` capability; its unused MCP wrapper was removed.

The Console Recall reader, access policy, and `haku_index` tool were removed. The shared
`haku/recall_index` package remains in use by Agentplane. This release retains the Console's Recall
tables, indexed data, and `vector` extension: older replicas validate that schema during rollout.

The `sandbox` in-process MCP server is absent from the deployed catalog, along with its
access-profile grant, `agent_sandbox` configuration, and auto-approval policy. The Haku-specific
template, warm pool, janitor, and Console sandbox Role/RoleBinding are gone from Flux output too. The
generic Agent Sandbox constructs remain for other workspaces.

## Free-form UI — Haku's own UI, embedded

The console frames `haku-ui.allegedly.works` full-page in a sandboxed cross-origin iframe and owns a
narrow rail of trusted chrome. The frame cannot read console DOM, cookies, credentials, approvals,
or capability routes. A schema-validated `postMessage` Agent UI bridge carries only requests that need the
trusted side; the shell origin-checks, decides, confirms where necessary, and owns revocation.

The complete Agent UI bridge and consent contract—including route/title mirroring, open-link checks,
geolocation and screenshot grants, shell-owned kill switches, and residual exfiltration bounds—is
<docs/containment.md>. Frontend ownership and routing are in <frontend/README.md>.

## Past tool calls — full-page history

`frontend/tool_calls_page.tsx` renders the Operator's durable audit ledger at
`/_console/tool-calls`. It pages newest-first by keyset cursor, defaults to 25 rows, and hides
routine auto-approved traffic unless requested. The small page is deliberate: each row may carry
whole argument/result payloads, so hundreds of rows make a multi-megabyte response. A live event
refreshes only the newest page and merges it over older pages; result/argument editors initialize
near the viewport rather than for every retained row.

## Notifications — Web Push for pending approvals

Web Push reaches an Operator when no console tab is open. The server (`notifications/push.py`,
`notifications/push_routes.py`) shows one versioned notification per queued call; the service worker
(`frontend/sw.ts`) offers Approve/Deny and deep-links to the audit view. A push grants no authority:
buttons call the ordinary exact-Origin decision endpoint under the Operator session.

`PendingApprovalNotifier` updates that notification on approval, denial, or withdrawal. Calls that
never queue are never pushed. Preserve these operational contracts:

- The VAPID private key is the console's push identity; rotating it invalidates every subscription.
- Push payload changes are additive within a `kind`, because an installed service worker may lag the
  server by a day. Non-additive changes require a new variant.
- An expired one-hour Operator session turns a notification action into a re-authentication deep
  link rather than a failed decision.

Third-party notification action services are intentionally not used: they would need a deciding
credential outside the console's origin, contrary to <../docs/security.md> invariant #4.

## Perimeter / deploy

Cluster topology and operations are owned by <../../cluster/k8s/haku/console/README.md>. In
particular, that document is canonical for static/API routing, migration Jobs, rollout strategy,
OAuth/client bootstrap, credentials, and placement. Keep the high-level
boundary here: Haku cannot mutate or inspect the `haku-console` namespace; browser auth is
app-owned; operator browser auth is independent of the retired MCP endpoint; every new top-level backend
prefix must also be routed by the static nginx shell; and API replicas overlap during a rollout, so
stored and cross-replica contracts must tolerate adjacent releases.

### Vocabularies across a roll

A new writer meeting an old reader fails transiently: it dies with the replica. An old writer
meeting a new reader fails permanently: it dies with the row. Tolerance fixes the first; only a
constraint fixes the second.

**Readers tolerate narration and cross-replica payloads produced by a newer replica.** Those values
must not raise merely because an older reader has no word for them. They decode to a named
unknown—such as `util.sqlalchemy_types.UnknownValue`—never `None` or a nearby member, so each
consumer must handle the uncertainty explicitly. Cross-replica payload models do not reject unknown
fields.

**Writer rollout depends on the vocabulary:**

- **Narration** is append-only information a reader may correctly skip, such as notification kinds
  and `ConsoleEvent.event_type`. A new value may ship with its writer in one release; the skipped
  narration is the named compatibility cost.
- **Decision** values drive behavior, such as tool-call, provenance, or rejection statuses. No
  old-reader guess is safe, so a reader that knows the new concrete member ships one release ahead
  of the writer and the writer waits for convergence. Decision columns are currently strict—they do
  not decode unknown values to `UnknownValue`. Tolerant decoding could keep an unrelated inventory
  read alive, but it would not remove the two-release rule because no consumer may guess what the
  value means.
- **A required field added to an existing shape** is a narrowing, not a vocabulary extension. Use
  expand/contract plus a constraint that makes the old writer fail instead of silently creating a
  permanently misread row.

The deciding question is: could this value have been produced by a newer commit than the reader?
If no—a request body, config file, MCP argument, or pinned third-party vocabulary—an unknown value
is a bug or attack and root <../../STYLE.md> strict mapping applies. If yes, unknown data is expected
and raising is the defect. A version-negotiated seam may reject unknown kinds after its handshake;
storage has no handshake.

## Test

```bash
bbr test //haku/console/...
```
