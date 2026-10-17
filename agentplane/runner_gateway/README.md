# Runner Gateway

The Runner Gateway combines durable Session Dispatcher admission and delivery with the
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

- `main.go`, `config.go`: process, listener wiring and deployment flags.
- `gateway/`: Runner connections, authenticated proxy, Sandbox/Pod validation and connection ownership.
- `dispatcher/`: durable admission, watch, delivery, guarded wake and scheduling ownership.
- `migrations/`: Goose migrations and the data-preserving Dispatcher Alembic handoff.
- `client.py`: the Python Runner proxy client; `testing/` provides narrow consumer peers and
  `gateway_process.py` runs the real binary for cross-language acceptance.

Build `//agentplane/runner_gateway:runner-gateway` or `//agentplane/runner_gateway:image`.
The migrator image is `//agentplane/runner_gateway/migrations:image`; serving never applies
migrations. Integration tests exercise real PostgreSQL and queue-to-runner tunnels on RBE.
The cross-language Notification acceptance runs a real Kubernetes API server and etcd through
`envtest`, installs the Agentplane CRDs and issues Pod-bound ServiceAccount tokens. Narrow
consumer tests continue using lightweight gRPC peers.

## Deployment

The environment root owns the stable Services, Dispatcher database/Secret and the Goose Job.
Its dependent delivery Kustomization owns the serving workload and admits it only from an
unsuspended root that is Ready, which includes the Job, and that health-checks it. Serving
never migrates on startup. Flux image automation advances the Gateway and migrator images
independently, so neither can assume the other already runs the matching release.

### Schema changes

Each step merges only after the previous one is running in both environments:

1. **Migration first.** Add the Goose migration alone, written so Gateway builds from before and
   after it both work against the old and the new schema. Flux picks up the migrator and
   force-recreates the root-owned Job; wait for it to complete and let it soak.
2. **Code that uses it afterwards.** Merge the Gateway change that depends on the new schema only
   once step 1 has soaked.
3. **Removals go the other way.** Stop the Gateway using a column or table first; the migration
   that drops it lands last.

The Dispatcher database and its historical migration fixtures stay. The automated proof of the
schema handoff is `//agentplane/runner_gateway/migrations:test_migrations`, including retained
queued work and idempotent application; it does not replace an environment's backup/restore check.
