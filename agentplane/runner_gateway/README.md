# Runner Gateway

The Runner Gateway combines durable Session Dispatcher admission and delivery, the
`EnvironmentService` inventory, management and explicit-resume API, and a Sandbox-fenced Runner proxy. Explicit
resume and Dispatcher automatic wake use the shared lifecycle manager. Automatic wake preserves the
effective caller and applies the Sandbox's `wakeOnSend` setting and the configured `wake_callers` grant.
EnvironmentService and Dispatcher share the Gateway listener and Dispatcher TokenReview audience.
The Gateway dials each Runner's native gRPC
server on private port `9090`. One TLS gRPC listener serves Dispatcher, EnvironmentService,
Gateway proxy, Executor and health methods, with independent per-method authorization.
Only the Pod-bound Gateway/Dispatcher workload can proxy Runtime Session creation and commands;
`gateway_callers` grants read-only diagnostics. Startup reviews `runner_token_file` with the Gateway
audience and verifies its Pod name, current UID and ServiceAccount against Kubernetes before serving.
The Kubernetes API must answer that TokenReview and Pod read; failure prevents startup.
See the [proxy contract](gateway/SPEC.md). The Sandbox
Controller owns `Sandbox.status`.

The required `rpc` settings block configures this shared TLS listener. It enables SPIFFE and
Kubernetes bearer authentication, with exactly the Dispatcher and Gateway audiences. Dispatcher
and EnvironmentService methods use the Dispatcher audience; Gateway proxy and Executor methods use
the Gateway audience. Dispatcher and EnvironmentService methods also accept a verified Sandbox
SVID. Each method retains its existing caller allowlist, delegation, and effective-caller checks.
Calls select one enabled credential: an invalid presented SVID cannot fall back to bearer
authentication, and certificate plus authorization or delegation credentials are rejected as
ambiguous. The Gateway's calls through each Sandbox control Service use `gateway_tls` to verify
the Session Dispatcher server SVID and present only their projected Gateway bearer.

Cluster and k3s Gateways and Runners use certificates from cert-manager and roots distributed by
trust-manager. Required `runner_security.mode: mtls` uses a `tls` block naming the Gateway's client
`cert_file`, `key_file` and `ca_file`. The Gateway identity
is `spiffe://agentplane/runner-gateway`; runner identities bind the Sandbox UID and its current
controller-assigned credential ID. Mutual TLS plus Sandbox, incarnation, Runner boot and Lease
epoch metadata fences every direct Runner RPC.

Local tests may explicitly select `runner_security: {mode: insecure}`, with the Runner's
`control.security` set to the same value. This disables transport encryption and peer authentication;
it preserves workload ownership validation, fencing and public Gateway caller authorization. The
mode has no default; insecure rejects a `tls` block and invalid mTLS never falls back. The Sandbox
Controller still provisions certificates, so cluster and k3s stacks retain real certificate controllers.

The Dispatcher role retains its PostgreSQL queue, command IDs, per-Sandbox delivery Leases
and database ownership generations. The connection role keeps separate connection Leases and
EndpointSlices; delivery on one replica routes to the replica holding the runner connection.
There is no separate Gateway database or Session registry and no History Service client.
Each Gateway reconciles one Runner observation per Sandbox on Sandbox and runner Pod events, reading
Sandboxes, Pods and KubeVirt objects from its informer cache; proxied calls and runner heartbeats read
the same cache, and new proxied calls are refused while it is unsynced or a watch is failing. After
losing a Runner observation, the owning Gateway retries after one second without waiting for an event.
Each attempt claims a fresh Lease epoch and validates the current workload and Runner boot before
serving calls. A replica refused by a foreign Lease holder retries its claim every heartbeat interval.
When an observer ends, the Gateway first removes its callable connection and closes the direct Runner
stream, then clears only its exact holder, Lease UID and epoch by a Kubernetes resource-version update.
This lets another replica claim without waiting 30 seconds after a graceful stop; an ungraceful death
still uses the conservative locally observed Lease duration. The connection epoch remains monotonic.
For a RAM-suspended Firecracker launcher, Pod readiness loss ends the observation and proxy calls
are refused. On resume, the same Pod and Runner boot ID reconnect under a higher Lease epoch;
the Gateway forwards each execution request only once and does not replay one whose outcome was
uncertain when the connection ended.
[Queue semantics](dispatcher/README.md) remain distinct from History's retained
conversation and from Runner admission.

## Source and tests

- `main.go`, `config.go`: process and listener wiring, and the settings (the required
  `AGENTPLANE_RUNNER_GATEWAY_CONFIG_FILE` path, with all non-secret settings in that file;
  [`settings.py`](settings.py) is the authoring schema). Only the Dispatcher database URL Secret,
  Pod IP from the Downward API and optional Pod name are runtime environment inputs. Generic
  `AGENTPLANE_RUNNER_GATEWAY_*` variables do not override file values. The independent `delegation`
  policy names an Actions actor and exact full RPC methods; it can include the
  `EnvironmentService` list/get/watch methods and, when separately configured, `ResumeEnvironment`, without
  delegating other Gateway methods. `operator_callers` is optional and defaults empty; it is
  restricted to direct, Pod-bound callers and gives only its configured operator broker the
  requested-stop override. No deployment caller or delegated-method activation is part of this
  backend slice. `environment_managers` grants direct Pod-bound accounts full Sandbox management;
  `environment_launchers` grants Action brokers create-only authority with required caller origin.
  `CreateEnvironment` names the Identity the Sandbox acts as and binds no PolicySets: a launch
  creates the Identity and grants it, including the default sets, before calling it.
- `gateway/`: Runner connections, authenticated proxy, Sandbox/Pod validation and connection ownership.
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
the direct mTLS connection on RBE.
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

The separate, default-empty `executor_callers` policy enables typed `Executor` RPCs for
trusted samplers. The Gateway checks destination and incarnation, then forwards process and file
operations to the Runner's authenticated `Executor` service using the same
[sandbox-owned contract](../env/runtime/execution/executor.proto). The Runner alone translates the
local Codex JSON-RPC protocol. A server-streaming `OpenSession` owns each adapter connection and
returns native-frame evidence; execution operations use unary RPCs with that session ID. Closing
the stream releases the adapter connection and detaches the native Codex session. Codex's
30-second resume grace period ends by expiring the session and terminating its remaining processes.
The Gateway adds no persistence or operation
receipts and never replays mutations. Open execution sessions participate in Runner activity and
quiesce.
The Dispatcher is absent from this tool path. See the
[central sampler contract](../central_sampler/SPEC.md).
