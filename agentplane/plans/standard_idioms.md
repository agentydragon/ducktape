# Standard idioms over hand-rolled mechanisms

A 2026-10-30 read-only audit of Agentplane against standard Kubernetes, gRPC, HTTP, PostgreSQL and
frontend idioms, after the custom watch-cycle freshness turned out to freeze the app's inventory
(`KUBERNETES_NATIVE_WATCH_HEALTH`). This plan holds each accepted item's problem, target and exit;
[the task DAG](task_dag.md#16-standard-idioms-over-hand-rolled-mechanisms) holds state and
dependencies. Items the audit judged justified are not listed: the central egress proxy, the
sampler's Responses stream parser, runner supervision, Electric sync, the local command outbox, CRD
validation, the Policy Controller and gRPC health.

Line references are to `devel` at the time of the audit; re-check before starting.

## HTTP

### `EGRESS_SIDECAR_STDLIB_HTTP`

The sidecar relay parses and rewrites HTTP/1 request heads byte by byte (`egress/sidecar/relay.go`
`readHead`, `isConnectRequest`, `rewriteRequestHead`, `responseStatus`, `writeBytes`) at the point
where it strips and injects `Proxy-Authorization` and the workload token, and splices the rest of a
plain-HTTP connection raw, relying on a forced `Connection: close` to stop a pipelined request from
bypassing the rewrite. Use `http.ReadRequest`/`http.ReadResponse` with `Header.Del`/`Set` and
`Request.Write`, or an `http.Server` with `httputil.ReverseProxy` and `Transport.Proxy` /
`ProxyConnectHeader` toward the central proxy. Exit: existing relay tests pass; tests for duplicate,
folded and pipelined headers at the token-injection point.

### `LLM_INGRESS_REVERSE_PROXY`

`llm_ingress/server.go` forwards by hand: path joining, an unbounded `io.ReadAll` of every request
body only to optionally log it, a manual copy/flush loop, hop-by-hop header stripping and
`Connection` token parsing. Use `httputil.ReverseProxy` (`Rewrite`/`SetURL`, `FlushInterval: -1`,
`ModifyResponse`, `ErrorHandler`), keeping the semaphore and dial timeout; log bodies by wrapping, with
a size bound. Exit: streaming and header tests pass; no unbounded buffering.

### `APP_PROXY_REVERSE_PROXY`

The app's upstream and Electric proxies (`app/go/proxy/upstream.go`, `electric.go`) copy headers,
flush and override `Cache-Control` by hand. Express the response-header allowlist in
`ModifyResponse` on `httputil.ReverseProxy`. Exit: tests prove no header beyond the allowlist leaks.

## gRPC

### `GRPC_AUTH_INTERCEPTORS`

Caller authorization is a `RequireAccount` call inside each handler in the Gateway proxy and executor
relay, egress admin, History API, Notification operator API and Dispatcher API, while the Action
Service, MCP edge and Runner control server use interceptors. A new RPC that forgets the call is
unauthenticated. Use one server interceptor per service with a per-`FullMethod` allowlist that fails
closed for unlisted methods (for example go-grpc-middleware v2 `auth` with `selector`). Exit: each
service rejects an unlisted method; per-method tests replace per-handler ones.

### `GRPC_NATIVE_LISTENER`

`grpc/facade/mount.go` serves gRPC through `grpc.Server.ServeHTTP`, which grpc-go marks experimental,
which `GracefulStop` cannot drain, and which forces the abort-write hook in `grpc/streaming`. Used by
the Action Service and egress. Serve gRPC on its own listener, as the Runner Gateway does. Exit: drain
on shutdown covered by a test; the abort-write hook removed if nothing else needs it.

### `RUNNERCLIENT_PER_RPC_CREDENTIALS`

`sandboxes/runnerclient/client.go` re-implements token-file reading and metadata attachment instead
of `grpc/bearer` or a `PerRPCCredentials`. Reuse `bearer.Dial` (or `PerRPCCredentials` with
`RequireTransportSecurity` false). The Python client's equivalent is justified and stays.

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

### `SANDBOX_LEASE_PREDICATE`

The Sandbox controller's Lease predicate drops renew-only updates (`controller.go`), so a lapsed
connection that resumes renewing is never enqueued, and `RequeueAfter: runnerLapsedPoll` (5 s) polls
for `ConnectionLost` and `ConnectionPending` instead. Enqueue when liveness crosses the boundary (old
Lease lapsed, new one live) and drop the polls; `ConnectionPending` already reacts to the boot-ID fact
change. Exit: tests show reconnect and lapse observed without a periodic requeue.

### `SANDBOX_CONTROLLER_SECRET_CACHE`

The controller watches Secrets and owns ConfigMaps and Services with no label selector in its cache
options, so it caches every Secret in the namespace under namespace-wide list/watch RBAC, while
reading Secrets and Certificates uncached. Restrict the cache with `cache.ByObject{Label: …}` (the
cert-manager `secretTemplate` already sets the label) or use `WatchesMetadata`, and narrow RBAC to
match. Exit: the README's "cache holds only labeled objects" is true; RBAC no longer lists all
Secrets.

### `CREDENTIAL_BINDING_LOCATION` (decision) and `CREDENTIAL_BINDING_STATUS`

The runner credential binding (`runner-credential`, `-kind`, `-accepted-id`, `-pod-uid`,
`-last-vmi-uid`) is written as Sandbox annotations with `Update` (`sandbox_controller/runner_credential.go`,
`vm.go`), so the app and Runner Gateway, which hold `patch` on Sandboxes, can overwrite an identity
binding the security model relies on. Alternatives:

- **`/status`** (recommended): the only subresource a CRD has besides `/scale`; RBAC on
  `sandboxes/status` already separates it from spec writers, and the controller already keeps
  `status.volumes` and `status.quiesceAttempt` there. A typed `status.runnerCredential` struct gives
  the specific name.
- **A separate controller-owned resource** (for example `SandboxRunnerCredential`, owned by the
  Sandbox): its own RBAC and name, at the cost of a new CRD and an extra read.
- A custom subresource name is not available to CRDs; it needs an aggregated API server.

Implementation: migrate with a read fallback to the annotations for one release, then drop them.

### `LEASE_OBSERVED_TIME`

The Gateway's per-Sandbox connection claim (`runner_gateway/gateway/records.go`) decides expiry by
comparing the other replica's `RenewTime` with the local clock; client-go `leaderelection` measures
expiry from a locally observed renewal instead, so writer clock skew cannot break it. Neither
client-go nor controller-runtime offers per-key leases, so keep a small helper with observed-time
semantics. The Dispatcher's `dispatcher-<uid>` Lease has the same flaw but retires with the
[Dispatcher and Gateway merge](dispatcher_gateway_merge.md).

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

### `HISTORY_FOLD_DIRTY_QUEUE`

The History projection (`history_service/projection/runner.go`) selects every retained Session,
including those of deleted Sandboxes, on every history commit notification or every 2 s, upserts each
summary and runs a lease transaction and checkpoint read per Session. Fold only Sessions whose
checkpoint is behind their last cursor (an indexed condition or a pending table maintained by
`Append`), claimed with `SKIP LOCKED`. Exit: work per wake is proportional to changed Sessions.

### `SAMPLER_EXPORTER_WORKQUEUE`

The sampler exporter (`x/central_sampler/exporter.go`) hand-rolls a keyed queue with in-flight set,
retry deadlines, earliest-retry timer, concurrency cap and round-robin cursor, retrying a failing
Session every 1 s flat and logging only the error's type. Use client-go `workqueue`
(`TypedRateLimitingQueue`, `AddAfter`, per-item exponential backoff) and log the error. Add a
`SKIP LOCKED` claim, or fix `SPEC.md`'s "across replicas", before scaling past one replica.

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

### `MCP_EDGE_TOKEN_CACHE`

`mcp_edge/core_client.go` mints a fresh 600 s ServiceAccount token with TokenRequest, and does live
ServiceAccount GETs, on every Action call and on every received stream frame, which also defeats the
Action Service's TokenReview cache keyed by token, while `callers.go` already runs an informer over
those ServiceAccounts. Cache minted tokens per account and ServiceAccount UID, refresh at about 80% of
lifetime (as the kubelet's token manager does), and read UID and labels from the informer.

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
