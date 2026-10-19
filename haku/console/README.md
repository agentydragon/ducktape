# haku/console — Haku's interactive console

FastAPI service + React SPA: the operator-owned shell around Haku's cross-origin UI, the
approval/audit ledger for privileged tool calls, and the home of Agent identity. Runs in the
`haku-console` namespace, outside Haku's `haku-sandbox` authority and egress fence, so it can hold
credentials Haku uses only through reviewed, operator-gated surfaces.

## Contracts

- <docs/agent_authority.md> — Agent identity, credentials, profiles, actor-scoped authority.
- <docs/containment.md> — iframe isolation, trusted chrome, Agent UI bridge actions, consent,
  browser-side exfiltration bounds.
- <docs/oauth_browser_surfaces.md> — Operator-login and third-party OAuth document boundaries.
- <../docs/security.md> — threat model and security invariants.
- <../../cluster/k8s/haku/console/README.md> — canonical for deployment topology, static/API
  routing, migration Jobs, rollout, OAuth/client bootstrap, credentials, placement.
- <frontend/README.md> — SPA module map and frontend build/test.

## Capability tier

`capabilities.py` serves the one bespoke capability, `POST /api/capabilities/launch-routine`:
console-only bearer, exact Origin admission, a small reviewed allowlist, trusted-shell
confirmation showing the prompt verbatim, audit log in a namespace Haku cannot read. The framed
Haku UI can only request a launch via the Agent UI bridge; it cannot call the route or render the
deciding control. Once haku-ui calls the route itself, the bridge action and bespoke router can
retire. There is no low-privilege console write tier: haku-ui writes its own state.

## Tool-call approval ledger

Operator REST API: `GET /api/approvals/pending`, `GET /api/tool-calls`,
`POST /api/tool-calls/{tool_call_id}/decision` (`mcp/approval.py`). It lists retained call
records and approves/denies already-pending work; browser views refresh every 30 s.

The Agent-facing MCP endpoint (`/mcp`), Operator browser MCP client, OAuth discovery handlers,
grants backend, and every Console-owned in-process MCP execution backend are retired: no new call
enters the ledger, and dispatching a call that needs a removed backend fails closed. Existing call
records, decisions, and Kubernetes grant rows stay in Postgres for audit. The cluster's generic
Agent Sandbox constructs remain for other workspaces.

### Canonical Agent authority

Contract: <docs/agent_authority.md>. `Operator`, `Agent`, credential bindings, names, profiles,
and tool-call principals are durable local identities; every Agent call record keeps exact
binding provenance. Settings lists Agents and lets Operators change OAuth Agent profiles; static
Agent profiles are deployment-owned. Access profiles select auto-approval policy and in-process
server access; missing assignments fail closed.

## Free-form UI

The console frames `haku-ui.allegedly.works` full-page in a sandboxed cross-origin iframe beside
a narrow rail of trusted chrome. The frame cannot read console DOM, cookies, credentials,
approvals, or capability routes; a schema-validated `postMessage` Agent UI bridge carries only
requests needing the trusted side, which origin-checks, decides, confirms, and owns revocation.
Full contract: <docs/containment.md>.

## Past tool calls

`frontend/tool_calls_page.tsx` renders the audit ledger at `/_console/tool-calls`: newest-first
keyset pages of 25 (rows carry whole payloads, so large pages reach megabytes), routine
auto-approved traffic hidden unless requested. Periodic refresh fetches only the newest page and
merges it over older ones; argument/result editors initialize near the viewport only.

## Perimeter / deploy

Haku cannot mutate or inspect the `haku-console` namespace; browser auth is app-owned. Every new
top-level backend prefix must also be routed by the static nginx shell. API replicas overlap
during a rollout, so stored and cross-replica contracts must tolerate adjacent releases:

### Vocabularies across a roll

New writer → old reader fails transiently (dies with the replica); old writer → new reader fails
permanently (dies with the row). Tolerance fixes the first; only a constraint fixes the second.

Deciding question: could this value come from a newer commit than the reader? If no (request
body, config file, MCP argument, pinned third-party vocabulary), an unknown value is a bug or
attack — <../../STYLE.md> strict mapping applies. If yes, raising is the defect: decode to a named
unknown (`util.enum_vocab.UnknownValue`), never `None` or a nearby member, so every consumer
handles it; cross-replica payload models accept unknown fields. A version-negotiated seam may
reject unknown kinds after its handshake; storage has no handshake.

Writer rollout by vocabulary:

- **Narration** — skippable, append-only (notification kinds, `ConsoleEvent.event_type`): a new
  value ships with its writer; skipped narration is the accepted cost.
- **Decision** — drives behavior (tool-call, provenance, rejection statuses): the reader knowing
  the new member ships one release before the writer. Columns stay strict (no `UnknownValue`
  decoding): tolerance would not lift the two-release rule, since no consumer may guess.
- **Required field added to an existing shape** — a narrowing, not an extension: expand/contract
  plus a constraint making the old writer fail rather than write a permanently misread row.

## Test

```bash
bbr test //haku/console/...
```
