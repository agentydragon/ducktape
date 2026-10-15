# Runner delivery boundaries and harness identity

Status: **design review only.** This note records current code paths, approved direction, alternatives
and remaining questions. The operator approved gRPC for internal APIs, a generated HTTP/JSON facade,
and an ergonomic CLI, adopted incrementally by service and interface. The selected HTTP/JSON
facade is gRPC-Gateway. This does not authorize runtime/API cutover. Other sections do not authorize changes to behavior,
schemas, database ownership, identity mappings or caller permissions. Keep the existing security
checks and durable queue until a replacement contract is reviewed.

The related decision nodes are in [section 14 of the task DAG](../plans/task_dag.md#14-delivery-boundaries-apis-and-harness-identity).
The current runner contracts remain in [Session layering](thread_layering.md), the
[Dispatcher README](../session_dispatcher/README.md), the [Gateway API](../runner_gateway/API.md),
and the [scripted harness capability index](../harness_tests/README.md#behavioral-coverage).

## Current delivery and inventory facts

The app and Notification service send Session openings and commands to the Session Dispatcher. It
records openings and commands in its own PostgreSQL database, groups delivery by Sandbox UID, and
uses per-Sandbox Kubernetes Leases plus database generations to fence replica ownership. It watches
Sandbox readiness and incarnation changes, then calls the Runner service through the Sandbox's
control Service and the Runner Gateway's proxy. It has no History Service client: routing and queue
recovery use the requested Sandbox, Kubernetes state, and runner control RPCs. See
[`delivery.py`](../session_dispatcher/delivery.py),
[`runner_client.py`](../session_dispatcher/runner_client.py), and
[`store.py`](../session_dispatcher/store.py).

The Gateway owns the runner's long-lived control connection and routes calls to the replica holding
that connection. It does not persist Dispatcher commands. Its separate `sandbox_commands` database
stores the legacy public Session registry, written only by Gateway `CreateSession`; it is not the
Dispatcher queue. The runner journals admitted commands on its state volume. The History Service
receives runner journals and serves retained events and folded Thread views, but these are separate
service boundaries rather than a Dispatcher dependency:

- **Dispatcher has no direct History dependency to remove.** Its current routing, queue recovery,
  admission and delivery result paths use the requested Sandbox, Kubernetes state, leases and Runner
  RPCs. No Dispatcher History client or call exists in the audited implementation.
- **The app uses History for retained Thread data.** `EventLogStore` reads archived events and
  observations and validates the History Service's committed prefix; summaries come from the History
  fold. Opening a Thread does not wait for History to observe the Session because the runner pushes
  its journal asynchronously. App resume separately checks the app's Thread/feed state and live
  `Runner.ListSessions` for the current runner's retained Session and spec.
- **Notifications uses History to reconcile command outcomes.** It reads the Session journal's
  committed watermark and entries to checkpoint receipts and confirm whether a command reached the
  runner journal. This is an outcome dependency of Notification, not Dispatcher routing. A
  Dispatcher admission or queued receipt does not prove that the runner admitted a command or that a
  harness consumed it. Keep the
  [`NOTIFICATION_COMMAND_OUTCOME_DESIGN`](../plans/task_dag.md#notification_command_outcome_design--notice-delivery-without-reading-conversation-history)
  as a separate decision.
- **Runner ingress acknowledgements connect Runner to History.** Runner pushes each Session journal
  to the History ingress and persists the acknowledged cursor and seal state on its own state volume.
  During Quiesce, the Runner first drains admissions and seals the journals; its reported stage remains
  `FLUSHING` until ingress acknowledges the exact sealed cursor, then becomes `QUIESCED`. This
  Runner-to-ingress contract is not mediated by Dispatcher and does not make Dispatcher depend on
  History.

There are two different Session inventories:

- Runner `ListSessions` asks the current runner's `SessionStore` for records it has retained and
  reports live harness, setup, launch and activity state. The app uses it to check archive/resume
  conditions; Notification uses it to check that a requested Session belongs to the destination at
  subscription time; Dispatcher uses it after runner `OpenSession` returns `ALREADY_EXISTS`. Runner
  test drivers also use it to check lifecycle behavior. The current production callers are in
  [`bridge.py`](../app/threads/bridge.py),
  [`notification_service/service.py`](../notification_service/service.py), and
  [`session_dispatcher/delivery.py`](../session_dispatcher/delivery.py).
- History `FindSessions` and summaries describe Sessions whose Events were retained and folded. They
  can include deleted Sandboxes, do not carry Sandbox UID, and can lag the runner. They are useful
  for retained history and display, but cannot answer whether a harness is running now or whether a
  Session is present in the current runner incarnation. See the
  [History README](../history_service/README.md).

Do not replace a live-state check with History merely because both APIs mention Sessions. First
review what each caller needs and whether that need is actually live. The Dispatcher `ALREADY_EXISTS`
list fallback has a separate known correctness issue: ID presence alone can misclassify a conflicting
spec as a successful retry. Keep that bounded correction independent from the wider queue or
service-boundary decision.

## Destination checks and authority

Notification subscription creation currently checks distinct things. It authenticates the caller,
verifies that the destination namespace is configured, reads the current Sandbox from Kubernetes,
checks the requested UID and owner ServiceAccount, then calls live `ListSessions` to check Session
membership. Separately, it checks source access through the Action Service or GitHub client before
storing the subscription. These are subscription/source checks; they do not by themselves define a
separate per-message send grant. See
[`notification_service/service.py`](../notification_service/service.py).

When a notification is delivered, the service submits a command to Dispatcher. Dispatcher checks
its caller account, resolves the Sandbox and stores its UID with the opening or command; its worker
checks that the watched Sandbox still has that UID and that it owns the Sandbox's delivery Lease
before continuing. The current `RunnerClient` sends the Sandbox name to the proxy, not the original
UID/owner as expected-identity metadata. The proxy authenticates its caller and resolves the live
Sandbox name to a UID; it can also require exact UID and owner headers when a caller supplies them.
The control Service routes by the holder of that UID. This is a sequence of checks across separate
requests, so the reviewed contract should say where identity is bound and where it is revalidated,
including replacement between the Dispatcher check and proxy resolution. See
[`runner_proxy.py`](../runner_gateway/runner_proxy.py),
[`destinations.py`](../runner_gateway/destinations.py), and
[`runner_client.py`](../session_dispatcher/runner_client.py).

**Approved preferred direction (operator, 2026-10-15):** every submission names its destination as
the Sandbox UID, an optional persisted harness/store ID, and the Session ID. Queue and retry that
tuple, locate the exact Sandbox, and let queued delivery wake only that target. Once the runner is
reachable, it is authoritative for whether that Session/store exists; an unknown target is a
terminal delivery failure, not a reason to search other Sandboxes, create a Session, or substitute a
same-name replacement. This lets offline delivery rely on the caller's destination tuple rather
than a central Session-to-Sandbox placement registry. The Kubernetes UID-to-live-connection or
workload routing function remains separate from any session placement registry.

Wake-on-send has a durable acceptance requirement: before reporting a request as queued or initiating
wake, persist its complete destination tuple, command ID and payload. That record must survive caller
disconnect, Dispatcher process restart and the full Sandbox image-pull/boot delay. Dispatcher queue
acceptance is a different outcome from Runner admission; name each stage explicitly, and do not
present the queued receipt as proof that the runner admitted or the harness consumed the command.

The API representation and resolver remain open: decide whether the request carries a Kubernetes
namespace/name locator alongside UID or resolves by UID, and how an optional store ID is represented.
Do not claim live Session membership can be verified while the runner is unavailable. Authenticate
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
   caller and destination grant rather than only the Notification service's broad identity. It also
   needs an explicit answer for session membership when the Sandbox is suspended and the runner is
   unavailable.
3. Keep an early sender preflight for user-facing feedback and repeat only the security-critical
   checks at one authoritative command boundary. If this is selected, name which checks are advisory
   and which deny execution; do not silently treat duplicate checks as equivalent authority.

The preferred submission address is the caller-provided Sandbox UID, optional persisted harness/store
ID and Session ID. The exact Kubernetes locator is still open: namespace/name plus UID or UID-based
resolution may locate the object, but the UID must fence identity. Persist the destination tuple
through queued wake and delivery, then send only to that exact Sandbox. Name-only lookup remains a
separate discovery/selection operation; it must not silently become an execution address. UID is an
identity fence, not authorization: authenticate and authorize the sender before wake, and separately
enforce any owner, grant and send permissions at the reviewed authority. The runner rejects an
unknown Session/store when reachable; do not search/reroute, implicitly create it, or substitute a
same-name Sandbox. Offline queue admission cannot prove live Session membership. Review which
boundary revalidates identity and permission and how it reports terminal unknown-target outcomes.

The decision must separately state: source/subscription authorization; destination namespace, name, UID
and owner validation; current Session membership; permission to send; and the execution-time fence.
Cover replacement under the same name, owner or grant revocation, a vanished runner, suspended
Sandboxes and delayed commands. A source-read subscription is not implicitly permission to wake a
Sandbox or send to any Session. Do not remove existing checks before an approved replacement is
implemented and its negative cases are covered.

## Delivery service and persistence alternatives

Dispatcher and Gateway currently have sequential but different responsibilities. Dispatcher owns
sender-facing durable admission, ordering, retries, delivery status and its optional guarded wake
path. Gateway owns the runner connection and the control proxy. Keeping the deployments separate
preserves those failure, scaling and credential boundaries but adds a service hop and two sets of
configuration. A proposed merge must say whether it means one API, one process/deployment, or one
state owner; those are different changes. Evaluate whether joining queue ownership to the Gateway's
connection lifecycle complicates replica routing, rollout, authorization or independent recovery.
Do not merge merely because every queued delivery eventually calls the proxy.

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
Dispatcher PostgreSQL alone does not remove CNPG. The current shared-CNPG databases are `app`,
`sandbox_commands`, `sandbox_service`, `actions`, `egress`, `notifications` and `session_dispatcher`;
the Indexing service also has a separate PostgreSQL dependency. See the
[service database inventory](../README.md#system-overview).

The decision should recommend one of: retain the separate queue; change its boundary/placement while
preserving its contract; or replace it with a specifically defined direct-admission contract. State
who owns idempotency, durable acceptance, unknown outcomes, retry responsibility and wake behavior
before authorizing any queue or database removal.

## API transports and agent workflows

**Approved direction; implementation design remains open.** Use gRPC for internal service APIs, a
gRPC-Gateway-generated HTTP/JSON facade over the protobuf contracts, and an ergonomic CLI for agent
workflows. Adopt these interfaces incrementally, one service and interface at a time; do not plan a
repo-wide cutover as one change. Reuse the repository's protobuf and code-generation tooling rather
than maintaining a second, hand-authored HTTP API alongside the gRPC service.

The operator selected [gRPC-Gateway](https://github.com/grpc-ecosystem/grpc-gateway) as the facade.
Use its newline-delimited JSON server streams for watches and waits, including terminal stream-error
envelopes; this is not SSE framing. Keep full-duplex connections on native gRPC. Resume cursors,
reconnection and retention remain application contracts, not guarantees supplied by the facade.

The exact generation path, first service, API-specific contract, rollout sequence and CLI command
syntax remain open. Resolve those through a bounded integration spike and reviewed service plans. Keep authentication and authorization, stable errors,
streaming and reconnect, wait deadlines and cancellation behavior consistent across the generated
facade and CLI; a transport conversion must not weaken a service's existing permission checks.

Agent tooling should support both concise commands and composable JSON for listing Action groups and
Actions, inspecting an Action schema, reading the authenticated caller's own Action status/results,
waiting for completion, and subscribing to or cancelling GitHub PR notifications. The detailed CLI
contract remains a separate DAG item. No runtime API or CLI implementation is authorized by this
direction alone.

## Harness process, store and Session identity

Current Agentplane code creates one `HarnessProcess` inside a runner `Session`; the Session record
stores one Claude session ID or Codex thread ID, and its state directory holds that harness's
persistent data. Claude's `CLAUDE_CONFIG_DIR` and Codex's `CODEX_HOME` are below the per-Session
store. A runner restart can create a new process and resume the native conversation from that store.
This is the current implementation, not an invariant that one process must always own exactly one
Agentplane Session. See [`session.py`](../runner/session.py),
[`harness_process.py`](../runner/harness_process.py),
[`store.py`](../runner/store.py), and the adapters
([Claude](../runner/claude.py), [Codex](../runner/codex.py)).

The current lifecycle is Session-scoped: `Runner` keeps a map of `Session` objects; each Session's
`ensure_running()` creates its own `HarnessProcess` and adapter using that Session's directory, and
`Session.shutdown()` stops the process while retaining resumable Session state. Runner shutdown stops
each Session's process separately. Therefore a Session stop currently stops only its own harness.
Multiplexing multiple Sessions through one process would decouple those lifetimes: stopping or closing
one Session must not terminate a process still serving other Sessions, and the process needs its own
start, failure, restart and no-Sessions-left policy.

`HarnessStarted` records `resumed` and a PID, not a unique process-incarnation ID. PID is useful
process-local diagnostic data, but may be reused and does not identify a spawn over time. The stored
`native_session_id` survives process restart; it is a Claude session ID or Codex thread ID, not the
Agentplane public Session ID. The public ID also keys the Runner directory, Dispatcher opening and
History record. These are already separate identity roles even though the current address usually
looks like Sandbox plus public Session ID.

Keep three axes explicit in the design:

- **Process incarnation:** one unique identity per harness process spawn, useful for routing and
  liveness; it changes on process restart. A PID alone is not a stable identity.
- **Harness persisted data/store:** data reused by a later process, such as the current per-Session
  Claude configuration directory or Codex home. The same path/data may outlive a process. Decide
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

Review the existing `Sandbox UID + Agentplane Session ID` address against a candidate opaque address
whose resolved components are `immutable Sandbox UID + persisted harness-store ID + native Session
ID within that store`. Decide whether native IDs or client-chosen public IDs are canonical, how a
public/logical ID maps to native IDs, and whether the API should expose any components or only an
opaque handle. Evaluate collisions when separate stores return the same native ID, native-child
discovery, authorization binding, unavailable/suspended lookup, and whether moving a store between
Sandboxes changes its address or requires a new grant. Do not assume that a store's path is its
identity or that PID/process identity determines data ownership.

Creation idempotency is a separate design axis from address identity. Today the caller-chosen public
Session ID is the retry key to Dispatcher and Runner; the native ID may only be known after the
harness starts. If a proposed flow lets the harness assign the canonical ID, specify how a retry
finds a creation whose response was lost, how the durable public-to-native mapping is committed, and
what happens if the process or runner fails between native creation and mapping persistence. Compare
an explicit operation/idempotency key with the eventual Session address. No schema or migration is
selected here.

## Harness capability boundary

The [test capability index](../harness_tests/README.md#behavioral-coverage) and its linked modules
record what the pinned binaries do in scripted scenarios. Codex's test demonstrates two root
threads over one app-server; Claude's current suite covers one root session and does not characterize
multiple roots in one process. The Claude multiplexing investigation remains held in the task DAG.

Neither test result decides whether one runner process should serve multiple Agentplane Sessions or
how a harness, persisted store, native Session and public Session should be addressed. Those remain
the open questions in the identity design above; neither test result authorizes runtime changes.

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
boot a real supported VM fixture: the existing k3s suite's schemaless VirtualMachine stubs only make
controller caches resolve and are not VM execution coverage. Keep the test-tier design and
implementation as a separate DAG item, gated on the reviewed execution-persistence contract and
the real VM fixture prerequisite.
