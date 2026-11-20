# Session Dispatcher

Sessions serves Dispatcher and gRPC health at `sessions:8080` for durable Session admission,
watches, automatic wake and delivery. The TLS listener presents
`spiffe://agentplane/session-dispatcher` and admits live workload Identity SVIDs, exact service
SVIDs and explicitly scoped typed service delegation.

Native delivery calls HarnessHost through the [Environments](../environments/README.md) Service
at `environments:8082`, presenting `spiffe://agentplane/sessions` and verifying the Sandbox
Controller SVID. Central delivery calls the sampler. Both paths use the same HarnessHost client
for creation and commands, selecting the destination and routing metadata. The client uses the
original command ID and its own credentials. Environments owns the Runtime connections and watches.

Dispatcher automatic wake retains each row's attribution and applies `wakeOnSend`, caller policy or
configured service-broker grants, and the shared budget. Action-attributed rows use the Actions
service's Dispatcher broker grant while keeping the effective Identity in attribution. Its
Kubernetes Role reads workload and policy state and patches existing Sandboxes for automatic wake.
Delivery uses per-Session PostgreSQL leases. The Role cannot create or delete Sandboxes or read
SandboxTemplates. The Controller owns `Sandbox.status`; its leader election and Runtime observation
are independent of Dispatcher scheduling ownership. See the
[Dispatcher contract](dispatcher/SPEC.md) and [Runtime proxy contract](../environments/runtimeproxy/SPEC.md).

The required `rpc` block configures TLS and SPIFFE trust. `gateway_tls` and
`runner_control_target_template` configure the Sessions client's Environments connection; their
historical setting names remain. The Sessions Role covers Dispatcher inventory, admission and
automatic wake. Sessions calls Runtime through the Environments proxy over its dedicated network
path and exact method grant.

## Replica ownership decision

The selected any-replica Runtime contract now belongs to Environments: no connection Lease, epoch
or per-Sandbox control EndpointSlice; every proxy replica may call the Controller-bound Sandbox UID
and workload Pod UID with the observed Runtime boot ID. Watch loss closes only that replica's route;
Runtime restart or same-name replacement invalidates stale fences. Uncertain mutations are never
automatically replayed. The [proxy specification](../environments/runtimeproxy/SPEC.md) owns this
contract; Dispatcher delivery uses a timed per-Session PostgreSQL lease.

## Source and tests

Source lives under `agentplane/session_dispatcher/`: `cmd/` owns signal/log setup,
`internal/app/` owns deployment settings, startup, migrations and server composition,
`dispatcher/` owns Session admission/delivery, `internal/store/` owns persistence, and
`internal/sandboxinventory/` owns its live Sandbox catalog. CI publishes the image as
`agentplane-sessions`.

- `cmd/main.go`, `internal/app/` implement process and listener wiring. The process loads strict
  YAML settings from `AGENTPLANE_SESSIONS_CONFIG_FILE` using the authoring schema in
  [`settings.py`](settings.py). The database URL comes from the
  `AGENTPLANE_SESSIONS_DISPATCHER_DATABASE_URL` Secret input. The independent
  `dispatcher_service_delegation` policy scopes each service actor to exact Dispatcher RPC
  methods. Environments owns its own roles and delegation settings.
- `internal/sandboxinventory/`: informer-backed Sandbox reads and the uncached Identity reader that supply the Dispatcher contract.
- `dispatcher/`: durable admission, watch, delivery, guarded wake and scheduling ownership.
- `internal/store/`: Session and dispatch persistence, leases, retention and notification reads.
- `migrations/`: Goose migrations for the Dispatcher database.
- `client.py`: the Python Environments Runtime proxy client; `testing/` provides narrow consumer peers and
  `gateway_process.py` runs the real binary for cross-language acceptance.

`rpc.listen_address` accepts a TCP address or an inherited `fd:N` as documented by
[`agentplane/net/listener`](../net/listener/README.md).

Build `//agentplane/session_dispatcher/cmd:runner-gateway` or `//agentplane/session_dispatcher:image`.
The same binary embeds Goose migrations and applies them before request handling or workers start.
`runner-gateway --migrate-only` initializes a database using the same lifecycle and
`AGENTPLANE_SESSIONS_DISPATCHER_DATABASE_URL`. PostgreSQL session locks serialize simultaneous
startup attempts. Integration tests exercise real PostgreSQL and queue-to-Runner delivery over
the separate Environments mTLS receiver on RBE.
The cross-language Notification acceptance runs a real Kubernetes API server and etcd through
`envtest`, installs the Agentplane CRDs and issues Pod-bound ServiceAccount tokens. Narrow
consumer tests continue using lightweight gRPC peers.

## Deployment

The environment root owns the `sessions` Service, Dispatcher database and Secret. Its dependent
delivery Kustomization owns the serving workload and waits for the environment root to become
Ready. Each Sessions process initializes its schema before serving. The binary and migrations
share one image; schema changes remain compatible with old replicas during rolling updates.

### Schema changes

1. **Additions.** A new Pod migrates before its Sessions process starts, while old Pods may still
   serve. Keep the migration compatible with both the old and new Sessions versions; the schema
   addition and code that uses it can ship together when that holds.
2. **Removals.** First stop serving code from using a column or table and let old Pods leave the
   Deployment. Drop it in a later migration.

`//agentplane/session_dispatcher/migrations:test_migrations` covers fresh schema creation,
idempotent application and concurrent migrations.

## Optional central sampler

The optional `central_sampler` configures one gRPC target and its service TLS credentials.
Omitting it disables central Session admission. `SessionSpec.placement` records either the exact
native environment and harness (`environment_hosted`) or the exact required central executor
environment (`central_sampler_v1`); callers do not choose a service endpoint. The Dispatcher sends
HarnessHost creation requests and commands to the configured sampler; wrapped native Sessions use
the same [`HarnessHostClient`](dispatcher/runtime_client.go) through the Environments proxy. Central
controls bypass executor readiness/wake checks after Session creation.
Central sampler V1 retains the existing Sandbox identity and requires the bound Sandbox object; it
does not support optional attachments or environment-free Sessions.

Executor runs in Environments, independently of Dispatcher admission. The sampler presents its own
narrow service SVID grant there; this process has no Executor receiver. See the
[central sampler contract](../sampler/SPEC.md) and the
[Runtime proxy contract](../environments/runtimeproxy/SPEC.md).
