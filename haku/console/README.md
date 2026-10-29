# haku/console — Haku's interactive console

FastAPI service + React SPA: the operator-owned shell around Haku's cross-origin UI, the
launch-routine capability, and the home of Agent identity. Runs in the
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

## Historical tool-call data

The Console no longer accepts, executes, lists, or decides tool calls. Its tool-call tables,
status enum, grants, and migrations remain in Postgres and in the ORM schema so historical data is
preserved and deployments continue to validate the existing database. There are no live
application readers or writers for those records. The enriched per-server renderers
(`frontend/tool_call_card.tsx`, `frontend/tool_rendering/`) remain a buildable development
component with their MCP schema exports, lookup clients, and per-server preview harnesses. They are
excluded from the production frontend bundle and static image. The cluster's generic Agent Sandbox
constructs remain for other workspaces.

### Canonical Agent authority

Contract: <docs/agent_authority.md>. `Operator`, `Agent`, credential bindings, names, profiles,
and credential grants are durable local identities. Settings lists retained Agents and lets
Operators change eligible profile assignments. Static Agent configuration and reconciliation are
retired; existing static credential rows remain as history, with active bindings revoked by
migration. Access profiles remain deployment-owned identity labels; they do not enable tool
execution or approval policy.

## Free-form UI

The console frames `haku-ui.allegedly.works` full-page in a sandboxed cross-origin iframe beside
a narrow rail of trusted chrome. The frame cannot read console DOM, cookies, credentials,
approvals, or capability routes; a schema-validated `postMessage` Agent UI bridge carries only
requests needing the trusted side, which origin-checks, decides, confirms, and owns revocation.
Full contract: <docs/containment.md>.

## Perimeter / deploy

Haku cannot mutate or inspect the `haku-console` namespace; browser auth is app-owned. Every new
top-level backend prefix must also be routed by the static nginx shell. API replicas overlap
during a rollout, so stored and cross-replica contracts must tolerate adjacent releases:

### Vocabularies across a roll

New writer → old reader fails transiently (dies with the replica); old writer → new reader fails
permanently (dies with the row). Tolerance fixes the first; only a constraint fixes the second.

Deciding question: could this value come from a newer commit than the reader? If no (request
body, config file, pinned third-party vocabulary), an unknown value is a bug or
attack — <../../STYLE.md> strict mapping applies. If yes, raising is the defect: decode to a named
unknown variant, never `None` or a nearby member, so every consumer handles it; cross-replica
payload models accept unknown fields. A version-negotiated seam may
reject unknown kinds after its handshake; storage has no handshake.

Writer rollout by vocabulary:

- **Narration** — skippable, append-only (notification kinds, `ConsoleEvent.event_type`): a new
  value ships with its writer; skipped narration is the accepted cost.
- **Decision** — drives behavior (identity, provenance, rejection statuses): the reader knowing
  the new member ships one release before the writer. Columns stay strict: tolerance would not lift
  the two-release rule, since no consumer may guess.
- **Required field added to an existing shape** — a narrowing, not an extension: expand/contract
  plus a constraint making the old writer fail rather than write a permanently misread row.

## Test

```bash
bbr test //haku/console/...
```
