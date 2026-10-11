# Sandbox Service

Independent backend owner for sandbox lifecycle and runner session access.
It does not own notification payloads or inboxes, Action Decisions or execution, native harness
scheduling, browser sessions, or UI-only Thread projections. The
[service dependency rule](../docs/service_boundaries.md) applies to this package.

## Reaching runners

Runners dial the [Runner Gateway](../runner_gateway/README.md) over the
[runner control stream](../docs/runner_control.md); nothing connects to a runner Pod. A command is
admitted once the runner's journal records the exact `CommandAdmitted` and returns its original
`EventEntry`. Admission is not native user-message confirmation, model completion, or inbox
acknowledgement. An uncertain result is settled by the caller retrying the same command ID and
payload; there is no exactly-once guarantee beyond that.

Protocol-edge tests script the runner's end of the control stream. Native tests exercise both
harnesses with scripted model endpoints, without the integration app or its database.

## Inventory and concrete launch configuration

`inventory.py` now owns the existing Kubernetes-backed inventory and low-level
Sandbox lifecycle operations. `session_config.py` owns the concrete, serialized
launch fields; `kubernetes_grants.py` owns the selected grant shapes. Consumers import public models and read-only projections, not these mutation implementations. UI preset catalogs remain app-owned and are not interpreted
by this package. The API and the binding annotation use `SessionDefaults` / `session_defaults`. Annotation keys, defaults,
ServiceAccount creation, PVC policy, and provisioning behavior are unchanged.

`provisioning.py` owns recoverable grant orchestration and policy binding, with pending launch
intent stored on the Sandbox. The production app now uses the remote client, not an in-process provisioner or reconciler.
The deployment source transfers these authorities to the service.

## Session API

The [authenticated gRPC API](API.md) has a standalone server entry point and Python client. This
service serves Sandbox lifecycle and provisioning (`SandboxService`); the
[Runner Gateway](../runner_gateway/README.md) serves Sessions and commands (`RunnerGateway`). It resolves
SA-authorized, UID-pinned destinations inside the configured Kubernetes inventory, then
lists and commands existing runner sessions. Separately authorized explicit management routes
bootstrap, open, and resume using runner-retained specs. The backend owns launch instructions and
concrete defaults; no UI preset lookup is needed. Commands never start sessions. Separate explicit
Sandbox lifecycle operations can resume a Sandbox.

## Production app cutover and rollout

The production app requires `sandbox_service_target` and a projected workload token. Its directory,
bridge, and ingester use the service client with no direct-runner fallback. UI preset selection,
read-only Kubernetes projections, UI folds remain app-owned; PostgreSQL raw history is described below.
App tests use the service-owned gRPC fixtures; there is no alternate direct-runner app directory.
App/service integration acceptance uses native harnesses and the existing app database archive.

The deployment source adds a separately built service image, Deployment/Service/ServiceAccount,
TokenReview and lifecycle/grant RBAC, and network isolation. The app has read-only resource RBAC,
and runner Pods admit no connections. Cross-namespace delegation keeps its
existing Role/RoleBinding identities and changes the grantee. Runner template/volume identities
and archive schema are unchanged. Service-native acceptance tests exercise authenticated gRPC,
including launch defaults, bootstrap/setup, delivery evidence, and recovery after service restart.

Runner journals remain on their state volume as execution evidence, and the runner journal still
authors execution Events. Retained history lives in the [History Service](../history_service/README.md)'s PostgreSQL.
The Session registry (public Session IDs, runner locators and Open reservations) lives in the service's own `sandbox_commands` database beside command admission.

Runners authenticate to the gateway with a Pod-bound token the egress proxy substitutes on their
control stream. App-to-service calls require a Pod-bound workload TokenReview.
