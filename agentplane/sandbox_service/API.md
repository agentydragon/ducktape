# Sandbox Service API

The service uses [protobuf/gRPC](../proto/sandbox_service.proto). Its standalone entry point serves this API;
HTTP is limited to a health probe. The integration app retains its browser-facing HTTP API.
There is no transparent runner tunnel or caller-supplied runner URL.

Production callers and service acceptance tests use this same gRPC interface; there is no separate
HTTP service adapter.

The Sandbox Service serves `SandboxService`: Sandbox inventory and lifecycle, templates and egress
grants. Sessions and commands are the [Runner Gateway's API](../runner_gateway/API.md), its own
server and image, with its own client. Authentication and destinations below apply to both.

## Authentication and destinations

Every RPC requires one `authorization: Bearer …` metadata value. The shared workload-principal
resolver performs TokenReview for the configured audience and allowed ServiceAccount namespaces.
Missing, invalid, duplicate, or revoked credentials are refused. The Python client rereads its
projected token file on every RPC.

A `SandboxDestination` contains the owner ServiceAccount (namespace/name), Sandbox name, and
Sandbox UID. A `SessionDestination` wraps that destination and adds an explicit runner session ID.
The owner is a resource selector, not a forwarded identity. Every RPC requires a caller in the
configured `caller_accounts` service-account allowlist. Authorization happens before any inventory
lookup or runner contact. An ordinary workload token, even for the destination's own account, does
not grant access. There is no separate manager/delivery tier or inferred Thread identity in v1.
Listed services can operate across owners, but must still supply the verified destination owner/UID.

Resolution checks the Sandbox's account, UID and phase, and the current ready
Pod's identity and controller ownership. Name reuse with a new UID is refused. The runner itself
is reached only over the [runner control stream](../docs/runner_control.md) it dialed with a Pod-bound
token.

## Inventory and explicit sandbox lifecycle

The same service-caller allowlist gates every RPC:

- `ListSandboxes`, `GetSandbox`: Kubernetes-backed inventory, including the Sandbox's session
  defaults and bootstrap, its raw status, and a same-name Pod's identity metadata and raw status as
  separate objects. The operating mode is the desired state; the `phase` is derived from what the
  Sandbox Controller records (deletion asked for, `spec.running`, whether an incarnation exists,
  `Quiesced`, the `Running` condition's reason) as Creating, Running, Suspending, Suspended,
  Quiescing (a stop or deletion held for the runner's seal) or Tearing down.
- `ListTemplates`: available SandboxTemplates.
- `CreateSandbox`: concrete template, `policy_sets`, optional session defaults, and bootstrap. The
  requested `name` is the Sandbox's. It runs from the start, bound to `policy_sets` plus the
  configured `default_policy_sets`, which must all name `PolicySet`s in the namespace; a request
  naming egress policies, Action policy sets or Kubernetes grants is refused, a PolicySet holding
  every rule type. Retry with the same name and exact choices while that Sandbox exists; a different
  caller or request conflicts. The RPC returns when the Sandbox is persisted; the Sandbox
  Controller makes its ServiceAccount, `PolicyBinding`, state volume and Pod asynchronously. Its
  template's runner reaches the Runner Gateway and the History Service ingress over the egress
  sidecar's platform routes.
- `GrantEgress`: UID-pinned Sandbox destination and egress policy names; returns the created
  binding name, owned by the Sandbox. `RevokeEgress`: binding name; retains the refusal to delete
  Git-owned bindings.
- `SuspendSandbox`, `ResumeSandbox`, `DeleteSandbox`: explicit owner/name/UID-pinned mutations of
  `spec.running`, or the deletion. `DeleteSandbox` requires suspension and starts
  [teardown](#teardown).

Create retries by exact caller, name, and request while the Sandbox exists; after deletion no
name-only receipt can prove whether a past Create succeeded. The client disables automatic gRPC
retries. Mutations use UID/resourceVersion guards and are safe to replay.

## Teardown

`DeleteSandbox` on a suspended Sandbox deletes it and returns; `GetSandbox` reports its `phase`
(Quiescing, then Tearing down), and repeating the call changes nothing. The Sandbox Controller holds the deletion until the
runner has sealed every Session ([Sandbox Controller](../sandbox_controller/README.md)): it runs the
Sandbox again (`SANDBOX_PHASE_QUIESCING`), which from then on refuses new Sessions and commands, and
suspend and resume, while its Sessions can still be listed and read. The Runner Gateway replica
holding the runner's connection seals it and records `Quiesced`, after which the Sandbox and
its volume go.

## Errors and uncertain outcomes

- `UNAUTHENTICATED`: invalid workload bearer.
- `PERMISSION_DENIED`: authenticated but unlisted service caller.
- `NOT_FOUND`: missing/stale Sandbox incarnation.
- `ABORTED`: `ResumeSandbox` arrived while the controller is quiescing an incarnation; retry when the Sandbox is suspended.
- `INVALID_ARGUMENT`: malformed request or unknown policy set.
- `FAILED_PRECONDITION`: runner or Sandbox state refuses the operation.
- `UNAVAILABLE`: destination/backend unavailable; no offline admission.
- `DEADLINE_EXCEEDED`: operation or transport safety deadline expired.

Neither a successful write nor a timeout proves the outcome: a mutation may commit before its
response is lost. Command outcomes: [Runner Gateway API](../runner_gateway/API.md#errors-and-uncertain-outcomes).

## Server configuration

`//agentplane/sandbox_service:server` uses `AGENTPLANE_SANDBOX_SERVICE_*` environment variables or
kebab-case CLI flags. Required settings are `sandbox_namespace`, `caller_accounts` (nonempty),
`token_audience` and `default_policy_sets`. TokenReview namespaces are derived from that allowlist.
Kubernetes access is in-cluster unless `kubeconfig` is supplied.

`port` defaults to 8080 for gRPC, which also serves the unauthenticated `grpc.health.v1.Health`;
it is liveness, not proof that Kubernetes or a particular destination is ready. Admission requests
are bounded by `admission_timeout_s` (default 15); management uses `lifecycle_timeout_s` (default
300).

Deployment must grant the service the appropriate Kubernetes/TokenReview/lifecycle permissions and
project an audience-correct token for app-to-service calls.

Each service has its own token audience, `agentplane-sandbox-service` and `agentplane-runner-gateway`,
so a token for one is refused by the other. Every caller mounts a rotating projected token per service
it calls, and the client rereads it on every RPC. `AGENTPLANE_SANDBOX_SERVICE_CONFIG_FILE`
can select an independent YAML settings file. It does not load app configuration or require app startup.

## Representation

Generated protobuf messages are the service's request/response and client types, including inventory,
launch defaults and destinations. There is no parallel Pydantic service DTO layer.
The integration app owns its browser-facing HTTP schemas and converts only at that boundary.
Pydantic remains for deployment settings and persisted Kubernetes input parsing; those are not a
second service protocol.
