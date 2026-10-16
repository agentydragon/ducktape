# Agentplane service boundaries

The integration app is a user-facing composition layer, not a backend dependency. The former
Sandbox Service is not deployed: the app submits Sandbox desired state and operator grants through
Kubernetes as the requesting operator, and the Sandbox Controller reconciles resources and owns
`Sandbox.status`.

## The integration app is a client

Other Agentplane services must not depend on the app. The dependency direction is app to services
and shared infrastructure, never services to the app.

```mermaid
flowchart LR
    App[Integration app] --> Kubernetes[Kubernetes API: Sandbox spec and operator grants]
    App -->|health probe| Gateway[Runner Gateway]
    App -->|UID/owner-fenced live checks and native resume| Proxy[Per-Sandbox Runner Proxy]
    App -->|openings and durable commands| Dispatcher[Session Dispatcher]
    Dispatcher -->|delivery through runner proxy| Gateway
    App --> History[History Service]
    App --> Notifications[Notification service]
    App --> Actions[Action Service]
    Notifications --> Kubernetes
    Notifications --> Gateway
    Notifications --> History
    Notifications --> Actions
    Controller[Sandbox Controller] --> Kubernetes
    Controller --> Gateway
    Gateway --> Runners[Runners]
    Proxy --> Runners
```

The graph shows ownership direction, not every infrastructure dependency.

For backend operations, this forbids:

- Calling the app's Thread, runner, provisioning, or private identity endpoints as a service API.
- Reading app-owned tables or importing `agentplane.app` implementation modules from another backend.
- Depending on the app's process/attachment, a browser session, or an app-issued ticket merely to act
  as a service. Shared authentication and independently owned operator authority are different things.
- Making the app the only source of session instructions, provider configuration, or destination IDs
  needed to create or manage sessions independently.
- A temporary app API or proxy that becomes a prerequisite for notification delivery.

A shared helper belongs in an independently owned package. Backend records belong to their backend
owner; a shared database server does not make private tables a supported API. The app can retain
presentation state and projections, but those must not become hidden sources of backend authority.

## Ownership

- **Integration app:** user-facing composition and interaction. As the requesting operator, it writes
  Sandbox `spec` and operator-owned `PolicyBinding` grants through Kubernetes. It reads Sandbox
  projections for the UI, opens Sessions and submits commands through the Session Dispatcher, uses
  the Runner Gateway for placement, and uses each Sandbox's Runner Proxy for UID/owner-fenced live
  inventory and explicit native resume. Resume keeps the Thread's History-backed name as a routing
  key, then verifies the current runner's retained Session and spec before calling
  `Runner.ResumeSession`; it does not read the Gateway registry. The legacy Gateway Resume RPC
  remains separately gated on external and pinned callers. The app reads Session history and Threads
  from the History Service. See the [control-surface audit](control_surface_retirement.md).
- **Sandbox Controller:** reconciles requested Sandbox state into ServiceAccounts, Pods or VMs,
  state volumes and controller-owned grants. It owns `Sandbox.status`, including quiescing facts,
  and asks the Runner Gateway to seal Sessions before stopping or deleting a runner.
- **Runner Gateway:** runner control stream, verified destination bindings, a legacy partial Session
  placement registry, the per-Sandbox Runner Proxy and read-only Sandbox inventory. Only its direct
  `CreateSession` writes the registry; Dispatcher openings are absent. The app uses the proxy for
  live inventory and native resume. Its command wrapper and redundant live operations remain on the
  retirement path after callers migrate; the Dispatcher owns sender-facing durable command admission
  and delivery state.
- **History Service:** the Session Event archive, the ingress runners push their journals to, the
  Thread fold of each Session, and the read API over both. It retains the existing `sandbox_service`
  PostgreSQL database and its authored history.
- **Runner:** native harness scheduling and execution, durable command journal, canonical execution
  Events and causal receipts.
- **Notification service:** providers, subscriptions, persisted payloads, inbox HWM, notice policy,
  and notice delivery bookkeeping.
- **Action Service:** Action authorization and Decisions, execution lifecycle, and canonical Action
  history.

`app/threads/` owns only the app's Thread metadata, projections, and browser-facing composition. Its
session adapter uses the History Service; it does not own Sandbox reconciliation or connect directly
to runners. Thread identities never enter Runner Gateway APIs.

## Session access without the app

The Runner Gateway resolves explicit session destinations and distinguishes available, unavailable,
and permanently removed destinations without querying the app. The Session Dispatcher owns durable
sender-facing command admission and delivery state, and uses the Gateway's runner proxy to reach the
runner. The Gateway still exposes a legacy command-admission wrapper while its retirement is gated
on caller migration. The Sandbox Controller reconciles each submitted desired state and uses
the Gateway's runner proxy for quiescing; neither service depends on the app process to continue its work.
Notification-triggered wake and durable acceptance for offline destinations are separate features
with their own rollout gates. Do not create another runner-command queue as a side effect of a
service-boundary change.

Runner journals remain durable on runner state volumes. Runners push them to the History Service,
whose Session Event archive outlives the runner volume; the app reads history from that archive and
keeps its UI projections.

The Runner Gateway and other backend components expose their own APIs; the app retains its
browser-facing HTTP API. The Runner Gateway is the only production server of the runner control
stream. It does not proxy runner outbound model, Actions, or egress traffic. Administrative and test
exceptions must be explicit; there is no production direct-runner fallback in the app or
notifications.

## Existing staging data

Backend independence is not a reason to discard the current staging instance. When state moves
between services, preserve existing data and identities, with an inventory, tested migration/restore
path, and controlled ownership handoff. One-time migration of app-owned records is distinct from a
steady-state dependency on app tables. See [staging data preservation](staging_data_preservation.md).
Any necessary loss/reset must be described and explicitly approved before execution; the general
staging-disposability rule in [`../AGENTS.md`](../AGENTS.md) is not permission to bypass this
requirement.

## Review and acceptance gates

- Reject reverse imports, app API clients, app-table reads, or app-only bootstrap dependencies in
  backend code. Make this rule visible to coding agents in [`../AGENTS.md`](../AGENTS.md).
- Give each record, queue, lifecycle operation, and authorization decision one owner. Avoid
  overlapping command authorities or implicit persistence semantics.
- Exercise notification ingestion/subscription, payload read/ack, command delivery, and receipt replay
  with the app unavailable. Sandbox desired state is an operator request; reconciliation and runner
  sealing remain owned by the controller and Gateway.
- Verify restart/recovery does not depend on the app process after desired state has been accepted.
- Test authorization failures and preserve the distinction between accepted intent, runner admission,
  and harness effect. Human decision requirements stay in their backend authority even if the UI is down.

## Sandbox lifecycle and Runner Gateway callers

The Sandbox Service API, server source, and Deployment are retired. The History Service retains the
existing `sandbox_service` database and its authored Session history. The app submits Sandbox desired
state and operator grants through the Kubernetes API as the requesting operator; the Sandbox
Controller reconciles `spec` and owns `status`.

Shared read-only Sandbox projections may be used by the app, notifications and Runner Gateway. They
carry no create/delete/grant/session authority. Gateway and History protobuf/client types remain
service-owned APIs; there is no Sandbox Service API or client.

Thread launch defaults live in the app's configured preset catalog and are selected by the preset
label on a Sandbox. The shared Sandbox creation request has no per-Sandbox Session defaults. The
shared Sandbox read model carries the Sandbox's bootstrap script but no launch defaults. The app
resolves a complete Session spec from the preset before sending its opening to the Dispatcher. No
database schema, identities, or retained logs change with package renames.
