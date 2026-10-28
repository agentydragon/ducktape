# Runner delivery boundaries and harness identity

Status: **approved direction, implementation planning; harness lifecycle deployed and accepted.** This note records current code paths, approved direction, alternatives
and remaining questions. The operator approved gRPC for internal APIs, a generated HTTP/JSON facade,
and an ergonomic CLI, adopted incrementally by service and interface. The selected HTTP/JSON
facade is gRPC-Gateway. Harness lifecycle ownership is approved below (operator, 2026-10-17); its
implementation and rollout acceptance are complete. The separate lifecycle rollout was
authorized by the operator; this note does not otherwise authorize runtime/API cutover or
otherwise authorize changes to behavior, schemas, database ownership, identity mappings or caller
permissions. Keep the existing security
checks and durable queue until a replacement contract is reviewed.

The current runner contracts remain in [Session layering](thread_layering.md), the
[Dispatcher README](../runner_gateway/dispatcher/README.md), the
[Runner Gateway README](../runner_gateway/README.md), and the
[scripted harness capability index](../native/README.md#behavioral-coverage).

## Current delivery and inventory facts

The app and Notification service send Session openings and commands to the Session Dispatcher. It
records openings and commands in its own PostgreSQL database, groups delivery by Sandbox UID, and
uses per-Sandbox Kubernetes Leases plus database generations to fence replica ownership. It watches
Sandbox readiness and incarnation changes, then calls the Runner service through the Sandbox's
control Service and the Runner Gateway's proxy. It has no History Service client: routing and queue
recovery use the requested Sandbox, Kubernetes state, and runner control RPCs. See
[`delivery.go`](../runner_gateway/dispatcher/delivery.go),
[`runner_client.go`](../runner_gateway/dispatcher/runner_client.go), and
[`store.go`](../runner_gateway/dispatcher/store.go).

The Gateway connection role owns the runner's long-lived control connection and routes calls to the replica holding
that connection. The unified Go process also owns the Dispatcher queue through its separate delivery role. It does
not read the retired `sandbox_commands` Session table. The separate Gateway database resource is absent
from current manifests. The runner journals
admitted commands on its state volume. The History Service
receives runner journals and serves retained events and folded Thread views, but these are separate
service boundaries rather than a Dispatcher dependency:

- **Dispatcher has no direct History dependency to remove.** Its current routing, queue recovery,
  admission and delivery result paths use the requested Sandbox, Kubernetes state, leases and Runner
  RPCs. No Dispatcher History client or call exists in the audited implementation.
- **The app uses History for retained Thread data.** `EventLogStore` reads archived events and
  observations and validates the History Service's committed prefix; summaries come from the History
  fold. Opening a Thread does not wait for History to observe the Session because the runner pushes
  its journal asynchronously. Sending input uses Dispatcher; the runner owns native readiness.
  Live `GetStatus` supplies Sandbox diagnostics and archive safety without waking a Sandbox.
- **Notifications uses History to reconcile command outcomes.** It reads the Session journal's
  committed watermark and entries to checkpoint receipts and confirm whether a command reached the
  runner journal. This is an outcome dependency of Notification, not Dispatcher routing. A
  Dispatcher admission or queued receipt does not prove that the runner admitted a command or that a
  harness consumed it. Notice delivery
  without reading conversation history is a separate decision.
- **Runner ingress acknowledgements connect Runner to History.** Runner pushes each Session journal
  to the History ingress and persists the acknowledged cursor and seal state on its own state volume.
  During Quiesce, the Runner first drains admissions and seals the journals; its reported stage remains
  `FLUSHING` until ingress acknowledges the exact sealed cursor, then becomes `QUIESCED`. This
  Runner-to-ingress contract is not mediated by Dispatcher and does not make Dispatcher depend on
  History.

There are two different Session inventories:

- Runner `ListSessions` asks the current runner's `retained.Store` for persisted records and reports
  them with the current harness, setup, launch and activity state. The current
  production callers are the app and Notification, through the per-Sandbox proxy with expected
  Sandbox UID and owner. Dispatcher does not call the list RPC: an identical `OpenSession` retry
  returns the runner's existing attachment and a conflicting spec or setup returns `ALREADY_EXISTS`. Runner test drivers use the read for lifecycle assertions.
- History `FindSessions` and summaries describe Sessions whose Events were retained and folded. They
  can include deleted Sandboxes, do not carry Sandbox UID, and can lag the runner. They are useful
  for retained history and display, but cannot answer whether a harness is running now or whether a
  Session is present in the current runner incarnation. See the
  [History README](../history_service/README.md).

| Consumer                                                | Fact it needs                                                                                            | Present authority and disposition to review                                                                                                                                                                           |
| ------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| App Thread table, Sandbox filter/search and Thread view | Retained Thread identity, Sandbox association and folded display state, including after Sandbox deletion | History `FindSessions`/summaries through `ThreadStore` and `Summaries`; keep as retained inventory. `ContentStore` uses summary scope for retained evidence, not live presence.                                       |
| App archive safety                                      | Whether this Session has unfinished work in the current ready runner                                     | `api.py` reads `Runner.GetStatus` and checks the Session's `busy` flag. An idle process does not block archive; a Pod whose runner cannot be checked does.                                                            |
| Notification subscription                               | Session membership in the requested UID/owner Sandbox at subscription time                               | `Service.subscribe` validates namespace, UID and owner, then calls live `ListSessions`; preserve an equivalent preflight or explicitly revise offline subscription semantics. This read alone is not send permission. |
| Notification outcome reconciliation                     | Durable command journal watermark and entries                                                            | History `ReadSessionEvents` is evidence of runner admission/confirmation after an attempt, not destination inventory; its separate outcome design remains open.                                                       |

The app's History reads are appropriate for retained views. No History inventory read can stand in for
current live decisions. A narrower runner state or membership API could replace broad
`ListSessions` only after each caller's required fields, identity fence and unavailable-runner behavior
are preserved. The current app and Notification callers still require the live read.

## Destination checks and authority

Notification subscription creation currently checks distinct things. It authenticates the caller,
verifies that the destination namespace is configured, reads the current Sandbox from Kubernetes,
checks the requested UID and owner ServiceAccount, then calls live `ListSessions` to check Session
membership. Separately, it checks source access through the Action Service or GitHub client before
storing the subscription. These are subscription/source checks; they do not by themselves define a
separate per-message send grant. See
[`notification_service/api/notifications.go`](../notification_service/api/notifications.go).

When a notification is delivered, the service submits a command to Dispatcher. Dispatcher checks
its caller account, resolves the Sandbox and stores its UID with the opening or command; its worker
checks that the watched Sandbox still has that UID and that it owns the Sandbox's delivery Lease
before continuing. The Go `ProxyRunner` sends the Sandbox name and UID to the proxy with
expected-identity metadata. The proxy authenticates its caller and requires exact Sandbox UID and
owner metadata, then resolves and checks the live Sandbox and ready Pod.
The control Service routes by the holder of that UID. This is a sequence of checks across separate
requests, so the reviewed contract should say where identity is bound and where it is revalidated,
including replacement between the Dispatcher check and proxy resolution. See
[`proxy.go`](../runner_gateway/gateway/proxy.go),
[`sandboxes.go`](../runner_gateway/gateway/sandboxes.go) with the UID fence in
[`reader.go`](../sandboxes/sandboxreader/reader.go), and
[`runner_client.go`](../runner_gateway/dispatcher/runner_client.go).

The current path has different authority at each step:

| Boundary                             | Current check                                                                                                                                                                                                                       | Gap the decision must close                                                                                                                                                                                                            |
| ------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Notification subscription and worker | Caller/source access; configured namespace; live Sandbox UID and owner; live membership at creation. The worker rechecks UID and owner before sending.                                                                              | A source-read subscription is not a durable grant to send or wake after owner/grant revocation. Its Dispatcher request carries the Sandbox name and Session ID, not the stored UID.                                                    |
| App command and diagnostics          | The app resolves a retained Thread from History; live diagnostic reads carry expected UID and owner.                                                                                                                                | Retained association and a live preflight do not authorize a future command after replacement or revocation. App and Notification need the same reviewed send contract.                                                                |
| Dispatcher admission and wake        | Authenticated service-account allowlist; a separate wake-caller allowlist. It resolves the requested name in its watch, persists that UID with queued work, compares UID before delivery, and UID-guards the Kubernetes wake patch. | The caller does not supply the expected UID. Current admission can bind a same-name replacement. V0 delegates user checks to App/Notification; generalized per-destination grants and queued-work revocation need a separate decision. |
| Gateway proxy and runner             | Proxy authenticates an allowed service account and routes to the connection holder. UID and owner headers are required; the proxy checks the exact Sandbox and ready Pod. Runner rejects an unknown Session.                        | The Go Dispatcher carries the watched UID into proxy calls, rejecting a same-name replacement. Proxy's broad caller list is not a per-destination send grant; runner membership cannot be checked while offline.                       |

Current app resume and live inventory both call the Runner through the identity-fenced per-Sandbox
proxy. The Gateway Session wrappers, registry and separate database are retired.

**Approved public destination (operator, 2026-10-15):** every submission names the exact
`(Sandbox UID, Agentplane Session ID)`. Store, native-ID and process routing are runner-private;
the caller does not choose a store. Queue and retry that full address, locate the exact Sandbox,
and let queued delivery wake only that target. Once the runner is reachable, it is authoritative
for whether that Session exists; an unknown target is a terminal delivery failure, not a reason to
search other Sandboxes, create a Session, or substitute a same-name replacement. This lets offline
delivery rely on the caller's destination tuple rather
than a central Session-to-Sandbox placement registry. The Kubernetes UID-to-live-connection or
workload routing function remains separate from any session placement registry.

Wake-on-send has a durable acceptance requirement: before reporting a request as queued or initiating
wake, persist its complete destination tuple, command ID and payload. That record must survive caller
disconnect, Dispatcher process restart and the full Sandbox image-pull/boot delay. Dispatcher queue
acceptance is a different outcome from Runner admission; name each stage explicitly, and do not
present the queued receipt as proof that the runner admitted or the harness consumed the command.

The API representation and resolver remain open: decide whether the request carries a Kubernetes
namespace/name locator alongside UID or resolves by UID. Do not claim live Session membership can
be verified while the runner is unavailable. Authenticate
and authorize the sender before waking; preserve the UID identity fence, command idempotency and
outcome semantics. Direct admission, if selected, remains online-only and never wakes; only the
queued mode owns wake-to-deliver.

Review these alternatives without preselecting centralization:

1. Keep caller-side preflight checks at subscription time and make every execution boundary enforce
   its own complete destination and send policy. This gives early errors and preserves source-specific
   knowledge, but duplicates reads and can leave a time-of-check/time-of-use gap unless the immutable
   Sandbox identity is carried through admission.
2. Make one Dispatcher or Gateway admission boundary authoritative for destination namespace, UID,
   owner and Session membership, while the source service retains subscription/source authorization.
   This can give each send path one consistent fence, but the boundary must receive a trustworthy
   caller and, for generalized senders, a destination grant rather than only the Notification
   service's broad identity. It also
   needs an explicit answer for session membership when the Sandbox is suspended and the runner is
   unavailable.
3. Keep an early sender preflight for user-facing feedback and repeat only the security-critical
   checks at one authoritative command boundary. If this is selected, name which checks are advisory
   and which deny execution; do not silently treat duplicate checks as equivalent authority.

**Reviewable candidate:** use sender preflight for source access, readable errors and early
membership where the runner is reachable; make Dispatcher bind the authenticated sender and exact
Sandbox UID before accepting queued work; make the Gateway proxy enforce the same UID and owner
against Kubernetes immediately before forwarding; let the runner decide current Session
membership. The execution fence must receive the persisted expected UID, not a new name-only lookup.
The admission and execution checks are separate: early feedback cannot replace the identity fence.

**V0 sender decision (operator, 2026-10-15):** Dispatcher may trust only the App and Notification
service identities to act for their users. App owns its user-facing access checks; Notification owns
subscription and source checks. Dispatcher authenticates those named services and keeps wake authority
separately allowlisted. This is service delegation, not a general per-user or agent send grant and not
permission to infer a destination from its name. Even a trusted sender must supply the expected
Sandbox UID, which is persisted and fenced at wake and execution. This trust rule does not define
the effect of later user-grant revocation on already queued work; Dispatcher has no v0 per-user grant
to recheck. Notification separately rechecks its source and destination during
work. Do not admit direct agent callers under this exception.

**TODO — generalized send authority:** design destination- and operation-scoped grants (for example,
RBAC-backed permissions), delegation proof, revocation behavior for queued work, and revalidation
before delayed wake or execution. This is open design.

The candidate has distinct outcomes. A missing or changed UID or wrong owner denies before any wake
and again before forwarding if changed later. A temporarily unavailable runner
may leave an otherwise authorized queued command waiting; it cannot prove Session membership until
the runner returns. An available runner's unknown Session is terminal for that command. A lost
reply from direct runner admission is unknown: resolve or retry the same command ID against retained
runner storage. Loss of that storage ends runner-owned state; History is not a reconstruction source.

Two choices remain open. A namespace/name plus mandatory UID request can use the existing
namespace-local Kubernetes read and control Service as a locator; a UID-only request needs an
authoritative UID-to-object resolver that works during suspension and rejects ambiguous or stale
matches. Either representation must persist enough information to re-resolve the _same_ object and
must never promote name-only discovery to an execution address. If the runner alone selects a store,
its durable Session-to-store mapping must survive restart and a retry must not pick a different
store. The public address does not choose one versus several harnesses or stores per runner.

The decision must separately state: source/subscription authorization; destination namespace, name, UID
and owner validation; current Session membership; permission to send; and the execution-time fence.
Cover replacement under the same name, owner or grant revocation, a vanished runner, suspended
Sandboxes and delayed commands. A source-read subscription is not implicitly permission to wake a
Sandbox or send to any Session. Do not remove existing checks before an approved replacement is
implemented and its negative cases are covered.

## Delivery service and persistence alternatives

The Go Runner Gateway combines Dispatcher admission and delivery with runner connections and the
control proxy in one process and Deployment. Dispatcher owns durable admission, ordering, retries,
delivery status and guarded wake; the connection role owns the reverse stream and proxy. The stable
`agentplane-session-dispatcher` and `agentplane-runner-gateway` Services retain their token audiences.
One gRPC listener serves distinct authenticated Dispatcher, Runner proxy and runner `Connect`
surfaces. App and Notification may submit to Dispatcher v0, with a narrower wake allowlist;
`Quiesce` is controller-only, and runner `Connect` requires runner-audience and Pod/ServiceAccount
proof. Tightening the proxy's other method permissions remains separate work.

The Dispatcher PostgreSQL queue, per-Sandbox delivery Lease and database-generation fence remain
separate from the connection Lease and EndpointSlice. A worker on replica A may deliver through
`<sandbox>-control` to a runner connection held by replica B; co-location does not eliminate that
routing or the queue. The service has no History client or separate Gateway registry/database.
Runner journal ingress and Quiesce seal acknowledgements belong to History; app and Notification
also read History independently. See the [Runner Gateway README](../runner_gateway/README.md) for
current runtime and deployment ownership, including migration ordering and Flux image automation.

For persistence, compare the current durable queue with direct runner admission:

| Concern                                 | Dispatcher queue                                                                                                                                        | Direct admission RPC                                                                                                                                              |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Runner unavailable or Sandbox suspended | Persist the full destination tuple, command ID and payload before queued acceptance or wake; retain through caller/Dispatcher restart and Sandbox boot. | Requires an available runner. Reject if it is unavailable or suspended; do not wake it or wait for VM/image startup.                                              |
| Lost response                           | Query/retry the same durable command ID and read Dispatcher state; runner admission can still be uncertain until reconciled.                            | A lost reply is unknown, not rejection. Retry the same command ID against the runner journal while that storage is retained.                                      |
| Replica failure                         | Shared PostgreSQL rows, leases and generations recover queued/in-flight work.                                                                           | Caller retry or a new durable owner must survive the sender process and its failover. Runner-local journal only protects work after it is durably admitted there. |
| Runner storage loss                     | Runner-owned state is terminally lost; the queue or History is not a promise to reconstruct it.                                                         | Runner-owned state is terminally lost; no queue/History reconstruction guarantee is implied. Retained History remains a separate store and may still exist.       |
| Ordering and cancellation               | Queue defines per-Session order, stop behavior and delivery state.                                                                                      | The API must define ordering and cancellation without silently reintroducing a durable queue.                                                                     |
| Offline acceptance                      | A durable queued receipt can be returned independently of a live harness; it is not Runner admission.                                                   | Reject unavailable targets; this mode provides no offline or wake-on-send acceptance.                                                                             |

A direct RPC can reduce one durable service database only if its caller/retry contract handles lost
responses, idempotency, duplicate opens, suspended/offline runners, concurrency and failover. Do not
turn a timeout into a false rejection or an admission receipt into proof of harness execution. The
runner's journal and History evidence have different retention and availability semantics. While a
runner journal is retained, a retry with the same command ID must distinguish an unknown reply from
a new command. Loss of the runner's own storage ends that runner state; neither Dispatcher nor History
is required to recreate the runner journal, and this does not claim that separately retained History
was also lost. Any direct-admission mode is online-only and never wakes a suspended Sandbox; queued
delivery remains the mode that may retain work and wake-to-deliver. If assessing a useful deployment
without CNPG, explicitly inventory which other stateful services can be omitted or given another
persistence owner; runner-local persistence cannot accept work while the runner is absent. Removing
Dispatcher PostgreSQL alone does not remove CNPG. Current manifests declare the shared-CNPG databases
`app`, `sandbox_service`, `actions`, `egress`, `notifications` and `session_dispatcher`; the retired
`sandbox_commands` database is not part of the current manifests. The Indexing service also has a
separate PostgreSQL dependency. See the
[service database inventory](../README.md#system-overview).

The decision should recommend one of: retain the separate queue; change its boundary/placement while
preserving its contract; or replace it with a specifically defined direct-admission contract. State
who owns idempotency, durable acceptance, unknown outcomes, retry responsibility and wake behavior
before authorizing any queue or database removal.

## API transports and agent workflows

**Approved (operator, 2026-10-15); adopted one service and interface at a time.** Use gRPC for
internal service APIs, a gRPC-Gateway-generated HTTP/JSON facade over the protobuf contracts, and an
ergonomic CLI for agent workflows; no hand-authored HTTP API beside a gRPC service. Watches and waits
are the facade's newline-delimited JSON server streams, including terminal stream-error envelopes,
not SSE. Full-duplex connections stay native gRPC. Resume cursors, reconnection and retention remain
application contracts, not guarantees supplied by the facade. The facade contract is in
[gRPC services](grpc_services.md#httpjson-facades). Keep authentication and authorization, stable
errors, streaming and reconnect, wait deadlines and cancellation behavior consistent across the
facade and CLI; a transport conversion must not weaken a service's existing permission checks.

Rejected for the facade:

- **Connect:** RPC-shaped URLs and binary-enveloped streams that `curl` and `jq` cannot read, and a
  second agent-facing path beside gRPC-Gateway. The parked PRs (#7079, #7083) saw an open stream
  yield no frames ([transport experiments](thread_view_sync.md#transport-experiments)).
- **gRPC-Web through Envoy:** a translation hop in production and in browser acceptance (same
  experiments).
- **Envoy `grpc_json_transcoder`:** a service's HTTP API would live in cluster edge config; the facade
  travels with the service.
- **SSE through a custom gateway marshaler:** a community workaround, not an upstream mode, which
  gives agents and browsers different stream formats.

Agent tooling should support both concise commands and composable JSON for listing Action groups and
Actions, inspecting an Action schema, reading the authenticated caller's own Action status/results,
waiting for completion, and subscribing to or cancelling GitHub PR notifications. The
[`agentplane` CLI](agent_cli.md) uses each service's generated client and HTTP facade as appropriate.

## Harness process, store and Session identity

Each Claude Session runs its own supervised native process, with `CLAUDE_CONFIG_DIR` in the
Session's state directory. Every Codex Session of a Runner is one thread of the Runner's one Codex
app-server ([`codex_app_server.go`](../runner/codex_app_server.go)), whose `CODEX_HOME` is `<state_dir>/codex`.
The app-server's command line and `CODEX_HOME` are process-wide. `thread/start` and `thread/resume`
carry each Session's working directory, model, effort, developer instructions and a thread `config`
map, whose `shell_environment_policy.set` gives a thread's tools its Session's notification
destination. The Session record stores one Claude session ID or Codex thread ID, and a runner restart
creates a new process and resumes the native conversation from that store. See
[`session/`](../runner/session/), [`supervision/`](../runner/supervision/),
[`retained/`](../runner/retained/), and the adapters ([Claude](../runner/claude/adapter.go),
[Codex](../runner/codex/adapter.go)).

The shared app-server starts when a Codex Session first needs a harness and stays up until the
Runner stops or quiesces; it is not reaped when idle. Stopping is phased: every Session halts first
(its active turn interrupted), each Claude process stops, then the app-server stops once. Its frames
are assigned by thread ([`codex/router.go`](../runner/codex/router.go)): a response to the Session
whose request it answers, a frame naming a thread to that thread's Session or, for a sub-agent, its
root's, and a frame naming none, like stderr, to every hosted Session. Thread creation is serialized,
so a thread naming itself before its `thread/start` response belongs to the one Session creating it.

The app-server has one cgroup and memory budget, so one crash or OOM ends the active turn of every
hosted Codex Session (`PROCESS_LOST`); each resumes its thread when it next needs a harness. That is
the cost of sharing. Measured with the pinned Codex 0.157.0 against a model endpoint that never
answers (2026-10-24), eight Sessions take 131 MiB idle and 159 MiB with every turn waiting on the
model in one app-server, against 258 and 299 MiB proportional set size (969 and 1101 MiB summed
RSS) in eight.

**Decision (operator, 2026-10-24):** the operator asked for at most one Codex app-server per Runner
(2026-10-17 PDT). The per-Session Codex homes kept before it are left behind, not migrated: "I'd say
we leave it behind. I don't have any running sandboxes right now." (17:38 UTC). A Codex Session
created before it fails to resume its thread.

`HarnessStarted` records `resumed` and a PID, not a unique process-incarnation ID. PID is useful
process-local diagnostic data, but may be reused and does not identify a spawn over time. The stored
`native_session_id` survives process restart; it is a Claude session ID or Codex thread ID, not the
Agentplane public Session ID. The public ID also keys the Runner directory, Dispatcher opening and
History record. These are already separate identity roles even though the current address usually
looks like Sandbox plus public Session ID.

Keep three axes explicit in the design:

- **Process incarnation:** one unique identity per harness process spawn, useful for routing and
  liveness; it changes on process restart. A PID alone is not a stable identity.
- **Harness persisted data/store:** data reused by a later process, such as a Claude Session's
  configuration directory or the Runner's Codex home. The same path/data may outlive a process. Decide
  whether successive processes using the same store count as the same harness object, and what
  evidence distinguishes a resumed process from a new store lineage.
- **Native Session:** the native conversation identity within the harness/store, which may be scoped
  to that store rather than globally unique. Keep it distinct from Agentplane's public Session ID and
  any native child identity.

### Coherent harness-ownership alternatives

One coherent option is exactly one harness type and one persisted harness store per Runner, with its
process lifecycle coupled to the Runner. If that harness safely supports multiple independently
interactive Sessions, all Sessions in the Runner can route through it. This is the only single-harness
variant here that removes store-address ambiguity, but all Sessions share the store, raising isolation
and concurrency questions. It also gives up mixed Codex and Claude Sessions in one Sandbox/Runner;
compare per-Sandbox harness selection and homogeneous templates with multiple-harness flexibility.

The other coherent option allows multiple harness processes and stores per Runner, with each process
potentially serving multiple Sessions if the pinned harness supports it. Do not impose an arbitrary
same-harness-type rule. This preserves mixed Codex/Claude use but requires explicit process/store
identity and Session-to-store/process routing. Once infrastructure supports multiple processes and
stores, a same-type-only rule adds little architectural simplification; actual resource limits and
the harnesses' proven capabilities still constrain what can run. Restricting a deployment to one
later is a smaller step than adding identities and routing after committing to one; this modest
migration-flexibility advantage favors keeping the multi-harness option available, but does not
settle the choice.

Do not use a one-process-at-a-time, one-harness-type design that switches among per-Session stores as
a middle ground. It retains per-Session store addressing after restart while also forbidding mixed
harness types and coupling every Session's process availability; it combines the constraints of both
alternatives without the fixed-store routing simplification. A process restart always creates a new
incarnation; a persisted store can remain the same across it. Native child Sessions that are merely
discoverable or read-only do not establish that one process can host multiple independently
interactive Sessions. Use the separate Codex and Claude characterization evidence before evaluating
either alternative. These are design options, not implementation authorization.

**Approved public address and process boundary (operator, 2026-10-15):** process
allocation and routing are internal to the runner. Neither a PID nor a process incarnation belongs
in a durable public address, and there is no public `create Session on process` operation or refusal.
The public destination is `(Sandbox UID, Agentplane Session ID)`, with store/native/process mapping
private to the runner; no public store field is needed.
Creating a Session can select its harness kind while the runner manages a default store per kind;
that is a candidate policy, not a settled one-versus-many store/process choice. An adapter may
arrange one Codex app-server process per store or the current Claude stream-json process per root
Session. Process instances may appear as diagnostics with hosted Sessions/status. At-most-one
process per store and shared-store concurrent safety remain open.

Current [`SessionSpec`](../proto/runner.proto) already selects Claude or Codex and has no store
selector; the runner creates `CLAUDE_CONFIG_DIR` under the Session's runner-owned directory and
one `CODEX_HOME` for the Runner. No production in-repo caller currently chooses either directory. A private default-store
policy remains a candidate; future store-management needs can be considered separately without
putting a store ID in the current Session destination.

The Sandbox UID must identify the exact incarnation; namespace/name may be a locator, but its wire
syntax and UID resolver are open. A runner-owned store ID names one durable lineage and is never
recycled, even after retirement; it is not a path or process. A public Session ID names a logical
Session, durably mapped to its selected private store and native Claude/Codex ID. Native IDs may
collide across stores. Define authorization binding, unavailable/suspended lookup and store movement
without promoting a same-name Sandbox replacement.

**Recommended creation contract for review, not an approved ID-allocation decision:** keep
client-chosen, globally unique public IDs for client-created root Sessions. The exact `(Sandbox UID,
Session ID)` and creation spec are the idempotency key. A retry with the same address and spec
returns the same opening; reuse of the public ID with another Sandbox or spec conflicts. The runner
first durably reserves the public ID
and its chosen private store binding, then asks the adapter to create a native Session,
then durably records the harness-chosen native ID before reporting a completed opening. A queued
opening is only acceptance; it does not assert native creation. Store a stable public-to-native
mapping in runner-owned state, scoped by store, and reuse it after process or Runner restart. Do not
make a native ID, PID or process incarnation the public retry key.

This ordering does **not** make native creation exactly once: Codex may create a thread and the
process may die before the returned native ID is committed. Recovery must inspect durable native
store evidence or use a proven native idempotency mechanism before binding that thread. If neither
can establish which native Session belongs to the reservation, mark creation unresolved and stop
automatic native-create retries; expose that non-success state through the reviewed API and retain
the possibly orphaned native data for reconciliation. A lost response after the mapping
commit resolves by the same public ID. Claude's current adapter chooses a native ID before launch;
that narrower path does not solve the harness-chosen-ID crash window in general. Specify the
reservation/mapping format, native reconciliation proof, and failure status before implementation.
Today [`Session.startHarness`](../runner/session/native.go) writes the native ID only after adapter handshake;
[`CodexAdapter`](../runner/codex/adapter.go) starts a native thread during that handshake, and
[`ClaudeAdapter`](../runner/claude/adapter.go) selects an ID before launch but also persists it afterward.
Persisting Claude's chosen ID before launch can narrow its case; Codex still needs a proven recovery
path for an ID assigned by the harness.

**Store and Session lifecycle proposal for review:** a store has a durable internal ID, harness type
and binding to the exact Sandbox UID; it survives process exit, Runner restart and Sandbox suspension.
The runner creates or reuses a store under its selected policy. Public store operations are needed
only if callers truly need to choose or manage stores; the ordinary API opens Sessions, sends input,
interrupts an exact turn and changes model/effort unless a harness proves an option is necessarily
shared. A process has only ephemeral start/health/hosted-Session status and runner-selected capacity; it is never the
API target for opening a Session. The runner may restart or replace a process without
changing the store or public Session address. A dormant Session keeps its mapping and data for
runner-managed resume on input. Retiring a store stops new Sessions and requires an explicit policy
for existing ones; deleting its authored native files is a separate, explicitly authorized data operation, never a
side effect of stopping a Session/process, failed creation or Sandbox replacement. Moving/copying a
store across Sandbox UIDs needs a new binding and authorization; do not silently preserve the old
destination. Until shared-store concurrent writes are proven safe for a pinned adapter, serialize or
exclude them within that store. Exact create/retire/delete RPC shapes and retention policy remain
open.

Native subagent IDs remain harness-chosen. If a discovered child is exposed as a public Session,
durably register its native ID with store and parent public Session before publishing it, assigning
a stable runner-chosen public ID on first discovery and reusing it on replay/restart. Parentage, visibility in History/UI and independent control
are separate: a child may be visible and read-only even when the harness cannot accept direct input
for it. A child's Sandbox/store lineage does not automatically grant the parent's send permission.
Child Session projection is deferred; the selected native
delegation scope does not expose child Sessions or independent controls. This identity contract does not presume Claude root multiplexing from the bounded
single-root stream-json evidence or select one versus several processes per store.

When a runner is reachable, a missing public Session is a
terminal unknown-target result; no other store, process or same-name replacement is searched. When suspended
or unreachable, queued acceptance cannot prove live membership and keeps the exact tuple until
delivery. Loss of runner storage ends runner-owned state; retained History is not a native store or
mapping reconstruction source. An unknown delivery response is handled by the same command ID,
not by inventing another Session.

## Runner-owned harness lifecycle

**Approved direction (operator, 2026-10-17).** Callers address durable Sessions; the runner owns
harness startup, native conversation preparation, reconciliation and process replacement. Process
allocation stays internal. This decision does not select process/store counts, shared-store write
safety or the remaining native-ID creation contract above.

### Admission and execution

Dispatcher durably queues the exact destination, command ID and payload before acknowledging queued
acceptance or requesting authorized Sandbox wake. Runner validates membership and durably admits
input **before** starting or resuming its harness. Admission is not native readiness or confirmation
that the harness consumed the input. An unknown Session is rejected, never implicitly created.

Once admitted, work belongs to the runner despite caller disconnect. It ensures setup and native
conversation preparation, reconciles durable evidence, then delivers input. Concurrent inputs share
one necessary startup attempt and preserve Session input order. Startup must not hold the admission
lock across native I/O or prevent controls from being processed. Definitive startup failures become
journaled command failures with their concrete cause. Specify bounded transient retries and how
all affected admitted commands settle; do not leave them pending indefinitely. Ambiguous native
creation or delivery requires reconciliation before retry, not blind recreation or replay of tool
effects. Same command ID/payload returns the original admission; conflicting reuse is rejected.

### Runner protocol and callers

- The Runner API has no `ResumeSession` or `StopRunnerSession` RPC, executable stop command
  variant, or corresponding Dispatcher/client surface; the removed wire field numbers and names are
  reserved. Add no process kill/restart RPC.
- Keep `OpenSession` as an idempotent durable Session reservation with immutable creation
  configuration. Its acknowledgement does not prove a running harness or completed native creation;
  repeated open confirms the reservation rather than acting as a resume command.
- Keep the `SubmitInput` request and admission response shapes. Return durable admission without
  waiting for startup; later startup/delivery outcomes belong in the journal.
- Keep `InterruptTurn` and its required target `turn_id`. For a new command, atomically check the
  active turn and journal admission; a mismatch returns `FAILED_PRECONDITION` without admission.
  A retry of an already admitted command returns its original admission before checking current turn
  state. Serialize native interrupt dispatch with turn transitions so it cannot affect a successor.
  If the target finishes naturally after admission, record that outcome. Interrupt never starts a
  harness or cancels other queued/admitted inputs. No Session-wide interrupt or pause is introduced.
- Preserve Sandbox `Quiesce`: refuse new execution, settle work, stop harnesses, seal journals
  and wait for ingress acknowledgement before reporting quiescence. Serialize admission against
  quiescing; a racing send is either owned by the runner or remains queued for retry/wake.
  Let running Session setup and Sandbox bootstrap scripts finish naturally before teardown
  completes, rather than cancelling them partway through initialization. Orderly runner shutdown
  also waits for them; unexpected process loss is recorded as interrupted and never silently rerun.

Keep the guarded turn interrupt control and distinct Sandbox suspend/resume controls. Sending to a
dormant Session uses runner-managed readiness; existing Sandbox wake authorization still applies.
The app's manual resume changes Sandbox lifecycle state through Kubernetes; retained native state
recovery belongs to the Runner.

### Provisional introspection model and Sandbox page

Expose a coherent runner snapshot of harness processes and Sessions for frontend introspection.
Include per-spawn process identity, PID, harness kind, process state and assigned Session IDs;
per-Session status includes its assigned process (if any), persisted native conversation ID (if
known), preparation/readiness, setup state, active turn and failure evidence. Store identity may
appear as diagnostic information. Commands continue to target public Sessions, never a process ID.

The initial Session states are `DORMANT`, `PREPARING`, `READY` and `FAILED`. `READY` means native
conversation creation/resume and reconciliation have completed and the process can serve that
Session; no turn need have started. `PREPARING` includes process assignment/startup and conversation
preparation. Assignment may therefore precede readiness. A running Codex app server does not make
all conversations in its data directory ready or assigned: retained conversations remain dormant
until the runner prepares them. The default is lazy preparation when input needs it. Process status
uses `assigned_session_ids`, including preparing Sessions, rather than implying that all are ready.

Show process topology, Session mappings and per-Session status on the frontend **Sandbox page**.
Distinguish live status from unavailable status when suspended or disconnected; reading status must
not wake a Sandbox. Identify any retained snapshot as stale. Status fields and enum shapes are a
provisional information model and may change; clients must not use them to authorize, route or
coordinate lifecycle operations. The runner remains authoritative at execution boundaries.

### Scope and acceptance

V1 covers admission-before-startup, runner-managed readiness/reconciliation, exact-turn interruption,
removal of explicit harness lifecycle controls, and Sandbox-page introspection. Validate both Claude
and Codex for concurrent sends, startup failure, caller disconnect/retry, runner restart, ambiguous
native outcomes, stale interrupts and turn-transition races. Prove context retention with a follow-up
input after restart. Cover send/quiesce races and identity/wake denial boundaries. Review desktop and
phone screenshots of Sandbox diagnostics and the updated Session controls. Archive checks unfinished
Session work rather than harness process existence; an idle live Session can be archived without a
shutdown button.

**V2, deferred:** idle Claude process reclamation to reduce memory. Measure idle RAM and define safe
eligibility and thresholds first. Reaping must not end active turns, pending commands, dependent
children, background work or another hosted Session. Startup on send provides the recovery path;
V1 does not implement automatic idle reaping or depend on it. Shared-process placement and store
concurrency remain separate decisions.

### Rollout and rollback

The implementation changes protocol and callers together; publish a staging image-promotion hold
before merging it. Testing receives the release first. The held staging image families are the app,
Gateway/Dispatcher, Notification and History services (including their migration images), `runner`,
`runner-ducktape` and the runner VM. Testing acceptance must identify the exact tested tags rather
than assuming every image shares a build SHA.

1. Validate the release on fresh Claude and Codex Sessions in testing, including Pod and VM paths.
   Test retained state created by the old runner as well as new state. Cover automatic startup on
   input, context retention, startup failure, interruption, retry/reconnect and suspend/wake. Review
   Sandbox diagnostics and the controls on desktop and phone.
2. Inventory staging Sessions, pending Dispatches, historical receipts, journals/native histories,
   workspace and state volumes. Settle outstanding old stop commands before removing their wire
   variant; keep settled historical receipt readers. Hold Sandbox starts and automatic wake through
   cutover so no old runner can execute work against the changing service stack. Preserve admitted work and
   all person-authored data. Back up at a consistent boundary and verify restore in isolation before
   any persistent-format migration; do not restore an old snapshot over post-cutover writes.
3. Keep staging Sandbox starts disabled, quiesce any remaining runners with the old stack, and verify
   no runner workloads remain. The operator suspended all four staging Sandboxes and agreed to hold
   new starts (2026-10-17); recheck this condition immediately before cutover. Testing Sandboxes may
   be deleted/recreated under the operator's authorization. Staging Sandbox CRs, UIDs and retained
   PVCs must survive.
4. With zero staging runner workloads and starts held, promote the exact tested service and template
   tags together through GitOps and restore their Flux Image Automation markers in the same PR
   (operator, 2026-10-17). Flux manages the pins once that promotion merges. Normal Deployment
   rollouts are sufficient; no separate service stop or Flux suspension is needed (operator,
   2026-10-17). Upgrade every template, including suspended-only and VM templates. Verify all new
   service replicas, image revisions and migration gates, and wait for old service Pods to disappear
   before allowing any Sandbox start. If runners reappear, stop promotion and quiesce them first;
   updating a template alone does not upgrade a running Sandbox.
5. Activate retained Sessions with the new runner and verify native continuation, reconciliation,
   unchanged archived history and retained storage. Testing's follow-up model turns prove context
   recall on both harnesses. Check exact-turn rejection and process/Session status. Keep the rollout
   incomplete until behavior is observed, not just Flux or Deployment readiness. Record the actual
   deployed revisions as Flux continues managing the pins. The human staging UI drive follows
   lifecycle rollout; record operator-reported acceptance separately from automated coverage.

**Lifecycle rollout accepted (2026-10-17 PDT).** Implementation #10950, promotion #10961
and explicit Claude `bypassPermissions` #10965 are deployed. Flux Image Automation owns all service
and runner image pins; the temporary starts/wake hold is released. Deployed acceptance covered
Pod/VM context retention, dormant/ready status, retained staging history and volume identities,
and the two Claude tool cases after the permission change.

**Operator UI acceptance (2026-10-17 PDT):** the operator reports a basic Codex message worked,
then test-drove idle suspension after the five-minute interval and successfully sent a message to
wake the Sandbox. This is acceptance of the tested staging UI path; it does not establish a
separate VM UI drive.

Keep previous image revisions and a tested persistent-format rollback policy. Before new work is
accepted, revert the coordinated release if necessary. After new writes, reconcile/preserve them;
rollback must not overwrite new authored data or replay command side effects. No live data deletion
or Sandbox replacement is implicit in this rollout.

## Harness capability boundary

The [test capability index](../native/README.md#behavioral-coverage) and its linked modules
record what the pinned binaries do in scripted scenarios. Codex's test demonstrates two root
threads over one app-server. The pinned Claude Code 2.1.252 test shows that a candidate `session_id`
on an inbound `user` frame does not route to a concurrent second root: its lifecycle receipt names
the process's selected root, and it queues while that root is active. Separate Claude processes
with distinct workdirs and config
directories can make concurrent model requests with different model settings; interrupting one
process leaves the other's request usable. Different credentials, sharing one config directory
across processes, and concurrent multi-process crash recovery remain untested. The existing
single-session `--resume` tests cover restart of one root in a new process.

The operator reports a finding from the private Claude remote-control investigation: an
orchestrator daemon polls outbound HTTP for session-start work and launches each Session in its
own Claude process speaking RemoteIO, which the operator describes as essentially the existing
stream-json stdio protocol. This has not been reproduced as a runtime process-count test here.
It gives no process-sharing benefit to adopt that mode solely for multiplexing. Use one Claude
process per root Session as the present allocation baseline and allow Codex to use its
demonstrated shared app-server, both behind the same public Session interface.
Neither process policy belongs in the public address or requires one process per store; another
Claude mode could change the evidence later.

Neither result decides how many processes or stores a Runner should use, whether concurrent Sessions
may safely write one store, or which native children can be controlled. The approved public address
does not settle those internal allocation questions; these tests do not authorize runtime changes.

## Runner delivery acceptance without History

A focused delivery integration target can omit the History Service while retaining PostgreSQL for
the Session Dispatcher queue. The test owns a small real `RunnerIngress` endpoint implementing the
runner's journal push and acknowledgement protocol, drives the Session Dispatcher API, and waits
for the runner's journal receipt to arrive at that endpoint. Correlate the delivered command by
Session ID and command ID; verify ingress cursor acknowledgements and that Quiesce reaches
`QUIESCED` only after the exact sealed cursor is acknowledged. This exercises queued delivery,
runner journaling and the ingress flush contract without a History fold, archive read or Electric
projection. It is a no-History test tier, not a CNPG-free tier: the Dispatcher still uses its
PostgreSQL queue.

## CNPG-free K3s integration tier

The proposed CNPG-free K3s tier is a deliberately narrow integration environment for independently
usable, loosely coupled components. It is not a full Agentplane deployment or a replacement for
CNPG. Once a reviewed execution contract removes the tested paths' actual execution-database
dependencies, the tier should exercise only flows that can run without those dependencies. It may
omit Notifications, History, Electric, the CNPG operator and PostgreSQL; it does not need parity from
services it omits. Keep other stateful services and their data owners explicit rather than implying
that removing one Dispatcher database removes every database dependency.
This is separate from the no-History delivery test above: a flow that still uses the PostgreSQL-backed
Dispatcher queue is not part of a tier that omits PostgreSQL.

Cover both Pod and VM runner paths, with test ingress where a tested flow needs it. VM coverage must
boot a real supported VM fixture: the existing k3s suite does not install KubeVirt and therefore
exercises only Pod Sandboxes, not VM execution. Keep the test-tier design and
implementation are separate work, gated on the reviewed execution-persistence contract and
the real VM fixture prerequisite.
