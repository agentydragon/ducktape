# Sandbox Service

Independent backend owner for sandbox lifecycle and runner session access. The [plan](../plans/sandbox_service.md)
describes the intended service; the [service dependency rule](../docs/service_boundaries.md) applies to this package.

## Implemented foundation

`command_relay.admit_running_command` extracts the existing app's running-session command relay. The standalone service
uses it; the app calls the service while retaining its existing archive wait and error mapping. The helper:

- Opens an explicit runner session without a spec, so it cannot create or resume it.
- Sends the caller's unchanged common-protocol `Command` and requests detach.
- Waits for the runner's exact `CommandAdmitted` and returns its original `EventEntry`.
- Cancels the attachment on every exit, without taking ownership of the client.

Admission is not native user-message confirmation, model completion, or inbox acknowledgement. An uncertain result needs
reconciliation against the runner journal with the same command ID/payload and an appropriate runner-log replay cursor.
This helper adds no retry, command queue, Event authority, or exactly-once guarantee.

Protocol-edge tests use a controllable gRPC peer. Native tests exercise both harnesses with scripted model endpoints,
without the integration app or its database. They cover admission before model completion, receipt replay, and refusing
to create/resume a session.

## Inventory and concrete launch configuration

`inventory.py` now owns the existing Kubernetes-backed inventory and low-level Sandbox lifecycle operations.
`session_config.py` owns the concrete, serialized launch fields; `kubernetes_grants.py` owns the selected grant shapes.
Consumers import public models and read-only projections, not these mutation implementations. UI preset catalogs remain
app-owned and are not interpreted by this package. The API uses `SessionDefaults` / `session_defaults`;
`binding_storage.py` alone preserves the legacy annotation field spelling for data preservation and rollback. Annotation
keys, defaults, ServiceAccount creation, PVC policy, and provisioning behavior are unchanged.

`provisioning.py` owns recoverable grant orchestration and policy binding, with pending launch intent stored on the
Sandbox. The production app now uses the remote client, not an in-process provisioner or reconciler. The deployment
source transfers these authorities to the service; live handoff remains gated below.

## Session API

The [authenticated gRPC API](API.md) has a standalone server entry point and Python client. It resolves SA-authorized,
UID-pinned destinations inside the configured Kubernetes inventory, then inspects, commands, or follows existing runner
sessions. Separately authorized explicit management routes list sessions, bootstrap, open, and resume using
runner-retained specs. The backend owns launch instructions and concrete defaults; no UI preset lookup is needed.
Read/command/follow never start sessions. Separate explicit Sandbox lifecycle operations can resume a Sandbox.
Read/follow availability is limited to the surviving runner log.

## Production app cutover and rollout

The production app requires `sandbox_service_target` and a projected workload token. Its directory, bridge, and ingester
use the service client with no direct-runner fallback. UI preset selection, read-only Kubernetes projections, PostgreSQL
archive, and ingestion checkpoints remain app-owned. App tests use the service-owned gRPC fixtures; there is no
alternate direct-runner app directory. App/service integration acceptance uses native harnesses and the existing app
database archive.

The deployment source adds a separately built service image, Deployment/Service/ServiceAccount, TokenReview and
lifecycle/grant RBAC, and network isolation. The app has read-only resource RBAC; only Sandbox Service can reach the
runner control port. Cross-namespace delegation keeps its existing Role/RoleBinding identities and changes the grantee.
Runner template/volume identities and archive schema are unchanged. Service-native acceptance tests exercise
authenticated gRPC, including launch defaults, bootstrap/setup, delivery evidence, and recovery after service restart.

**Source changes are not evidence of a live cutover.** Image publication/pinning, generated-manifest validation, and the
staging preservation/rollback gate must complete before rollout. Do not let Flux independently switch RBAC/network
policies while an old app image is still running. See the
[handoff checklist](../plans/sandbox_service.md#production-handoff-checklist).

The service owns no session-log archive. Runner logs remain on their state volume; clients needing independent retention
must archive events themselves. The app keeps its existing PostgreSQL archive and checkpoints. Archive migration is not
a required follow-up.

TODO: proper runner RPC authentication/TLS. V1 uses network isolation for service-to-runner traffic, not runner command
RBAC. App-to-service calls already require a Pod-bound workload TokenReview.
