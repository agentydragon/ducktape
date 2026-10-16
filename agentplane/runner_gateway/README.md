# Runner Gateway

The Go Runner Gateway combines durable Session Dispatcher admission and delivery with the
runners' outbound [RunnerControl stream](../docs/runner_control.md) and the identity-fenced
Runner proxy. One gRPC listener on `:8080` serves Dispatcher, the Runner proxy and runner `Connect`,
with separate authentication and authorization for each service. The app and Notification service use
Python gRPC clients against those contracts. The Sandbox Controller owns `Sandbox.status`.

The Dispatcher role retains its PostgreSQL queue, command IDs, per-Sandbox delivery Leases
and database ownership generations. The connection role keeps separate connection Leases and
EndpointSlices; delivery on one replica routes to the replica holding the runner connection.
There is no separate Gateway database or Session registry and no History Service client.
[Queue semantics](../session_dispatcher/README.md) remain distinct from History's retained
conversation and from Runner admission.

## Source and tests

- `go/main.go`, `go/config.go`: process, listener wiring and deployment flags.
- `go/gateway/`: Runner connections, authenticated proxy, Sandbox/Pod validation and connection ownership.
- `go/dispatcher/`: durable admission, watch, delivery, guarded wake and scheduling ownership.
- `go/migrations/`: Goose migrations and the data-preserving Dispatcher Alembic handoff.
- `client.py`: the Python Runner proxy client; `testing/` provides narrow consumer peers and
  the real Go fixture for cross-language acceptance.

Build `//agentplane/runner_gateway/go:runner-gateway` or `//agentplane/runner_gateway:go_image`.
The migrator image is `//agentplane/runner_gateway/go/migrations:image`; serving never applies
migrations. Integration tests exercise real PostgreSQL and queue-to-runner tunnels on RBE.
The cross-language Notification acceptance runs a real Kubernetes API server and etcd through
`envtest`, installs the Agentplane CRDs and issues Pod-bound ServiceAccount tokens. Narrow
consumer tests continue using lightweight gRPC peers.

## Deployment gate

The environment root owns the stable Services, Dispatcher database/Secret and full-digest-named
Goose Job. Its dependent delivery Kustomization owns the pinned Go serving workload and starts
only after the current migration completes. Serving never migrates on startup. Image discovery
does not advance the reviewed serving/migration release pair automatically.

The activation configuration releases testing and keeps staging suspended. Merge it only after
the old writers are fenced. Use the [fenced rollout procedure](../docs/go_delivery_rollout.md) to stop
old writers, preserve queued work, activate testing and then staging, and verify reconnect and
replica recovery. Both environments retain `Recreate` through acceptance.
