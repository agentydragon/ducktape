# Standard idioms over hand-rolled mechanisms

A 2026-10-30 read-only audit of Agentplane against standard Kubernetes, gRPC, HTTP, PostgreSQL and
frontend idioms, after the custom watch-cycle freshness turned out to freeze the app's inventory
(`KUBERNETES_NATIVE_WATCH_HEALTH`). This plan holds each accepted item's problem, target and exit;
[the task DAG](task_dag.md#16-standard-idioms-over-hand-rolled-mechanisms) holds state and
dependencies. Items the audit judged justified are not listed: the central egress proxy, the
sampler's Responses stream parser, runner supervision, Electric sync, the local command outbox, CRD
validation, the Policy Controller and gRPC health.

File references are to `devel` at the time of the audit; re-check before starting.

## HTTP

### `APP_PROXY_REVERSE_PROXY`

The app's upstream and Electric proxies (`app/go/proxy/upstream.go`, `electric.go`) copy headers,
flush and override `Cache-Control` by hand. Express the response-header allowlist in
`ModifyResponse` on `httputil.ReverseProxy`. Exit: tests prove no header beyond the allowlist leaks.

## gRPC

### `GRPC_NATIVE_LISTENER`

`grpc/facade/mount.go` serves gRPC through `grpc.Server.ServeHTTP`, which grpc-go marks experimental,
which `GracefulStop` cannot drain, and which forces the abort-write hook in `grpc/streaming`. Used by
the Action Service and egress. Serve gRPC on its own listener, as the Runner Gateway does. Exit: drain
on shutdown covered by a test; the abort-write hook removed if nothing else needs it.

### `RUNNER_MTLS_GO_SPIFFE`

`runner_transport/credentials.go` (about 470 lines) skips Go's verification and re-implements SPIFFE
peer verification, verifies twice per handshake to fill `VerifiedChains`, re-reads certificate, key
and CA bundle from disk on every handshake and retries loads to ride out rotation. Use
`github.com/spiffe/go-spiffe/v2` `tlsconfig.MTLSClientConfig`/`MTLSServerConfig` with
`tlsconfig.AuthorizeID` and `grpccredentials.PeerIDFromContext`, and a rotating file source (grpc-go
`certprovider/pemfile` or an `x509svid.Source` over the mounted files). Issuance by cert-manager and
trust-manager stays. Exit: stale credential and epoch rejection tests from the transport contract
pass unchanged.

## Kubernetes

### `CONDITION_REASONS_NOT_STATE`

Code branches on condition `reason` strings: the Sandbox controller reads its own `Running` reason
`Quiescing` (`sandbox_controller/behavior.go`), the Gateway derives a lifecycle phase from `Deleting`
and `Quiescing` (`runner_gateway/gateway/sandboxes.go`), and the Dispatcher wakes on `Quiescing` and
`Ready=Stopped` (`runner_gateway/dispatcher/sandbox_kubernetes.go`). Reasons are informational under
the API conventions. Expose the state as a typed status field (the CRD already has `operatingMode`,
`suspendedBy`, `quiesceAttempt`) or a dedicated condition type, move every reader to it, then stop
depending on reasons. Roll-safe: write the new field before any reader requires it. Exit: no code
compares a condition reason; renaming a reason changes only display.

## Work queues

Hand-rolled full-scan loops where a keyed, rate-limited work queue (client-go `workqueue`, or a
controller-runtime Reconciler) or a PostgreSQL dirty-row queue claimed with `SKIP LOCKED` is the
standard shape.

### `GATEWAY_OBSERVER_RECONCILER`

`RunRunnerObservers` (`runner_gateway/gateway/runner_observer.go`) lists all Sandboxes every 10 s and
on a coalesced wake, and does a live Pod GET per Sandbox; `validateRunnerDestination` re-reads
Sandbox, Pod and for VMs the VMI and VM on every runner heartbeat; every proxied Runner or Executor
RPC does a live Sandbox read and Pod GET (`gateway/sandboxes.go`), while a synced cache of the same
objects feeds only the catalog and wake handlers. Drive observers from a Reconciler keyed by Sandbox
(`Watches(Pod)` mapped to its owner) reading the cache; keep live reads only where fencing needs
them (the Lease resourceVersion compare-and-swap already fences). Exit: API reads per runner no longer
scale with the 10 s tick; fencing tests unchanged.

### `DISPATCHER_KEYED_DELIVERY`

Blocked on the [merge design](dispatcher_gateway_merge.md). The Dispatcher turns any Sandbox event into
one signal and on each one, and every 10 s, lists all Sandboxes, scans pending work and acquires a
Lease per busy Sandbox (`runner_gateway/dispatcher/delivery.go`), detecting deletions by diffing UIDs.
In the merged service, key delivery by Sandbox on a work queue fed by informer events.

## PostgreSQL

### `POSTGRES_LEASE_HELPER`

Four hand-rolled acquire/renew/expiry/fence/release implementations on `clock_timestamp()`: the History
`session_projection_lease`, the Notification inbox `claim`/`claim_until`, the GitHub refresh claim
columns, and the sampler's `owner`/`lease_until`/`generation`. Extract one `postgres/go` helper with
fencing tokens and move each user to it; evaluate River where the user is really a job queue. The
Dispatcher's ownership is excluded: it moves to the Gateway with the merge.

### `DISPATCHER_WATCH_VERSIONS`

Blocked on the [merge design](dispatcher_gateway_merge.md). `WatchDispatches` ships deleted rows as
base64 protobuf split into `pg_notify` parts (`dispatcher/terminal.go`), reassembled with a timeout
and a pending cap, failing every stream on any malformed part, with no replay, through its own
`pq.NewListener` rather than the shared `postgres/go/wakeups.Hub`; every stream re-lists its scope on
any change. Use a version column bumped from a sequence on every write, short-lived tombstones keyed
by command ID with no Sandbox address (which satisfies "no row addresses a deleted Sandbox UID"),
payload-free notifications through `wakeups.Hub`, and `WHERE version > last` resumption.

## Central sampler

### `SAMPLER_TYPED_STATE`

The turn loop returns bare strings (`"done"`, `"cell"`, `"steer"`, …) dispatched by string compares;
`FinishCommand` re-derives the proto outcome from them; process and cell statuses are strings compared
about 80 times with no DB CHECK; intent payloads are `map[string]any` probed by key, beside the typed
`PendingIntentKind` in the same package. Use named types with constants, typed intent payloads and
CHECK constraints.

### `SAMPLER_JOURNAL_DIET` (decision)

Operator to weigh in. Every sampling intent stores the full Responses request body
(`runtime.go`, `Store.Begin`) and nothing reads it beyond its kind, with no deletion, so storage grows
with turns × context. Every streamed provider event is journaled as a `BoundaryRecord` in its own
locking transaction, then again inside the intent's result and again in failure evidence. Options:
store kind plus a conversation reference and journal raw events once; batch per-event writes or keep
only item-done and terminal events; or keep everything and add retention.

## App

### `APP_LIVE_FRAME_BROADCAST`

Each open `/live/*` stream recomputes its whole snapshot on every Kubernetes event in the namespace,
rerunning a thread page query, a History `BatchGetSummaries` call and an archived count per tab even
when only a Pod changed (`app/go/streamapi.go`). The same Sandbox inventory also arrives on two or
three streams, and the thread page reads the top-bar status and the composer state from different ones.
Compute each frame once per change and broadcast its bytes (a hub or `singleflight`), re-query threads
only on thread changes, and give each entity one client-side source.

### `FRONTEND_SSE_CLIENT`

`live_stream.ts` (`EventSource`) and `watch_stream.ts` (fetch plus NDJSON) duplicate backoff, resume
and phase reporting, and because `EventSource` cannot see a 401, `live_stream.ts` probes `/models`
after each hard close to trigger login. Use one fetch-based SSE reader that sees status codes and goes
through `fetchWithLogin`.

## Runner

### `RUNNER_JSONRPC`

Two hand-written JSON-RPC 2.0 clients with different envelopes: the exec-server client
(`runner/execution/connection.go`, `protocol.go`) and the app-server adapter (`runner/codex/adapter.go`,
a `map[string]any` envelope beside generated params). Both must capture exact native frames for the
journal. Evaluate an existing Go library with a frame hook (`sourcegraph/jsonrpc2`,
`creachadair/jrpc2`, `golang.org/x/exp/jsonrpc2`); otherwise share one envelope and correlation helper.
