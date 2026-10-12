# Sandbox Service

Independent backend owner for Sandbox lifecycle: creation, suspend/resume and deletion, templates
and egress grants. It has no database; Kubernetes is its inventory. Sessions, commands and runner
access belong to the [Runner Gateway](../runner_gateway/README.md). It does not own notification
payloads or inboxes, Action Decisions or execution, native harness scheduling, browser sessions, or
UI-only Thread projections. The [service dependency rule](../docs/service_boundaries.md) applies to
this package.

## Inventory and creation

`inventory.py` owns the Kubernetes-backed inventory and the Sandbox lifecycle operations on
Agentplane's `Sandbox` (`agentplane/sandbox_resource.py`). The
[Sandbox Controller](../sandbox_controller/README.md) does the rest: the Sandbox's ServiceAccount,
`PolicyBinding`, state volume and Pod, and its teardown. A view comes from the Sandbox's spec and
conditions and its Pod (`kubernetes_views.py`). `provisioning.py` validates a create request and
adds the deployment's default policy sets. UI preset catalogs remain app-owned and are not
interpreted by this package.

Nothing here calls the History Service or the Runner Gateway.

## API

The [authenticated gRPC API](API.md) has a standalone server entry point and Python client
(`client.py`). This service serves `SandboxService`: Sandbox lifecycle and egress grants.

## Deployment

A separately built image, Deployment/Service/ServiceAccount, TokenReview and lifecycle/grant RBAC,
and network isolation. Its callers are the app and the Notification Service; it reaches the
Kubernetes API alone. App-to-service calls require a Pod-bound workload TokenReview.
