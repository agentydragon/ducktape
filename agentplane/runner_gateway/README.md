# Runner Gateway

The Runner Gateway combines durable Session Dispatcher admission and delivery with a
Sandbox-fenced Runner proxy. The Gateway dials each Runner's native gRPC server on private port
`9090`. Its public `listen_address` serves Dispatcher, Runner proxy and optional
ExecutorGateway with the existing
caller authorization. The Sandbox Controller owns `Sandbox.status`.

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
After losing a Runner observation, the owning Gateway retries after one second without waiting for
the next Sandbox discovery scan. Each attempt claims a fresh Lease epoch and validates the current
workload and Runner boot before serving calls. A foreign Lease holder waits for discovery instead.
[Queue semantics](dispatcher/README.md) remain distinct from History's retained
conversation and from Runner admission.

## Source and tests

- `main.go`, `config.go`: process and listener wiring, and the settings (a YAML file named by
  `AGENTPLANE_RUNNER_GATEWAY_CONFIG_FILE` plus `AGENTPLANE_RUNNER_GATEWAY_*` environment variables;
  [`settings.py`](settings.py) is the authoring schema), including the optional `http_listen_address`
  for the Dispatcher's HTTP/JSON facade (none when unset).
- `gateway/`: Runner connections, authenticated proxy, Sandbox/Pod validation and connection ownership.
- `dispatcher/`: durable admission, watch, delivery, guarded wake and scheduling ownership.
- `migrations/`: Goose migrations for the Dispatcher database.
- `client.py`: the Python Runner proxy client; `testing/` provides narrow consumer peers and
  `gateway_process.py` runs the real binary for cross-language acceptance.

Listener settings accept ordinary TCP `host:port` addresses and `fd:N`, where `N`
is the descriptor number **in the child process** (at least 3). An inherited
socket must already be bound and listening on TCP. Both gRPC and the optional
HTTP facade use the same convention. Invalid descriptors fail startup without
falling back to another address.

Startup follows Go's `ListenAndServe`/`Serve` separation: `listenAndServe` obtains
and owns the listeners, while `serve` takes `net.Listener` values. A nil HTTP
listener disables the facade. FD parsing and conversion stay at the process
boundary; serving code never handles descriptor numbers. `net.FileListener`
duplicates the descriptor, so startup closes the inherited descriptor after
conversion and closes the resulting listener on exit, including setup failure.
The parent closes its own socket copy after spawning the child.

For Go parents, `exec.Cmd.ExtraFiles` assigns child descriptors `3+i`; Python's
`pass_fds` preserves descriptor numbers. The launcher supplies the corresponding
`fd:N` values. Client addresses come from the parent's held listener, without
startup files or log parsing.

Build `//agentplane/runner_gateway:runner-gateway` or `//agentplane/runner_gateway:image`.
The migration image is `//agentplane/runner_gateway/migrations:image`. Each new serving Pod
runs it as an init container before the Gateway starts. Goose's PostgreSQL session lock
serializes migrations when replicas start together. Restarting only the main container does
not rerun the init container; another Pod creation is required to apply migrations after a
database replacement. Integration tests exercise real PostgreSQL and queue-to-Runner delivery over
the direct mTLS connection on RBE.
The cross-language Notification acceptance runs a real Kubernetes API server and etcd through
`envtest`, installs the Agentplane CRDs and issues Pod-bound ServiceAccount tokens. Narrow
consumer tests continue using lightweight gRPC peers.

## Deployment

The environment root owns the stable Services, Dispatcher database and Secret. Its dependent
delivery Kustomization owns the serving workload and waits for the environment root to become
Ready. Each Gateway Pod runs the Goose migrator before its server starts. Flux image
automation advances the Gateway and migrator images independently, so migrations must remain
compatible with the version still serving during a rollout.

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
Omitting it disables central Session admission. Callers supply harness and model choices;
session specs contain no routing metadata. The Dispatcher sends central opens and commands
to this target; wrapped native Sessions continue through ProxyRunner. Central controls bypass
executor readiness/wake checks after open.
The existing Sandbox UID/incarnation binding is fixed; v1 requires the bound Sandbox object.

The separate, default-empty `executor_callers` policy enables typed `ExecutorGateway` RPCs for
trusted samplers. The Gateway checks destination and incarnation, then forwards process and file
operations to the Runner's authenticated `RunnerExec` service. The Runner alone translates the
local Codex JSON-RPC protocol. A server-streaming `OpenSession` owns each adapter connection and
returns native-frame evidence; execution operations use unary RPCs with that session ID. Closing
the stream does not terminate remote processes. The Gateway adds no persistence or operation
receipts and never replays mutations. Open execution sessions participate in Runner activity and
quiesce.
The Dispatcher is absent from this tool path. See the
[central sampler contract](../x/central_sampler/SPEC.md).
