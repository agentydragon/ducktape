# Sandbox Service API

The service uses [protobuf/gRPC](../proto/sandbox_service.proto). Its standalone entry point serves this API;
HTTP is limited to a health probe. The integration app retains its browser-facing HTTP API.
There is no transparent runner tunnel or caller-supplied runner URL.

Production callers and service acceptance tests use this same gRPC interface; there is no separate
HTTP service adapter.

The Sandbox Service serves `SandboxService`: Sandbox inventory and lifecycle, templates and egress
grants. One replica reconciles, and it alone holds the RBAC, ServiceAccount and egress grant
permissions. Sessions and commands are the [Runner Gateway's API](../runner_gateway/API.md), its own
server and image. `SandboxServiceClient` takes one target per service: `target` for the Sandbox
Service and `gateway_target` for the Runner Gateway; authentication and destinations below apply to
both.

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

Resolution checks the stored Sandbox account, UID, operating mode, provisioning/grant facts, and current
ready Pod's identity and controller ownership. Name reuse with a new UID is refused. The runner itself
is reached only over the [runner control stream](../docs/runner_control.md) it dialed with a Pod-bound
token.

## Inventory and explicit sandbox lifecycle

The same service-caller allowlist gates every RPC. Provisioning is always enabled:

- `ListSandboxes`, `GetSandbox`: Kubernetes-backed inventory, including concrete stored launch
  bindings, the Sandbox CR's raw status, and a same-name Pod's identity metadata and raw status as
  separate objects. Operating mode, launch-grants-pending, and Kubernetes-grant facts remain
  separate fields; the service does not synthesize one status from them.
- `ListTemplates`: available SandboxTemplates. The app renders its configured grant catalog locally.
- `CreateSandbox`: concrete template, egress policy selections, Action policy sets, Kubernetes grants,
  optional session defaults, and bootstrap. The requested `name` is the Kubernetes Sandbox CR name.
  Retry with the same name and exact choices while that CR exists; a different caller or request
  conflicts. Create first records intent on a Suspended CR, then ensures its UID-owned ServiceAccount
  and grants before resuming. A persisted initialization marker lets reconciliation recover without
  the app. The RPC returns when the CR is persisted; ServiceAccount, grants, and Pod readiness
  are asynchronous.
  After deletion, a new CR under the same name cannot prove whether an earlier Create succeeded.
  The service starts the template's `runner` container with the configured `runner_ingress` and
  Runner Gateway (`runner_control`) as `--ingress-target`/`--ingress-bearer` and
  `--control-target`/`--control-bearer`, and grants each endpoint's `egress_policy` with the launch
  policies ([Event ingress](../docs/runner_control.md#event-ingress)); the request chooses neither.
  A template whose runner already passes one of those flags is refused. No request may pick an
  endpoint's egress policy, at creation or through `GrantEgress`.
- `GrantEgress`: UID-pinned Sandbox destination and egress policy names; returns the created binding name.
  `RevokeEgress`: binding name; retains the refusal to delete Git-owned bindings. Both require the
  same service-caller authorization as other operations.
- `SuspendSandbox`, `ResumeSandbox`, `DeleteSandbox`: explicit owner/name/UID-pinned mutations.
  Resume refuses incomplete provisioning. `DeleteSandbox` requires suspension and starts
  [teardown](#teardown).

Create retries by exact caller, name, and request while the CR exists; after deletion no
name-only receipt can prove whether a past Create succeeded. The client disables automatic gRPC
retries.

The Sandbox Service controller lists and watches its managed Sandbox CRs from a Kubernetes
`resourceVersion`. It queues changed names, reads the current incarnation for each reconciliation,
and rate-limits failures per name. A startup and periodic full sweep repairs missed events and
orphaned external grants even while the watch is unavailable. Multiple replicas may reconcile the
same name: mutations use UID/resourceVersion guards and are safe to replay. The upstream Sandbox
controller owns `status` and Pod lifecycle; Agentplane's Create intent and progress live in its
namespaced annotations, not competing Sandbox status conditions.

## Teardown

`DeleteSandbox` on a suspended Sandbox returns once teardown has started; `GetSandbox` reports its
`teardown` phase, and repeating the call changes nothing. The controller then:

1. Runs the Sandbox again (`TEARDOWN_PHASE_SEALING`). From now on it refuses new Sessions and
   commands, and suspend and resume; its Sessions can still be listed and read.
2. Asks the Runner Gateway to seal it (`RunnerGateway.SealSandbox`, through the Sandbox's control
   Service; only the gateway's `sealer_accounts` may call it). The gateway calls the runner's
   [`Seal`](../runner/SPEC.md#teardown-seal), which stops every harness and returns
   each Session's final cursor.
3. Deletes the Sandbox, and its volume, once the runner's ingress has acknowledged every seal
   (`flushed`), sealing again every 10 s until then: history then has every Session through its
   seal.

While the runner is unreachable the Sandbox stays in `TEARDOWN_PHASE_SEALING`, retried with backoff,
for the operator to decide: nothing deletes an unsealed Sandbox.

## Errors and uncertain outcomes

- `UNAUTHENTICATED`: invalid workload bearer.
- `PERMISSION_DENIED`: authenticated but unlisted service caller.
- `NOT_FOUND`: missing/stale Sandbox incarnation.
- `INVALID_ARGUMENT`: malformed request or invalid concrete grant selection.
- `FAILED_PRECONDITION`: runner or Sandbox state refuses the operation.
- `UNAVAILABLE`: destination/backend unavailable; no offline admission.
- `DEADLINE_EXCEEDED`: operation or transport safety deadline expired.

Neither a successful write nor a timeout proves the outcome: a mutation may commit before its
response is lost. Command outcomes: [Runner Gateway API](../runner_gateway/API.md#errors-and-uncertain-outcomes).

## Server configuration

`//agentplane/sandbox_service:server` uses `AGENTPLANE_SANDBOX_SERVICE_*` environment variables or
kebab-case CLI flags. The Sandbox Service reaches the gateway at `gateway_target`, and a Sandbox's
holder replica through `control_target`, with the token in `gateway_token_file`; the gateway accepts
that account for `SealSandbox` through `sealer_accounts`.
Required settings are `sandbox_namespace`, `caller_accounts` (nonempty), `token_audience`,
`gateway_target`, `control_target` and `gateway_token_file`. TokenReview namespaces are derived from
that allowlist. Kubernetes access is in-cluster unless `kubeconfig` is supplied.

`port` defaults to 8080 for gRPC, which also serves the unauthenticated `grpc.health.v1.Health`;
it is liveness, not proof that Kubernetes or a particular destination is ready. Admission requests
are bounded by `admission_timeout_s` (default 15); management uses `lifecycle_timeout_s` (default
300).

Deployment must grant the service the appropriate Kubernetes/TokenReview/provisioning permissions
and project an audience-correct token for app-to-service calls, with one provisioning reconciler.

Each service has its own token audience, `agentplane-sandbox-service` and `agentplane-runner-gateway`,
so a token for one is refused by the other. Every caller mounts a rotating projected token per service
it calls, and the client rereads it on every RPC. `AGENTPLANE_SANDBOX_SERVICE_CONFIG_FILE`
can select an independent YAML settings file. It does not load app configuration or require app startup.

## Representation

Generated protobuf messages are the service's request/response and client types, including inventory,
launch defaults, destinations, and resolved grants. There is no parallel Pydantic service DTO layer.
The integration app owns its browser-facing HTTP schemas and converts only at that boundary.
Pydantic remains for deployment settings, catalog validation, and persisted Kubernetes input parsing;
those are not a second service protocol. Existing annotation spellings and optional-field presence
are retained for staging compatibility and rollback.
