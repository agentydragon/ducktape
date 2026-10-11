# Sandbox Service

Independent backend owner for Sandbox lifecycle: provisioning, suspend/resume and deletion,
templates, Kubernetes and egress grants, retention holds, teardown, and the runner endpoints each new
Sandbox's runner dials. It has no database; Kubernetes is its inventory. Sessions, commands and
runner access belong to the [Runner Gateway](../runner_gateway/README.md). It does not own
notification payloads or inboxes, Action Decisions or execution, native harness scheduling, browser
sessions, or UI-only Thread projections. The [service dependency rule](../docs/service_boundaries.md)
applies to this package.

## Inventory and launch configuration

`inventory.py` owns the Kubernetes-backed inventory and low-level Sandbox lifecycle operations.
`session_config.py` owns the concrete, serialized launch fields; `kubernetes_grants.py` owns the
selected grant shapes. Consumers import public models and read-only projections, not these mutation
implementations. UI preset catalogs remain app-owned and are not interpreted by this package. The
API and the binding annotation use `SessionDefaults` / `session_defaults`.

`provisioning.py` owns recoverable grant orchestration and policy binding, with pending launch
intent stored on the Sandbox. `controller.py` reconciles Sandbox initialization, grants and
teardown; `teardown.py` has the Runner Gateway seal each Session before deleting the Sandbox.

## API

The [authenticated gRPC API](API.md) has a standalone server entry point and Python client
(`client.py`), which also calls the Runner Gateway. This service serves `SandboxService`: Sandbox
lifecycle, provisioning, egress grants and retention holds. It calls the Runner Gateway only to seal
a Sandbox for teardown.

## Deployment

A separately built image, Deployment/Service/ServiceAccount, TokenReview and lifecycle/grant RBAC,
and network isolation. One replica reconciles. Its callers are the app, the Notification Service and
the History Service; it reaches the Kubernetes API and the Runner Gateway. App-to-service calls
require a Pod-bound workload TokenReview.
