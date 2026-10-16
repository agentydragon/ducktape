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
- `settings.py`: configuration contract for the still-pinned Python deployment, retained until cutover.

Build `//agentplane/runner_gateway/go:runner-gateway` or `//agentplane/runner_gateway:go_image`.
The migrator image is `//agentplane/runner_gateway/go/migrations:image`; serving never applies
migrations. Integration tests exercise real PostgreSQL and queue-to-runner tunnels on RBE.
The cross-language Notification acceptance runs a real Kubernetes API server and etcd through
`envtest`, installs the Agentplane CRDs and issues Pod-bound ServiceAccount tokens. Narrow
consumer tests continue using lightweight gRPC peers.

## Deployment gate

The live chart still renders the existing Python image pins. Their serving source and image
publishers are removed; the dedicated `agentplane-runner-gateway-go` release slot publishes Go
without advancing those pins. Before activation, stop old Python acceptance, delivery workers
and runner connections, verify all old Pods/workers are gone, then apply the digest-named Goose
Job under the existing root Flux owner. Keep CNPG, PVCs, Secrets, database and stable Services
under that owner. The new Go workload belongs to a dependent Kustomization and starts only after
migration completion. Verify row recovery, reconnect, two-replica routing and fencing before
restoring RollingUpdate. See the [delivery design](../docs/runner_delivery_and_harness_design.md).
