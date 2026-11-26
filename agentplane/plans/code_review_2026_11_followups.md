# Code review follow-ups (2026-11-26)

Open findings from the 2026-11-26 read-only Agentplane review that were not dispatched. The operator will triage them later.
Each entry is the reviewer's text and has not been re-verified. Re-check against current code before acting.
Dispatched findings are tracked in their PRs and are not listed here. Delete an entry when it lands or is rejected,
and delete this file when it is empty.

Deferred by the operator: ACTIONS-6 / CROSS-8 (`reader.KubernetesIdentities` is test-only), which waits for the
consolidation of the PolicyService Identity test fakes.

## `env/`

### ENV-6 `Runtime.facts()` has an always-true branch, duplicated blocker appends, and two sources for "Admission"

- **Category:** economy / idiom
- **Severity:** low **Confidence:** high
- **Where:** `env/runtime/internal/coordinator/runtime.go:497-517`; the vocabulary is at `runtime.go:480-505` (literals `"Bootstrap"`, `"Admission"`, `"ExecutorRelay"`), `internal/coordinator/session_status.go:17` (`const autosuspendAdmission = "Admission"`) and `session/status.go` (`autosuspendActiveTurn`, `AutosuspendSetup`)
- **What:**
  - In `else if desired.Mode == lifecycle.Running`, the condition is always true because the `if` above already includes `desired.Mode != lifecycle.Running`.
  - The two `ExecutorRelay` appends are guarded separately but are equivalent to `activeExecution > 0 || executionRecovering`.
  - `"Admission"` is both a named constant and a literal in the same package, and it is derived from both the COSI count (`sessionFacts`) and `r.activeAdmissions`.
  - The autosuspend reasons form a vocabulary used across two packages, but they are scattered literals and constants (STYLE: a vocabulary used more than once gets one definition).
- **Fix:**
  - Collapse the idle logic to one `if ... || !ready { facts.IdleSince = nil }`.
  - Add a local `addBlocker(reason)` that dedupes, and merge the relay conditions.
  - Move all reasons into one typed const block (for example in `session/status.go`, exported) and use it from the coordinator.
  - Replace `sort.Slice` with `slices.SortFunc`.
  - Estimated -12 LOC.

### ENV-7 `BuildEnvironment` compiles a regexp for every (variable × pattern) pair on each process start

- **Category:** economy
- **Severity:** low **Confidence:** high
- **Where:** `env/runtime/execution/native/native.go:117-191` (`matches` closure calls `regexp.MustCompile` inside the loop; it runs over all of `os.Environ()` × 11 core names, then × 3 default excludes, × `Exclude`, × `IncludeOnly`, × 6 restricted names)
- **What:** Each `process/start` compiles several hundred identical regexps. The constant lists (`core`, the default excludes, `restricted`) are rebuilt on every call. Behaviour is right, but this is wasted work on the hot exec path.
- **Fix:** Add `compileGlobs([]string) func(string) bool`, which builds one alternation regexp. Precompile the three constant lists as package `var`s and compile the policy lists once per call. Separately, the `native.EnvPolicy{...}` field-by-field conversion is duplicated at `codexshim/shim.go:403` and `typedbackend/server.go:98`; give `native` a constructor or make the JSON/proto shapes share one struct. Estimated -10 LOC.

### ENV-8 "Close the channel and make a new one" change broadcast is open-coded 10 times in `env/` (15+ repo-wide)

- **Category:** dedup
- **Severity:** low **Confidence:** high
- **Where:**
  - `env/runtime/internal/coordinator/runtime.go:544-547`
  - `env/runtime/execution/native/native.go:462-467,909-912,965-971`
  - `env/runtime/session/manager.go:164-175,270-276`
  - `env/runtime/journal/journal.go:376-379`
  - `env/runtime/codexappserver/host.go:~185`
  - Outside `env/`: `sampler/internal/executor/bindings.go:113-133` (already a tiny type), `watchhealth/health.go`, `policy/reader/index.go`, `policy/identitycache/cache.go`, `egress/internal/policy/index.go`, `action_service/internal/admission/catalog/catalog.go`
- **What:** The same subscribe-before-read idiom is repeated everywhere, each with its own field pair and an optional lock.
- **Fix:** Add a zero-value-usable `agentplane/internal/changes.Signal` (or similar) with `C() <-chan struct{}` and `Notify()`. It needs no internal lock so it can sit under existing mutexes, and it lazily initialises like the sampler copy. Replace the copies. Estimated -40 LOC repo-wide; mechanical.

### ENV-9 Guest resource budget validation is written twice, rule for rule, in Python and Go

- **Category:** dedup
- **Severity:** low **Confidence:** medium
- **Where:** `env/config.py` (`GuestResources.internally_consistent`, about 45 lines) and `env/configuration/guest.go:25-79` (`guestResources.Validate`)
- **What:** The same ordered rules (OS reserve, memory high ≤ max, aggregate within service, executor/workload headroom subtraction, `PidsMax >= pidsHeadroom`, CPU quota ≤ vCPU×100000 and a multiple of 1000) live on both sides of the config file. One drifting from the other produces configs that render but fail at VM boot, or the reverse. I did not find a cross-language test that feeds both validators the same cases.
- **Fix:** Per STYLE multi-site agreement, either:
  - keep Go as the single validator and have the Python side rely only on schema bounds (`gt=0` and so on), or
  - add one shared table of valid and invalid guest-resource fixtures that a Python test and a Go test both consume.

  Estimated -40 LOC (first option) or +30 LOC of shared fixtures (second option).

### ENV-10 `typedproxy.poisoned` grows without bound for the life of the proxy

- **Category:** bug
- **Severity:** low **Confidence:** high
- **Where:** `env/runtime/execution/typedproxy/proxy.go:146-148` (insert); no delete anywhere in the file
- **What:** Each execution session whose disconnect cleanup fails adds an entry forever, even after the backend session has expired (`SessionRetentionSeconds`, default 1h) and the ID can never be resumed. The leak is small but unbounded on a long-lived Runtime with a flaky backend.
- **Fix:** Expire entries with the native retention window. `apimachinery/pkg/util/cache.Expiring` is the library STYLE names for this. Or drop the entry once a resume with that ID has been refused after retention. Estimated +5 LOC.

### ENV-11 `backend.Config` mixes a typed `Owner` with untyped string `Placement`/`Implementation` vocabularies

- **Category:** idiom
- **Severity:** low **Confidence:** high
- **Where:** `env/runtime/execution/backend/server.go:25-27` (`ImplementationNative`/`ImplementationCodex` untyped), `:81-84` (`PlacementDirect`/`PlacementSystemd` untyped), `:86-91` (typed `Owner`); compared at `:103-110`, `execution/service/service.go:184` and `internal/coordinator/runtime.go:286`
- **What:** Both are closed vocabularies dispatched on in several packages. Unlike `Owner`, they are plain `string` fields, so a typo compiles. `Config.Supervision` (`"pid1"`/`"systemd"`, `runtime.go:179-190,294`, `close.go:203`) has the same shape.
- **Fix:** `type Placement string`, `type Implementation string`, `type Supervision string` with consts, and typed fields. Estimated +6 LOC.

### ENV-12 Ingress compares the stream error to `io.EOF` with `==`

- **Category:** idiom
- **Severity:** low **Confidence:** high
- **Where:** `env/runtime/ingress/client.go:192`
- **What:** The error comes from a gRPC `ClientStream.Recv` relayed through a channel; this is not a direct `io.Reader.Read`. gRPC can wrap stream termination errors, and if it did, a clean server close would be reported as a raw EOF and retried instead of getting the "ingress ended before acknowledging the seal" diagnosis. The impact is cosmetic today: both paths retry.
- **Fix:** `errors.Is(message.err, io.EOF)`. 1 LOC.

## `sampler/`

### SAMPLER-4 Lease heartbeat gives up ownership after one transient `Renew` error

- **Category:** bug
- **Severity:** medium **Confidence:** medium
- **Where:** `agentplane/sampler/internal/residentruntime/session/driver.go:170-191,104-110`
- **What:** The heartbeat renews every `LeaseSeconds/3`. Any error from `Renew` (for example one `RPCTimeoutSeconds` DB timeout) returns at once, and `Drive` then calls `cancelExecution`. That cancels in-flight provider calls and tool effects and releases the Session as `ownership_lost`, even though the lease is still valid for about 2/3 of `LeaseSeconds`. Only `*LostOwnershipError` actually means ownership is gone.
- **Fix:** Return at once only on `*LostOwnershipError`. For other errors, log and retry on the next tick until `lease_until` (last successful renewal plus `LeaseSeconds`) is near. To confirm, check whether the immediate abort is a deliberate fencing choice; the code says nothing about it. About +8 LOC.

### SAMPLER-6 About 15 hand-rolled `rows.Next` loops with manual `Close` on every branch

- **Category:** economy
- **Severity:** medium **Confidence:** high
- **Where:**
  - `sessionstore/recovery.go:174-218,392-431,688-708`
  - `sessionstore/processes.go:293-310,497-515`
  - `sessionstore/commands.go:38-53,312-337,483-499`
  - `sessionstore/leases.go:27-38` (`scanIDs`), duplicated by `action_service/internal/store/executions.go:62` (`scanIDs` for UUIDs)
  - Further copies in `compact_journal.go`, `notification.go`, `host_status.go`, `live_event_block.go`, `internal/contentdb/store.go`
- **What:** Each loop closes rows by hand on every error branch, because the transaction issues more statements afterwards. This is error-prone, and `commands.go:38-53` reads one row through `QueryContext` and `rows.Next()` where `QueryRowContext` plus `sql.ErrNoRows` would do.
- **Fix:** Add `postgres.CollectRows[T](rows *sql.Rows, scan func(*sql.Rows) (T, error)) ([]T, error)` to `agentplane/postgres`. It defers `Close` and checks `Err`. Replace both `scanIDs` and the loops with it. The LIMIT-1 reads become `QueryRowContext`. About -120 LOC.

### SAMPLER-7 Settings duration getters are 13 copies of one pattern; the RPC timeout is converted inline 18 times

- **Category:** economy
- **Severity:** low **Confidence:** high
- **Where:**
  - Getters: `internal/settings/validation.go:302-421` (`SetupTimeout` through `ShutdownBudgetTimeout`); `settings.go:68-97`.
  - `time.Duration(…RPCTimeoutSeconds)*time.Second`: `session/driver.go` (×7), `session/setup.go` (×3), `cmd/main.go` (×3), `executor/executor.go` (×2), `provider_recovery.go`, `rpc/harness_host.go`, `rpc/service.go:45`.
  - `LeaseSeconds` conversions: `driver.go:86,171,180`.
- **What:** Each getter repeats the same five lines: start from the default, override if the pointer is set, convert seconds to a `Duration`.
- **Fix:** Add `func secondsOr(p *int, def int) time.Duration` and a generic `valueOr[T]`, which turns each getter into one line. Add `Settings.RPCTimeout()` and `Settings.Lease()` and use them at the call sites. About -70 LOC.

### SAMPLER-8 Driver repeats its "close live host" block five times; the "process exited" predicate is spelled out about nine times

- **Category:** dedup
- **Severity:** low **Confidence:** high
- **Where:**
  - Close-live block: `session/driver.go:305-313,330-336,370-379,495-500,609-614`.
  - `Status == Exited || Status == ExitedUnread`: `session/driver.go:362-368`, `session/interrupt.go:49-55`, `executor/processes.go:375,387,526`, `executor/executor.go:97,249,293`.
- **What:** Each close-live copy builds a 5 s context, closes `live`, nils it and sometimes resets `reconciled`. One copy (:495) skips the `reconciled` reset; this is harmless today only because `reconciled` is non-nil only when an executor exists. The exited check is open-coded in each place.
- **Fix:**
  - Add `func (r *Driver) dropLive(live **liveHost, reconciled *<-chan struct{}) error`.
  - Add `func (s ProcessStatus) Exited() bool` in `executionstate/status.go`, so `runningProcess` becomes `slices.ContainsFunc(state.Processes, func(p) bool { return !p.Status.Exited() })`.
  - About -35 LOC.

### SAMPLER-10 Executor duplicates the transport's retained-session lookup and the "detach cancelled ctx" pattern

- **Category:** dedup
- **Severity:** low **Confidence:** high
- **Where:**
  - `Executor.retainedExecutorSession`: `internal/executor/transport_adapter.go:39-50`, which duplicates `Transport.retainedSessionID` (`internal/executor/transport/session.go:575-587`) over `storeBindings.CurrentSession` (`executor/bindings.go:72`).
  - Rebuild-a-context-if-cancelled block: those two sites and `executor/executor.go:170-174`.
- **What:** Both lookups swallow the store error and return `""` without logging, which STYLE.md calls a silent fallback. The fallback context is built from `context.Background()`, so it drops the context's values (tracing).
- **Fix:**
  - Expose one helper from `transport`.
  - Replace the cancelled-ctx block with `context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)`.
  - Log the read error at `warning`.
  - About -15 LOC.

### SAMPLER-11 Manager fails pending requests in two identical loops

- **Category:** dedup
- **Severity:** low **Confidence:** high
- **Where:** `internal/codemode/manager.go` `failBinding` (pending loop) and `fail` (the `for _, item := range pending` loop); the active-session snapshots in `fail` and `Close`.
- **What:** Both loops do the same non-blocking send of `codeReply{Err}` to `response` and `initial`. The two "copy map values to a slice" loops can be `slices.Collect(maps.Values(m.active))`.
- **Fix:** Add `func (p *codePending) fail(err error)` and use `slices.Collect(maps.Values(...))`. About -18 LOC.

### SAMPLER-12 `applyInterrupt` evidence is an untyped map; redundant logic and a variadic optional parameter

- **Category:** idiom
- **Severity:** low **Confidence:** high
- **Where:** `session/interrupt.go:12-80`; similar untyped evidence in `sessionstore/recovery.go:172,201-218` (`abandonedProcesses []map[string]any`, then `evidence["process_id"].(string)`).
- **What:**
  - `result["status"]` is set twice.
  - `complete := cleanupErr == nil && stateErr == nil`: `stateErr` is already joined into `cleanupErr`.
  - `pending ...*PendingIntent` is a variadic used as an optional parameter.
  - In `recovery.go`, the process ID is read back out of the map with a type assertion.
- **Fix:** Use typed evidence structs that are marshalled at the store boundary, and an explicit `*PendingIntent` parameter where `nil` means "read it". About -10 LOC.

### SAMPLER-13 `sessionstore.New` accepts nil settings as "defaults"

- **Category:** idiom
- **Severity:** low **Confidence:** high
- **Where:** `internal/sessionstore/store.go:50-62`; production caller `cmd/verify_journal.go:32`; test callers listed by `rg 'sessionstore.New\('`
- **What:** A nil `settings` silently means default backoff, packing profile and write flags. This is the footgun default STYLE.md forbids, and a production CLI relies on it.
- **Fix:** Take `samplersettings.Settings` by value. Callers pass `samplersettings.Settings{}` explicitly. About ±0 LOC.

### SAMPLER-14 `Owner.Token` is a string that is parsed back to a UUID on every fence, renew and release

- **Category:** idiom
- **Severity:** low **Confidence:** high
- **Where:** `internal/sessionstore/store.go:139-145` (`Owner.lease`); `leases.go:118`; consumers in `residentruntime/runtime_replica.go:98-103`
- **What:** The lease holder is a `uuid.UUID`. It is stringified at `Claim` and re-parsed, with an error path, on every lease operation. STYLE.md asks identifiers to carry their type.
- **Fix:** Make `Owner.Token` a `uuid.UUID` and convert only for SQL `ANY($1::uuid[])` and logs. About -5 LOC.

### SAMPLER-15 Journal compaction retries a persistently failing Session about once a second, forever

- **Category:** bug
- **Severity:** low **Confidence:** medium
- **Where:** `internal/sessionstore/journal_compaction_worker.go:12-44`
- **What:** If `compactPendingJournal` keeps failing for one Session (for example on a row that fails to decode), the loop lists that Session again on each pass: a 50 ms pace, then a 1 s idle wait. The result is a WARN log about once a second with no backoff and no cap.
- **Fix:** Add per-Session exponential backoff. The workqueue rate limiter that `historyexport` already uses would do, or a small `map[id]time.Time` skip-until. About +10 LOC.

### SAMPLER-16 `FollowEvents` sends without a deadline, unlike `WatchSessions`

- **Category:** bug
- **Severity:** low **Confidence:** medium
- **Where:** `internal/rpc/admin_service.go:142-145` versus `internal/rpc/harness_host.go:75-77` (`streaming.SendWithin`)
- **What:** A connected admin client that stops reading blocks `stream.Send` on flow control indefinitely. That holds the handler goroutine and its wakeups subscription. Separately, `FollowEvents` wakes on the global `OutboxChannel`, so every follower re-queries on any Session's commit.
- **Fix:** Wrap the send in `streaming.SendWithin(ctx, s.Settings.RPCTimeout(), stream.Send, …)`, as `WatchSessions` does. +3 LOC.

## `action_service`, `mcp_edge`, `policy*`, `llm_ingress`

### ACTIONS-6 `reader.KubernetesIdentities` is test-only, and its semantics differ from production

- **Category:** economy (dead code) / idiom
- **Severity:** medium **Confidence:** high
- **Where:** `policy/reader/identities.go:20-37`. Production callers all pass `CachedIdentities` (`action_service/cmd/main.go:336`,
  `egress/cmd/main.go:212`, `environments/internal/serving/server.go:72`, `session_dispatcher/internal/app/server.go:98`,
  `notification_service/cmd/identities.go:25`). `KubernetesIdentities{}` appears only in tests:
  `policy/reader/fixture_test.go:202`, `egress/internal/informer/fixture_test.go:88`,
  `action_service/internal/admission/informer/fixture_test.go:80`, `action_service/internal/rpc/fixture_test.go:148`.
- **What:** This is the Kubernetes Identity informer path that `agentplane/AGENTS.md` § Identities says to retire.
  Its behaviour also differs. `Index.PutIdentity` (`policy/reader/index.go:198-203`) records an Identity that has a
  `deletionTimestamp`. The production `identitycache.put` (`policy/identitycache/cache.go:206-213`) drops deleting
  Identities. So these four test suites admit callers that production would refuse.
- **Fix:** Delete `KubernetesIdentities` and the informer branch. Point the four fixtures at `CachedIdentities` over
  the existing PolicyService fake. This overlaps the deferred "five copies of the PolicyService Identity view fake"
  item, so land it with that consolidation. About −20 LOC production.
- **Status:** deferred (see top).

### ACTIONS-7 grpcgate repeats the delegated-identity switch and the method table

- **Category:** economy
- **Severity:** low **Confidence:** high
- **Where:** `action_service/internal/admission/grpcgate/gate.go:190-218` (Recheck) and `:247-258` (authenticateAction):
  the same `switch profile { session → sessionIdentity; connection → connectionIdentity; default → ErrUnauthenticated }`.
  `:89-101`: 13 lines of `X: g.authenticateAction(X)`. `:409-412`: `authenticator` is a no-op wrapper around the map lookup.
- **Fix:** Add `func (g *Gate) delegatedIdentity(profile, verified) (auth.Identity, error)` and use it in both places.
  Build the Action entries with a loop over a `[]string` of method names, and the operator entries likewise.
  Inline `authenticator`. About −35 LOC.

### ACTIONS-8 MCP edge gate reimplements peer-identity extraction and carries Action Service strings

- **Category:** dedup / idiom
- **Severity:** low **Confidence:** high
- **Where:** `mcp_edge/internal/admission/gate.go:103-119` (`peer.FromContext` → `TLSInfo` → `PeerIdentityFromTLSState`; a
  copy of `peertls.PeerIdentity`, `net/peertls/credentials.go:376`, which grpcgate uses; the EKU is already enforced at
  handshake). `:29-30` and `:44-47` use `map[string]struct{}` and `map[string]bool` instead of `sets.Set`.
  `:24`, `:244`, `:247` use copied Action Service text: "Action Service is draining", "action request deadline
  expired", "the Action Service is temporarily unavailable". These reach MCP edge operators' clients and logs.
- **Fix:** Use `peertls.PeerIdentity` and `sets.New`, and rename the messages to the MCP edge / Connection API.
  About −20 LOC.

### ACTIONS-9 Revoke refuses Flux- and controller-owned bindings with retryable `ABORTED`

- **Category:** idiom
- **Severity:** low **Confidence:** high
- **Where:** `policy_controller/internal/rpc/sandbox_bindings.go:141-151` (`codes.Aborted` for "managed by Flux",
  "owned by the Sandbox controller", "belongs to another Sandbox incarnation", "belongs to another Identity");
  compare `identities.go:76` (`DeleteIdentity` returns `FailedPrecondition` for the same Flux case).
- **What:** `ABORTED` means "concurrency conflict, retry". These refusals are permanent, so clients that retry
  `ABORTED` will loop. Only the UID/resourceVersion mismatch is really `ABORTED`.
- **Fix:** Return `FailedPrecondition` for ownership refusals and keep `Aborted` for the UID/RV conflicts. About 0 LOC.

### ACTIONS-11 PolicyService checks the reader allowlist twice and stores it twice

- **Category:** economy
- **Severity:** low **Confidence:** high
- **Where:** `policy_controller/internal/rpc/service.go:33-35,82-116`
- **What:** `Methods()` already binds every reader method to `auth.Services(readerServiceIDs)`. That rule admits only
  a `ServiceAdmission` from a listed SPIFFE ID (`identity/auth/service.go:84,156-159`). `requireReader` then repeats
  both checks against `readerServices`, the same list stored a second time as a set. It cannot fail after admission.
- **Fix:** Drop `readerServices` and `requireReader` (6 call sites). If defence in depth is wanted, keep one comment at
  `Methods()`. About −25 LOC.

### ACTIONS-12 llm_ingress `central_sampler_identity_callers` is a redundant derived flag

- **Category:** economy / idiom
- **Severity:** low **Confidence:** high
- **Where:** `llm_ingress/internal/server/config.go:73-75,122-124,150-152`; `server.go:60-70`; `runtime.go:66-69`;
  `llm_ingress/settings.py` and the cdk8s manifest that sets it.
- **What:** Validation forces `flag == (len(central_sampler_services) > 0)` in both directions, so the bool carries
  no information (STYLE: no redundant derived fields). The nested `if` in `newIngressHandler` and the nil-able
  `identityUIDs` exist only because of it.
- **Fix:** Delete the field. Delegation is enabled iff `central_sampler_services` is non-empty. Remove it from
  manifests first, then the field (AGENTS § Settings). About −15 LOC.

### ACTIONS-13 Four copies of "build the Executor of each group of binding kind X" in Action Service main

- **Category:** dedup
- **Severity:** low **Confidence:** high
- **Where:** `action_service/cmd/main.go:166-208` (sandbox), `:229-258` (orchestration), `:262-283` (session delegation),
  `:293-307` (MCP)
- **What:** Each copy iterates `groups.Groups()`, type-asserts the binding, constructs the executor, calls `offer`,
  and stores it in `byGroup`.
- **Fix:** Add a generic `executorsOf[B catalog.Binding, E worker.Executor](groups, build func(key string, b B) (E, error), offers bool)`.
  Sandbox keeps its lazy client setup inside the closure. About −30 LOC.

### ACTIONS-16 DeleteIdentity is blocked by a predecessor's grants that are waiting for GC

- **Category:** bug (minor)
- **Severity:** low **Confidence:** high
- **Where:** `policy_controller/internal/rpc/identities.go:80-89`
- **What:** A binding owned by a _previous_ incarnation of the same name still names the Identity and is not
  `ownedBy` the current UID, so it counts as blocking. The policy reader already ignores it (`policy/reader/index.go:308-312`
  pins Identity-owned bindings to their owner UID), and GC is about to remove it. Delete after recreate therefore
  fails transiently with a confusing `FAILED_PRECONDITION`.
- **Fix:** Skip bindings that carry an Identity owner reference with a different UID, mirroring `reader.names`.
  About +3 LOC.

## `session_dispatcher`, `history_service`, `notification_service`

### SESSIONS-7 `ReadDispatchChanges` reads an unbounded batch into memory

- **Category:** bug (resource)
- **Severity:** medium **Confidence:** medium
- **Where:** `agentplane/session_dispatcher/internal/store/watch.go:81-133`
- **What:** The first read (`after=0`) or a long-gap resume selects every live dispatch row and every retained tombstone (24 h) in scope, with no `LIMIT`. The whole set is materialised before the first send. A Sandbox-scoped watch is not bounded by the per-Session `QueueCap`, which bounds only queued rows. To confirm: check whether terminal dispatch rows are retained in `dispatch` until the Sandbox ends. They appear to be, because only `EndSandbox` deletes them.
- **Fix:** Page by `ORDER BY version LIMIT n`. Set `Through` to the last version returned when the page is full, and loop without waiting. `WatchSessions` already does this (`SessionsAfter … LIMIT 100`). About +8 LOC.

### SESSIONS-8 Dispatcher `serve` returns without joining its background goroutines

- **Category:** bug (shutdown ordering)
- **Severity:** low **Confidence:** high
- **Where:** `agentplane/session_dispatcher/internal/app/server.go:162-168, 193, 202, 281-286`
- **What:** The tombstone pruner, inventory cache, activity observer and delivery worker are bare `go` calls. After `cancel()`, `serve` returns, and the deferred `hub.Close()`, `identities.stop()` and `db.Close()` run while those goroutines may still be finishing a delivery or lease release. Each one also uses `context.WithoutCancel` for releases and expects the DB and Hub to stay up.
- **Fix:** Run them in a `sync.WaitGroup` (`wg.Go`) or `errgroup` and wait before the deferred closes. About +6 LOC.

### SESSIONS-9 Notification watches re-check Session ownership with a Dispatcher RPC on every loop, including the first

- **Category:** economy
- **Severity:** low **Confidence:** high
- **Where:** `agentplane/notification_service/internal/rpc/session_watch.go:165-172` (`followSession`), after `sessionOwner` already called `requireSessionIdentity` (`:41`)
- **What:** Each loop iteration calls `Dispatcher.GetSession`: every heartbeat tick (15 s), every keyed wakeup and every due timer. The first iteration repeats the check `sessionOwner` just made. With three watch kinds per Session, that is roughly 12+ GetSession RPCs per minute per idle Identity, and a brief Dispatcher outage ends all Identity watches with Unavailable.
- **Fix:** Skip the check on the first iteration. Revalidate on the heartbeat tick, alongside `auth.RevalidateStream`, rather than on every read. About +3/-3 LOC.

### SESSIONS-10 Redundant per-request validation of Dispatcher watch wiring

- **Category:** economy / idiom
- **Severity:** low **Confidence:** high
- **Where:** `agentplane/session_dispatcher/dispatcher/watch.go:32, 98`, `dispatch_workset.go:32`, `session_watch.go:52, 65`
- **What:** `s.Hub == nil` and `s.WatchLease <= 0` are deployment invariants, but they are checked inside every watch RPC. Some checks run after subscribing, and they return `Internal`/`Unavailable` to callers for what is a configuration error.
- **Fix:** Validate them once when the `Service` is built (constructor or `serve`) and drop the five checks. About -12 LOC.

### SESSIONS-11 `Release` / `ReleaseWithRetry` branch is redundant

- **Category:** economy
- **Severity:** low **Confidence:** high
- **Where:** `agentplane/notification_service/internal/worker/worker.go:160-165`, `agentplane/notification_service/internal/inbox/claims.go:131-139`
- **What:** `ReleaseWithRetry` already clamps `minimumRetry` up to `releaseBackoff`, so `Release(...)` equals `ReleaseWithRetry(..., 0)`. The worker branches on `retryAfter > 0` only to pick between them.
- **Fix:** Call `ReleaseWithRetry` unconditionally. Delete `Release` and update its test callers. About -8 LOC.

### SESSIONS-12 Session process is still named "Runner Gateway" in logs and traces

- **Category:** idiom
- **Severity:** low **Confidence:** high
- **Where:** `agentplane/session_dispatcher/internal/app/server.go:287-305` (`observability.Init(ctx, "runner-gateway")`, `"Runner Gateway …"` log messages), `:105, :159` (`"Gateway RPC listener TLS"`, `"configure Gateway SVID authentication"`)
- **What:** The service is `sessions` (image `agentplane-sessions`), but its OTEL service name is `runner-gateway`. Traces for the Dispatcher are filed under a retired component name, and on-call has to know the alias.
- **Fix:** Rename the tracing service name and log strings to `sessions` / "Session Dispatcher". About 6 LOC changed.

### SESSIONS-13 Settlement row loop closes `rows` by hand on four paths

- **Category:** economy
- **Severity:** low **Confidence:** high
- **Where:** `agentplane/history_service/internal/storage/follow.go:142-183`
- **What:** `settlements.Close()` is called manually on every error path and once more on success. Every other scan loop in the store uses `defer rows.Close()`.
- **Fix:** Move the settlement read into a helper with `defer rows.Close()` that returns `(settlements, overflowCursor, err)`. About -6 LOC.

### SESSIONS-14 Notice delivery silently stalls when the boundary entry read returns nothing

- **Category:** bug
- **Severity:** low **Confidence:** medium
- **Where:** `agentplane/notification_service/internal/worker/deliver.go:83-86, 91-94`
- **What:** `if err != nil || len(boundary) == 0 { return err }` returns nil when History has no entry after `copied-1` (same for `tail`). The claim is released with no delivery error, so the inbox is retried every backoff forever and the owner sees nothing wrong. To confirm: whether History compaction can leave `last_cursor ≥ copied` with no retained entry at or after `copied`.
- **Fix:** Return `&inboxstore.InvalidHistoryError{Reason: "runner history boundary missing"}` when the slice is empty. About +4 LOC.

## `environments`, `egress`, `firecracker`, `identity`, `net`, `cli`, `sandboxes`

### INFRA-5 Lifecycle authorization does redundant uncached Sandbox GETs per resume attempt

- **Category:** economy
- **Severity:** low **Confidence:** medium
- **Where:** `environments/lifecycle/policy.go:255` (`identityWakeAssessment`), `:320` (`identityEligibility`), `:479` (`identityCanReadTarget`), each calling `sandboxes.ExecutionIdentity`. That re-GETs the Sandbox (`sandboxes/sandboxreader/reader.go` `ExecutionIdentity`) right after `ReadTarget` (`:489`) has read it uncached.
- **What:** In each `applyResume` loop iteration (`:135`), up to 5 attempts, an Identity caller costs two uncached API-server GETs of the same Sandbox. `identityEligibility` then compares the second read's identity name with the first read's `view.Spec.Identity`, which only guards against a change between two reads microseconds apart. The optimistic-lock Patch already refuses a changed object.
- **Fix:** Add `sandboxreader.Reader.ExecutionIdentityOf(ctx, *sandboxapi.Sandbox)`, which takes the already-read object and does only the Identity lookup. Use it in these three places. About -10 LOC, and half the API reads.

### INFRA-6 `applyResume` hand-rolls a conflict-retry loop; the sibling package uses `retry.RetryOnConflict`

- **Category:** library
- **Severity:** low **Confidence:** high
- **Where:** `environments/lifecycle/policy.go:30` (`maxPatchAttempts`), `:135-195` (`for attempt … if apierrors.IsConflict(err) { continue }`) vs `environments/internal/service/management/access.go:123,148,…` (`retry.RetryOnConflict(retry.DefaultRetry, …)`).
- **What:** The loop retries conflicts back to back with no backoff. client-go's helper adds jittered backoff and is already the convention in the same service.
- **Fix:** Wrap the read-evaluate-patch body in `retry.RetryOnConflict`, carrying the assessment out through a closure variable. Map exhausted retries to the existing `WAKE_REASON_TARGET_CHANGED` failure. About -5 LOC.

### INFRA-8 Decision-history writer runs a retention DELETE after every batch, even after a failed write

- **Category:** economy
- **Severity:** low **Confidence:** high
- **Where:** `egress/internal/history/log.go:202-209` (`run` calls `l.cleanup(ctx)` every loop) and `egress/internal/history/store.go:294-303`.
- **What:** At low request rates each batch holds one record, so every egress decision costs an INSERT plus a DELETE…LIMIT scan. When the database is down, each batch's three failed `Append` attempts are followed by a cleanup that also fails, counted in `CleanupFailures`, which doubles the noise. Retention is coarse (hours), so per-batch cleanup buys nothing.
- **Fix:** Rate-limit cleanup to the `idleCleanup` cadence: keep a `lastCleanup` timestamp and run it only when a minute has passed. Skip it when the preceding write failed. About +5 LOC.

### INFRA-9 vsock `dialListener` hand-rolls a backoff that never resets

- **Category:** library
- **Severity:** low **Confidence:** high
- **Where:** `firecracker/vsock/reverse.go:39-102` (`delay` in `Accept`), `:283-291` (`retryDelay`).
- **What:** `delay` lives across loop iterations and is never reset after a successful dial. After one outage reaches the 1s cap, every later single failure, for example the first redial after a stream closes, waits the full second. The doubling and cap repeat what `sethvargo/go-retry` (STYLE.md § Go "Retry") provides.
- **Fix:** Use `retry.WithCappedDuration(time.Second, retry.NewExponential(10*time.Millisecond))`, creating a fresh backoff after each successful dial. Delete `retryDelay`/`maxRetryDelay`. About -10 LOC.

### INFRA-12 vsock `DialBroker` discards a fresh stream when it dequeues a cancelled request

- **Category:** bug
- **Severity:** low **Confidence:** medium
- **Where:** `firecracker/vsock/reverse.go:216-224` (`dispatch`: `if r.ctx.Err() != nil { _ = conn.Close(); return true }`)
- **What:** The request channel has a buffer of 1. A `DialContext` whose context expired while queued makes `dispatch` close the stream it just accepted, instead of looping to the next waiting request. The guest then has to redial, which costs a round trip through `dialListener`'s backoff (INFRA-9) during gRPC reconnects. Confirm by checking whether the guest side treats an immediately closed stream as a failure that triggers backoff.
- **Fix:** On a cancelled request, `continue` the `for` loop so the same `conn` goes to the next request. Close it only on `b.done`. 1 LOC.

### INFRA-13 Small stdlib/apimachinery replacements

- **Category:** economy / library
- **Severity:** low **Confidence:** high
- **Where:**
  - `sandboxes/sandboxreader/reader.go:158-165`: an owner-ref loop that `metav1.GetControllerOfNoCopy(&account)` replaces, followed by a UID/APIVersion/Kind compare. The resolver already uses `GetControllerOfNoCopy`.
  - `net/peertls/credentials.go:602-609`: `requireEKU` loop → `slices.Contains(certificate.ExtKeyUsage, usage)`, as `identity/credentials/resolver/resolver.go:96` already does.
  - `egress/internal/policy/index.go:97-106`: `Credentials()` → `slices.SortedFunc(maps.Values(i.credentials), …)`.
- **Fix:** Mechanical replacements. About -15 LOC in total.

## `app/`

### APP-6 Browser-session store repeats its SELECT, transaction and deadline boilerplate

- **Category:** dedup
- **Severity:** low **Confidence:** high
- **Where:** `agentplane/app/internal/auth/session/store.go:81-85, 266-270, 420-424` (the same 10-column SELECT 3×); BeginTx/defer Rollback/Commit in `Read`, `CompleteLogin`, `SavePending`, `Terminate` and `RefreshLogin` (5×) vs `withTx` at `agentplane/app/internal/threads/store.go:186-196`; idle-vs-absolute clamp at `store.go:119-122, 185-188, 226-229`; nullable token args at `store.go:230-237` and `450-456`; `step` clamp at `store.go:123-126`
- **What:** The copies cannot drift without a test catching it, but they make up about a third of the file. The deadline clamps and `auth.go:349-353`/`381-384` are hand-written `min`/`max`.
- **Fix:** Add `const recordColumns`, a `withTx` helper (lift `threads.withTx` into `agentplane/postgres` and reuse it from both stores), `deadline := min(now.Add(s.idle), absolute)`, and `tokenArgs(*Tokens) (any, any, any)`. About -50 LOC.

### APP-7 Console relay spawns a goroutine and channel per read, which `ConsoleConnector` already makes unnecessary

- **Category:** economy
- **Severity:** low **Confidence:** high
- **Where:** `agentplane/app/internal/httpapi/streams/routes.go:342-377`; `agentplane/app/internal/sandboxes/console.go:91-97` (already closes the stream on `ctx.Done()`)
- **What:** `relayConsole` allocates a channel and goroutine for every 32 KiB chunk so it can `select` on `ctx.Done()`. `ConsoleConnector.Open` already closes `output` and the KubeVirt conn when `ctx` ends, so a plain `Read` returns promptly.
- **Fix:** Use a plain `for { n, err := console.Read(buf); … }` loop. If a `ConsoleSource` must be assumed not to honor ctx, use `context.AfterFunc(ctx, func(){ console.Close() })`. About -15 LOC.

### APP-8 One-shot and polling fetch effects hand-roll what the configured react-query client provides

- **Category:** library
- **Severity:** low **Confidence:** high
- **Where:** `agentplane/app/frontend/system/system.tsx:19-37` (poll every 15 s), `agentplane/app/frontend/sandboxes/identity.tsx:63-80`, `agentplane/app/frontend/connections/consent.tsx:52-67`, `agentplane/app/frontend/settings/dialog.tsx:34-46` (a revision counter as manual refetch), `agentplane/app/frontend/sessions/new_thread_form.tsx:65-95`, `agentplane/app/frontend/identities/delete.tsx:72-83`, `agentplane/app/frontend/connections/management.tsx:95-111`; `@tanstack/react-query` is already wired (`transport/query.ts`, used by `actions/history.tsx`, `actions/detail.tsx`, `sessions/thread_evidence.tsx`)
- **What:** Each copy re-implements `let active = true`, loading, error and refresh state. `new_thread_form.tsx` calls `listSandboxPresets()` again whenever the preset changes.
- **Fix:** Use `useQuery({ queryKey, queryFn, refetchInterval })` and `refetch()`. Error text comes from `query.error` via `displayableError`. About -80 LOC.

### APP-9 `@mantine/hooks` is a dependency but has zero imports; its hooks are hand-rolled

- **Category:** library
- **Severity:** low **Confidence:** high
- **Where:** `agentplane/app/frontend/shell/sidebar.tsx:19-28` (`usePhoneWidth`), `agentplane/app/frontend/shell/app.tsx:133-139` (matchMedia listener), `agentplane/app/frontend/shell/sidebar.tsx:45-55` (Escape keydown), `agentplane/app/frontend/sessions/command_progress.tsx:223` (keydown); dependency at `agentplane/app/frontend/BUILD.bazel:67`
- **What:** These are manual `matchMedia`/`addEventListener` effects. Mantine is the repo's default UI library.
- **Fix:** Use `useMediaQuery(PHONE_QUERY)`. For `app.tsx`, use `useMediaQuery` plus `useDidUpdate` to reset on crossing. Use `useHotkeys([["Escape", onClose]])` or `useWindowEvent`. About -25 LOC.

### APP-10 Federation and OIDC settings are validated and defaulted twice, with divergent rules

- **Category:** dedup
- **Severity:** low **Confidence:** high
- **Where:** `agentplane/app/internal/settings/config.go:410-429` and `476-489` (mode, profiles, token endpoint, `validateHTTPSOrLoopbackURL`) vs `agentplane/app/internal/federation/federation.go:390-465` (`validateConfig`, `validateProvider`, `loopback`); OIDC defaults at `agentplane/app/internal/auth/config.go:98-101` and again in `withDefaults` at `config.go:176-187`; validation in `LoadOIDC` at `config.go:103-114` and again in `validate` at `config.go:158-174` (called from `auth.New`, `auth.go:40-43`)
- **What:** The two loopback checks disagree. Settings accepts `127.*` and `::1` by string prefix. Federation uses `net.ParseIP(...).IsLoopback()` and also accepts `*.localhost` regardless of case. Federation also forbids a query on `token_endpoint`, while the settings check allows any path. Today the stricter check runs second, so behavior is the union of both. A future edit to one copy will silently diverge.
- **Fix:** Keep one owner. Validate federation in `federation.validateConfig`, called from `settings.Load` or at construction, and delete `config.go:410-429`. Make `LoadOIDC` the only place that sets defaults and checks positivity, and drop `withDefaults` plus the re-validation in `auth.New`. About -50 LOC.

### APP-11 `operator_federation_not_configured` is sent as JSON inside a string, on an unreachable branch

- **Category:** bug
- **Severity:** low **Confidence:** high
- **Where:** `agentplane/app/internal/httpapi/streams/routes.go:261-263`; contract at `agentplane/app/README.md:557` (`503 detail.code=operator_federation_not_configured`)
- **What:** `WriteDetailError` wraps the string, so the body is `{"detail":"{\"code\":…}"}`, not `detail.code`. The branch is also unreachable. `Federator` is nil only when `config.OIDC` is nil (`runtime.go:123`). Without OIDC, `auth.store` is nil, no request ever has `session.Login`, and the `operatorOnly` wrapper (`server.go:72-83`) returns 403 first.
- **Fix:** Delete the branch and make `Federator` required, or emit it via `writeFederationError(w, federationError(...))`. Update the README sentence to match. About -4 LOC.

### APP-12 Operator checks run three times per operator-facade request

- **Category:** economy
- **Severity:** low **Confidence:** high
- **Where:** `agentplane/app/internal/server/server.go:72-83` (`operatorOnly` wrapper), `agentplane/app/internal/httpapi/streams/routes.go:117-123` (`operatorLifetime`), `routes.go:255-260` (`operatorFacade` re-reads Caller/Session); `agentplane/app/internal/httpapi/operator/routes.go:80-91` (`nilDependency` uses reflection to probe a dependency that is always set in production)
- **What:** Every route that reaches these checks is registered with `operatorOnly=true`. The repeated checks, and the reflection probe, have no live failure path.
- **Fix:** Have `operatorLifetime` take the already-guaranteed `session.Row` and drop the re-checks in `operatorFacade`. Delete `nilDependency`. About -25 LOC.

### APP-13 Connections page loads its list and opens its watch at the same time; the list can overwrite newer rows

- **Category:** bug
- **Severity:** low **Confidence:** medium
- **Where:** `agentplane/app/frontend/connections/management.tsx:50-90` (watch `setRows`) and `92-111` (`load` → `listConnections` → `setRows` on mount); also `reloadAfterConflict` at `management.tsx:112-121`
- **What:** On mount, both start. If a watch upsert lands while `listConnections` is in flight, the list response written afterwards replaces the newer row with the pre-upsert copy. It stays stale until the next upsert. The watch's snapshot already gives the full list, so the list fetch is redundant for rows. It is only needed for `connectionIdentities()`.
- **Fix:** Load only identities in `load`. On an ABORTED conflict, rely on the watch, which delivers the competing write, instead of re-listing. Confirm by checking that `WatchConnections` always replays a full snapshot on connect, which `readConnectionWatchFrame` suggests. About -10 LOC.

### APP-15 JWKS and OIDC discovery block every caller under a mutex; JWKS refresh is hand-rolled beside go-oidc

- **Category:** library
- **Severity:** low **Confidence:** medium
- **Where:** `agentplane/app/internal/auth/auth.go:65-95` (`openIDClient` holds `oidcMu` across up to 30 s of discovery, using the first caller's ctx; failures are not cached, so every login or bearer request retries discovery while the IdP is down); `agentplane/app/internal/federation/verify.go:91-167` (`signingKey` holds `mu` across the JWKS fetch, and an unknown `kid` forces an un-rate-limited refetch on every call)
- **What:** While the IdP is slow, all concurrent `/auth/login` calls, bearer verification and Action federation calls are serialized behind one network call. go-oidc, already a dependency, provides `oidc.NewRemoteKeySet` with kid-miss refresh and in-flight dedup. The custom claim checks (exact `aud`, `azp` profile) can stay on top of `RemoteKeySet.VerifySignature`.
- **Fix:** Use `singleflight` (already in the Go table) or `sync.OnceValues` with retry-on-error for discovery. Replace the `tokenVerifier` key cache with `oidc.NewRemoteKeySet(ctx, jwksURI)` and keep `verify`'s claim checks. About -70 LOC. Confirm that `go-jose` v4's `ParseSigned` already rejects `crit` headers; if so, delete `hasCriticalHeader`.

### APP-16 Settings loader has no-op defaults and hand-rolled `slices.Contains`

- **Category:** economy
- **Severity:** low **Confidence:** high
- **Where:** `agentplane/app/internal/settings/config.go:301-306` (sets `Instructions`/`SetupScript` to `""` when absent, which is already their decoded value), `config.go:528-544` (`found = found || …` loops), `config.go:497`/`565-571` (`uniqueStrings` only to compare lengths), `config.go:545`/`553` and `agentplane/app/internal/sessionclient/session_client.go:112` (the 65 536 limit written three times), `agentplane/app/internal/httpapi/sessions/handler.go:119` vs `session_client.go:48` (the same setup-script length check twice on one path)
- **What:** These lines are dead or longer than they need to be. The duplicated limit can drift.
- **Fix:** Delete the no-op assignments. Use `slices.Contains`, and `len(slices.Compact(slices.Sorted(...)))` or a set for the duplicate check. Use `sessionclient.MaxSetupScriptCharacters` everywhere and check it once. About -20 LOC.

## Python: `native`, `system_tests`, `x`

### PY-4 `port_forward._wait_for_task` duplicates `cleanup.finish_cleanup`

- **Category:** dedup
- **Severity:** low **Confidence:** high
- **Where:** `agentplane/system_tests/driver/port_forward.py:166-178,184-204`; `agentplane/system_tests/driver/cleanup.py:46-77`
- **What:** Both shield a task until it finishes despite caller cancellation, then re-raise the deferred cancellation. `finish_cleanup` is the tested version: it tracks `cancelling()` counts and tells a cancelled child apart from a cancelled caller.
- **Fix:** have `async_port_forward` call `finish_cleanup(asyncio.to_thread(...))` and use the PY-3 helper to raise. About -15 LOC.

### PY-6 History event pagers in `central_sampler.py` bypass `SessionEventPages` and stop early or truncate

- **Category:** bug + library
- **Severity:** medium **Confidence:** medium
- **Where:** `agentplane/system_tests/scenarios/central_sampler.py:451,510,778-795` (hand-rolled cursor loops), `:81,128,1596,1895,1932,1980,2210` (one read with `limit=1000`, treated as the whole Session); `x/native_contracts/test_recovery.py:99,180,215,243,278,317,333`, `test_admission_reply_loss.py:175`, `test_session_driver.py:32` (same single-page pattern); shared helper at `agentplane/history_service/testing/session_events.py:53` (`SessionEventPages`, already used by `driver/session.py:162`)
- **What:**
  - `_assert_single_command_settlement` (`:778-795`) breaks on `if not page.entries`. A page that carries only an unstored range before later events ends the scan and undercounts commands.
  - The other loops advance `cursor = entry.cursor` and ignore unstored ranges and corrections.
  - The single-page reads silently drop anything after 1000 events. Diagnostics and assertions then see a prefix without saying so.
- **Fix:** add a `read_all(history, session_id, after_cursor=0)` to `history_service/testing/session_events.py` that loops `SessionEventPages.apply_page` until `not more_available`, and use it everywhere. The waiting loops become `GoHistoryService.until` (`x/`) or a `SessionEventPages`-driven poll. About -60 LOC.
- **Confirm:** check whether History can return an entry-less page with `last_cursor` above the cursor. `covered_cursor`'s handling of unstored intervals implies it can.

### PY-7 Three identical "wait until History has an entry matching a predicate" helpers in `x/native_contracts`

- **Category:** dedup
- **Severity:** low **Confidence:** high
- **Where:** `agentplane/x/native_contracts/test_admission_reply_loss.py:85-95` (`_recorded_turn`), `test_recovery.py:~35-49`, `test_quiesce_cutoff.py:~120-134`; near-copy `test_session_driver.py:25-35` (`history_after_turns`)
- **What:** The same `AsyncRetrying(stop_after_delay(20|30), wait_fixed(0.05))` body reads one page after `after` and asserts `any(predicate)`. Only the timeout and the error text differ.
- **Fix:** add one `entries_after(history, session_id, after, predicate=events.turn_completed)` to `x/native_contracts/conftest.py`, or as a `GoHistoryService` method built on its `until`. About -30 LOC.

### PY-8 Recording proxy writes a 502 status line into an already-started response

- **Category:** bug
- **Severity:** low **Confidence:** high
- **Where:** `agentplane/native/capture/llm_recording_proxy.py:146-165`
- **What:** `_relay` calls `_begin`, which sends status and headers, and then streams `response.read1`. If the upstream read fails mid-stream (timeout, reset), or a non-UTF-8 frame makes `frame.decode` raise, the broad `except Exception` calls `self.send_error(502, ...)`. That emits a second HTTP status line inside the SSE body. The capture records a `proxy_error`, but the harness sees a corrupted stream instead of a clean disconnect, so the live capture shows behaviour that never happens in production.
- **Fix:** track `started` (set in `_begin`). After headers have gone out, record the error and `_drop` the connection instead of calling `send_error`. About +4 LOC.

### PY-9 `local_proxy` shutdown waits on open tunnels before cancelling them

- **Category:** bug
- **Severity:** low (`x/`) **Confidence:** medium
- **Where:** `agentplane/native/claude/x/remote_io/local_proxy.py:88-94`
- **What:** On Python ≥3.12.1, `Server.wait_closed()` waits for every active connection to close. The handler tasks pumping CONNECT tunnels are only cancelled after it returns, so exiting the context while a client still holds a keep-alive tunnel hangs. Handler exceptions (such as `IncompleteReadError` from `readuntil`) are also never retrieved.
- **Fix:** cancel and gather `tasks` (or call `server.close_clients()`) before `await server.wait_closed()`. About 0 LOC net (reorder).
- **Confirm:** exit the context while a client tunnel is open.

### PY-10 `ClaudeRun`/`CodexRun` startup failure skips the process-group kill

- **Category:** bug
- **Severity:** low **Confidence:** medium
- **Where:** `agentplane/native/claude/testing/async_run.py:100-107`, `agentplane/native/codex/testing/async_run.py:168-183`; `agentplane/native/testing/async_process.py:154-164,198-211`
- **What:** When `initialize()` fails (for example, a timeout waiting for the init response), both run facades call `process.close()` instead of `process.__aexit__(type(e), e, tb)`.
  - `close()` closes stdin and then waits on `process.wait()` with no timeout and no SIGKILL. A harness stuck in startup, or a grandchild holding stdout open (`await self._stdout_task`), turns a fast failure into a Bazel timeout.
  - The `CancelledError` branch of `close()` terminates only the group leader and returns without awaiting the reader tasks.
- **Fix:** in both facades, replace `await process.close()` with `await process.__aexit__(*sys.exc_info())`. In `close()`'s cancel path, use `os.killpg` and await or cancel the reader tasks. About +3 LOC.

### PY-11 Two `wait_until_running` implementations with diverging semantics

- **Category:** dedup
- **Severity:** low **Confidence:** high
- **Where:** `agentplane/system_tests/driver/app_sandbox.py:231-285`; `agentplane/system_tests/driver/native_sandbox.py:29-46`
- **What:** Both watch the inventory for the exact target until it is `OPERATING_MODE_RUNNING` and `READINESS_STATE_READY`, and both fail on a UID replacement. The app version also reports `last_view`/status, records a span and tells deadline expiry apart from other errors. The native version raises a bare `RuntimeError` without that evidence. The readiness predicate is spelled out in both.
- **Fix:** have the native version accept the same snapshot iterator. Both clients yield `list[EnvironmentDescriptor]`, so `native_sandbox` can call `app_sandbox.wait_until_running` with a `snapshots` callable. About -18 LOC.

### PY-12 Kubernetes polling swallows every kubectl failure until the deadline

- **Category:** idiom
- **Severity:** low **Confidence:** high
- **Where:** `agentplane/system_tests/scenarios/workload_trust.py:327-333` (`contextlib.suppress(RuntimeError)` around `_object(...)`); similar `kubectl get` + `sleep` loops at `agentplane/system_tests/suites/mcp/test_delegation.py:78-90` and `agentplane/system_tests/driver/app_sandbox.py:340-349`
- **What:** `Cluster.kubectl` raises `RuntimeError` for any non-zero exit. Suppressing it turns RBAC denials, a wrong resource name or a dead apiserver into a generic `TimeoutError` 300 s later. Only NotFound is a foreseeable "not yet" state.
- **Fix:** use `--ignore-not-found` (empty output means not yet) and let other errors raise, as `_wait_for_sandbox_deletion` already does. Where a rollout condition exists, use `kubectl wait --for=jsonpath=...` or `kubectl rollout status`. About -10 LOC.

## Checked and not reported

- Fixed sleeps: nearly all 59 `sleep` sites are poll intervals inside condition loops bounded by `asyncio.timeout`. None is a bare "sleep N then assert".
- `port_forward.py`, `cluster.py`, `model_endpoint.py`, `case_batch.py` and the `x/indexing/maintenance.py` loops (logged fallbacks, cancelled on exit) have sound lifecycles.
- The `getattr`/`setattr` count (260) is almost entirely `monkeypatch.setattr` in tests. It is not probing.

## Cross-service

### CROSS-7 gRPC server option assembly repeated, with dead nil-credential checks

- **Category:** economy
- **Severity:** low **Confidence:** high
- **Where:**
  - Assembly: `agentplane/history_service/cmd/main.go:191-200`, `agentplane/notification_service/cmd/main.go:250-258`, `agentplane/session_dispatcher/internal/app/server.go:244-257`, `agentplane/environments/internal/serving/server.go:126-128`, `agentplane/egress/cmd/admin_grpc.go:61-87`, `agentplane/llm_ingress/internal/server/runtime.go:150`
  - Dead `if creds != nil`: `agentplane/action_service/cmd/main.go:600`, `agentplane/notification_service/cmd/main.go:255`, `agentplane/session_dispatcher/internal/app/server.go:246`
  - Keepalive policy: `history_service/cmd/main.go:192`, `session_dispatcher/internal/app/server.go:252`, `environments/internal/serving/server.go:127`
- **What:** Each server builds `methods.ServerOptions(t)`, `observability.GRPCServerOption()`, `grpc.Creds(…)` and sometimes an identical `KeepaliveEnforcementPolicy{MinTime: 30s}`. `ServerCredentialsWithServices` never returns `nil, nil` (`agentplane/identity/auth/listener/config.go:130-136` → `agentplane/net/peertls/expiry_credentials.go:45-57`), so the three nil checks are dead.

  Notification, egress admin and LLM ingress keep grpc-go's 5-minute `MinTime`. They also receive sidecar platform routes, whose client keepalive is 1 minute (`agentplane/egress/sidecar/internal/relay/platform_routes.go:40-44`). Only the 15 s server heartbeats prevent `too_many_pings` GOAWAYs (`agentplane/notification_service/internal/rpc/session_watch.go:23`).

- **Fix:** add `auth.Methods.NewServer(timeout, creds, extra...)`, which always adds tracing and the shared keepalive policy, and delete the nil checks. About −40 LOC.

### CROSS-8 `reader.KubernetesIdentities` is test-only, and four services wrap one cache

- **Category:** economy
- **Severity:** medium **Confidence:** medium
- **Where:**
  - `agentplane/policy/reader/identities.go:18-45` (`IdentitySource` sum type)
  - Production always passes `CachedIdentities`: `agentplane/action_service/cmd/main.go:331-337`, `agentplane/notification_service/cmd/identities.go:20-26`, `agentplane/session_dispatcher/internal/app/server.go:83-100` (three fields hold the same view), `agentplane/mcp_edge/cmd/callers.go:20-26`, `agentplane/egress/cmd/main.go:212`, `agentplane/environments/internal/serving/server.go:72`
  - Only tests pass `KubernetesIdentities{}`: `agentplane/policy/reader/fixture_test.go:202`, `agentplane/egress/internal/informer/fixture_test.go:88`, `agentplane/action_service/internal/admission/informer/fixture_test.go:80`, `agentplane/action_service/internal/rpc/fixture_test.go:148`
- **What:** The direct Identity informer is a Kubernetes Identity reader. AGENTS.md § Identities says to retire these, and no shipped binary reaches it. Keeping it costs a sum type, a wrapper struct, a type switch, and per-service `identitySources` structs whose fields alias one `*identitycache.Cache`.
- **Fix:** have `reader.Watch*` take `*identitycache.Cache`, delete `IdentitySource`, `KubernetesIdentities` and `CachedIdentities`, and collapse the per-service structs. This is blocked on giving those four tests the PolicyService-view fake (the deferred five-copies item), so sequence it after that. About −60 LOC.
- **Status:** deferred (see top).

### CROSS-9 Scheme plus controller-runtime client construction copied seven times

- **Category:** dedup
- **Severity:** low **Confidence:** high
- **Where:** `agentplane/notification_service/cmd/main.go:91-101`, `agentplane/action_service/cmd/main.go:183-189` and `:233-242`, `agentplane/session_dispatcher/internal/app/server.go:112-128`, `agentplane/policy_controller/cmd/main.go:55-67`, `agentplane/environments/cmd/main.go:56-70`, `agentplane/identity/auth/delegation.go:59-67`, `agentplane/identity/credentials/resolver/resolver.go:81-89`; also `agentplane/policy_controller/cmd/main.go:102-103` (`ctrl.GetConfigOrDie` plus its own `WrapTransport`, duplicating `kubeclient.Config`)
- **What:** Every site repeats `runtime.NewScheme()`, then N separate `if err := x.AddToScheme(…)` blocks or a hand loop, then `client.New`. apimachinery's `runtime.NewSchemeBuilder(...).AddToScheme` already does the middle step.
- **Fix:** add `kubeclient.Scheme(adders ...func(*runtime.Scheme) error) (*runtime.Scheme, error)` (a `SchemeBuilder` wrapper), plus optionally `kubeclient.Client(config, adders...)`. Make the policy controller use `kubeclient.Config`. About −50 LOC.

### CROSS-11 Settings-file loading and error redaction re-implemented per service

- **Category:** dedup
- **Severity:** low **Confidence:** high
- **Where:**
  - Path from env plus "required": `agentplane/action_service/cmd/config.go:86-89`, `agentplane/history_service/cmd/config.go:122-125`, `agentplane/notification_service/cmd/config.go:137-140`, `agentplane/mcp_edge/cmd/config.go:63-66`, `agentplane/session_dispatcher/internal/app/config.go:61-64`, `agentplane/app/internal/settings/config.go:185-188`, `agentplane/policy_controller/cmd/config.go:37-40`, plus egress, environments and LLM ingress through `configFileEnv`
  - Redaction switch duplicated for the KnownFields path: `agentplane/policy_controller/cmd/config.go:42-51`, `agentplane/action_service/cmd/config.go:287-297`, `agentplane/mcp_edge/cmd/config.go:67-73`, `agentplane/app/internal/settings/config.go:194-204`
- **What:** `configfile.RedactMapstructureError` exists, but only for the mapstructure path, so four services re-implement the same `PathError` / `ErrInvalidYAML` / `ErrMultipleDocuments` switch. They differ on `ErrTrailingYAML` and on `TrimSpace` of the path. The app wraps the raw decoder error (`%w`), which can carry the offending value, while action_service deliberately strips values ("a setting can be a credential", `action_service/cmd/config.go:301-305`).
- **Fix:** add `configfile.LoadFile(envName, service string, decode func(path string) error) error`. It reads and requires the env path and redacts every `configfile` error kind uniformly. Rename `RedactMapstructureError` to `RedactError` and cover `ErrTrailingYAML`. About −60 LOC.

### CROSS-12 Client-side timeout interceptor and token-file reads duplicated; Sampler re-implements delegation

- **Category:** dedup
- **Severity:** low **Confidence:** medium
- **Where:**
  - Unary-timeout interceptors: `agentplane/grpc/bearer/bearer.go:87`, `agentplane/identity/auth/grpc/client.go:123-129`, `agentplane/grpc/facade/facade.go:72-78`, `agentplane/sampler/internal/actions/client.go:285-291`
  - `tokenFileError` and token read: `agentplane/grpc/bearer/bearer.go:49-70` vs `agentplane/identity/auth/grpc/client.go:100-121`
  - Sampler's own delegation injection and `ErrDelegationContext`: `agentplane/sampler/internal/actions/client.go:30-32`, `:249-263`, vs shared `identity/auth/grpc.WithDelegation`/`DialDelegated`, which `agentplane/sampler/historyexport/exporter.go` already uses
- **What:** There are four copies of a 6-line interceptor and two copies of the token-file error type and its reader. The Sampler's Actions client hand-rolls the delegation envelope and its metadata-collision check, which the shared delegated dialer already provides.
- **Fix:** export one `bearer.UnaryTimeout` (or put it in `grpc/`), and share `readToken`/`tokenFileError`. Move the Sampler's Actions client to `DialDelegated`. Caveat: the Sampler opens `peertls.Config` through `Security.Open`, which supports the Workload API socket, while `ClientTLS` is file-only. Confirm that the Sampler deployment uses files, or extend `ClientTLS`, before switching. About −60 LOC.

### CROSS-13 PostgreSQL URL rewriting and `openDatabase` wrappers repeated; egress bypasses `postgres.Open`

- **Category:** dedup
- **Severity:** low **Confidence:** high
- **Where:**
  - Query-parameter rewriting: `agentplane/history_service/cmd/main.go:65-77` (read-only) and `:87-96` (`application_name`), `agentplane/egress/internal/history/store.go:58-81`, `agentplane/postgres/migrate/migrate.go:62-70`, `agentplane/testing/postgres/postgres.go:189-200`
  - Egress opens its own pool with no ping and no `DriverURL`: `agentplane/egress/internal/history/store.go:44-55`
  - Redundant `DriverURL` re-implementation: `agentplane/sampler/cmd/verify_journal.go:25`
  - `openDatabase` "X PostgreSQL unavailable" wrappers: `agentplane/action_service/cmd/main.go:115-121`, `agentplane/history_service/cmd/main.go:79-85`, `agentplane/notification_service/cmd/main.go:126-132`, `agentplane/mcp_edge/cmd/main.go:56-62`
- **What:** Five ad hoc `url.Parse` → `Query().Set` → `Encode` rewrites. Only egress and one History subcommand set `application_name`, so `pg_stat_activity` cannot attribute sessions for the other services.
- **Fix:** have `postgres.Open(ctx, url, postgres.Pool{MaxOpen, MaxIdle, ApplicationName, ReadOnly, ConnectTimeout})` apply the parameters. Callers pass their service name, and egress and the Sampler drop their copies. About −40 LOC.

### CROSS-14 Session Dispatcher hand-rolls capped exponential backoff

- **Category:** library
- **Severity:** low **Confidence:** high
- **Where:** `agentplane/session_dispatcher/dispatcher/delivery.go:543-562`; the house pattern is `agentplane/action_service/internal/worker/worker.go:50` and `agentplane/policy/identitycache/cache.go:124`
- **What:** An 18-line doubling and cap loop using `time.After`. The other services use `sethvargo/go-retry`, which STYLE.md's Go table names.
- **Fix:** `retry.Do(ctx, retry.WithCappedDuration(30*time.Second, retry.NewExponential(500*time.Millisecond)), func(ctx) error { …; return retry.RetryableError(err) })`. About −10 LOC.
