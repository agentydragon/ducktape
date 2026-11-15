# Runner Gateway

The deployed Runner Gateway serves the Session Dispatcher: durable Session admission, watches,
automatic wake and delivery. Its TLS gRPC listener at `:8080` serves Dispatcher and health methods.
It admits live workload Identity SVIDs, exact service SVIDs and explicitly scoped typed service
delegation. Kubernetes bearers grant no admission. The deployed names and
`spiffe://agentplane/session-dispatcher` server identity stay unchanged.

EnvironmentService, the Runtime proxy and Executor serve on every
[Environments](../environments/README.md) replica at `sandbox-controller:8082`. The Gateway opens no
Runtime connection or `WatchState` stream and registers none of those services. Native Dispatcher
delivery calls Environments under the Sessions SVID (`spiffe://agentplane/sessions`), pinning the
Sandbox Controller server identity. Central delivery calls the sampler. These calls retain the
original command ID and never forward caller credentials. Environments does not call Sessions.

Dispatcher automatic wake preserves the effective caller and applies `wakeOnSend`, configured wake
grants and the shared budget. Its Kubernetes Role still reads workload and policy state, patches
existing Sandboxes for automatic wake and maintains delivery Leases. It cannot create or delete
Sandboxes or read SandboxTemplates. The Controller owns `Sandbox.status`; its leader election and
Runtime observation are independent of Dispatcher scheduling ownership. See the
[Dispatcher contract](dispatcher/SPEC.md) and [Runtime proxy contract](../environments/runtimeproxy/SPEC.md).

The required `rpc` block configures TLS and SPIFFE trust. `gateway_tls` and
`runner_control_target_template` configure the Sessions client's Environments connection; their
historical setting names remain. Retired `runner_security`, `runtime_proxy_readers`,
`runtime_proxy_session_callers`, `runtime_proxy_controller_callers` and `executor_service_callers`
remain decodable without receiver authority until their manifest omission has rolled out.
`runner_security` is optional and no credential file it names is opened by this process. Obsolete
Runtime client certificates and mounts are no longer rendered after the receiver-free image
was pinned and rolled; the Sessions client and Dispatcher listener certificates remain. The Gateway has no direct Runtime
network permission, old Runtime peer admission or proxy grants. Its Kubernetes permissions cover
Dispatcher inventory, admission, automatic wake and delivery ownership, without Pod/VM discovery
or TokenReview authority. Restoring the old receiver for rollback requires its image and credentials before callers
are retargeted.

## Replica ownership decision

The selected any-replica Runtime contract now belongs to Environments: no connection Lease, epoch
or per-Sandbox control EndpointSlice; every proxy replica may call the Controller-bound Sandbox UID
and workload Pod UID with the observed Runtime boot ID. Watch loss closes only that replica's route;
Runtime restart or same-name replacement invalidates stale fences. Uncertain mutations are never
automatically replayed. The [proxy specification](../environments/runtimeproxy/SPEC.md) owns this
contract; the separate Dispatcher delivery Lease remains unchanged.

## Source and tests

- `main.go`, `config.go`: process and listener wiring, and the settings (the required
  `AGENTPLANE_RUNNER_GATEWAY_CONFIG_FILE` path, with all non-secret settings in that file;
  [`settings.py`](settings.py) is the authoring schema). The Dispatcher database URL Secret and
  legacy ignored Pod name are runtime environment inputs. Generic
  `AGENTPLANE_RUNNER_GATEWAY_*` variables do not override file values. The independent `dispatcher_service_delegation`
  policy names an exact service actor and exact Dispatcher RPC methods; it grants no Runtime proxy or
  Executor authority. EnvironmentService roles and delegation belong to the Controller settings.
- `internal/sandboxinventory/`: informer-backed Sandbox reads and the uncached Identity reader that supply the Dispatcher contract.
- `dispatcher/`: durable admission, watch, delivery, guarded wake and scheduling ownership.
- `migrations/`: Goose migrations for the Dispatcher database.
- `client.py`: the Python Runner proxy client; `testing/` provides narrow consumer peers and
  `gateway_process.py` runs the real binary for cross-language acceptance.

Listener settings accept ordinary TCP `host:port` addresses and `fd:N`, where `N`
is the descriptor number **in the child process** (at least 3). An inherited
socket must already be bound and listening on TCP. Invalid descriptors fail startup without
falling back to another address.

Startup follows Go's `ListenAndServe`/`Serve` separation: `listenAndServe` obtains
and owns the listener, while `serve` takes a `net.Listener`. FD parsing and conversion stay at the process
boundary; serving code never handles descriptor numbers. `net.FileListener`
duplicates the descriptor, so startup closes the inherited descriptor after
conversion and closes the resulting listener on exit, including setup failure.
The parent closes its own socket copy after spawning the child.

For Go parents, `exec.Cmd.ExtraFiles` assigns child descriptors `3+i`; Python's
`pass_fds` preserves descriptor numbers. The launcher supplies the corresponding
`fd:N` values. Client addresses come from the parent's held listener, without
startup files or log parsing.

Build `//agentplane/runner_gateway:runner-gateway` or `//agentplane/runner_gateway:image`.
The same binary embeds Goose migrations and applies them before request handling or workers start.
`runner-gateway --migrate-only` initializes a database using the same lifecycle and
`AGENTPLANE_RUNNER_GATEWAY_DISPATCHER_DATABASE_URL`. PostgreSQL session locks serialize simultaneous
startup attempts. Integration tests exercise real PostgreSQL and queue-to-Runner delivery over
the separate Environments mTLS receiver on RBE.
The cross-language Notification acceptance runs a real Kubernetes API server and etcd through
`envtest`, installs the Agentplane CRDs and issues Pod-bound ServiceAccount tokens. Narrow
consumer tests continue using lightweight gRPC peers.

## Deployment

The environment root owns the stable Services, Dispatcher database and Secret. Its dependent
delivery Kustomization owns the serving workload and waits for the environment root to become
Ready. Each Gateway process initializes its schema before serving. The binary and migrations
share one image; schema changes remain compatible with old replicas during rolling updates.

### Schema changes

1. **Additions.** A new Pod migrates before its Gateway process starts, while old Pods may still
   serve. Keep the migration compatible with both the old and new Gateway versions; the schema
   addition and code that uses it can ship together when that holds.
2. **Removals.** First stop serving code from using a column or table and let old Pods leave the
   Deployment. Drop it in a later migration.

`//agentplane/runner_gateway/migrations:test_migrations` covers fresh schema creation,
idempotent application and concurrent migrations.

## Optional central sampler

The optional `central_sampler` configures one gRPC target and rotating caller token file.
Omitting it disables central Session admission. `SessionSpec.placement` records either the exact
native environment and harness (`environment_hosted`) or the exact required central executor
environment (`central_sampler_v1`); callers do not choose a service endpoint. The Dispatcher sends
central creation requests and commands to the configured sampler; wrapped native Sessions continue through
ProxyRunner. Central controls bypass executor readiness/wake checks after Session creation.
Central sampler V1 retains the existing Sandbox identity and requires the bound Sandbox object; it
does not support optional attachments or environment-free Sessions.

Executor runs in Environments, independently of Dispatcher admission. The sampler presents its own
narrow service SVID grant there; this process has no Executor receiver. See the
[central sampler contract](../central_sampler/SPEC.md) and the
[Runtime proxy contract](../environments/runtimeproxy/SPEC.md).
