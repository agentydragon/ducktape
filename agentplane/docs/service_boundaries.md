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
    App --> Gateway[Runner Gateway]
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
  projections for the UI, calls the Runner Gateway for Sessions and commands, and reads Session
  history and Threads from the History Service.
- **Sandbox Controller:** reconciles requested Sandbox state into ServiceAccounts, Pods or VMs,
  state volumes and controller-owned grants. It owns `Sandbox.status`, including quiescing facts,
  and asks the Runner Gateway to seal Sessions before stopping or deleting a runner.
- **Runner Gateway:** runner control stream, verified destination bindings, Session registry,
  authorized runner-session access, command admission and delivery, receipt reads, the
  `sandbox_commands` database, and read-only Sandbox inventory.
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

The Runner Gateway resolves explicit session destinations, exposes authorized command delivery and
receipt reads, and distinguishes available, unavailable, and permanently removed destinations
without querying the app. The Sandbox Controller reconciles each submitted desired state and uses the
Gateway's runner proxy for quiescing; neither service depends on the app process to continue its work.
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
label on a Sandbox. New app Sandbox requests do not write per-Sandbox Session defaults. The existing
`SandboxBinding.session_defaults` field remains in the shared read model while its old producers and
consumers are retired; removing that field is a later rollout step. No database schema, identities,
or retained logs change with package renames.
