# Runner delivery boundaries and harness identity

Status: **design review only.** This note records current code paths, alternatives and questions for
operator review. It does not authorize changing runtime behavior, APIs, schemas, database ownership,
identity mappings or caller permissions. Keep the existing security checks and durable queue until a
replacement contract is reviewed.

The related decision nodes are in [section 14 of the task DAG](../plans/task_dag.md#14-delivery-boundaries-apis-and-harness-identity).
The current runner contracts remain in [Session layering](thread_layering.md), the
[Dispatcher README](../session_dispatcher/README.md), the [Gateway API](../runner_gateway/API.md),
and [native harness evidence](harness_evidence.md).

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
receives runner journals and serves retained events and folded Thread views; the app uses it for
Threads, while Notifications uses it to reconcile command receipts and confirmation. Notification's
history reads are an outcome dependency, not a delivery-routing dependency. In particular,
`RUNNER_ADMITTED` from Dispatcher does not prove that a harness consumed an input. The existing
[`NOTIFICATION_COMMAND_OUTCOME_DESIGN`](../plans/task_dag.md#notification_command_outcome_design--notice-delivery-without-reading-conversation-history)
remains an independent decision.

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

One candidate execution contract is that every open, send, wake and other Sandbox-scoped execution API
requires the Sandbox namespace, name and immutable UID. Persist that complete identity through any
queued wake or delivery, and reject it at execution if the current object is absent or has another UID.
Name-only lookup can remain a separate discovery/selection operation; it must not silently become an
execution address. UID is an identity fence, not authorization: authenticate the caller and separately
enforce ownership, grants and send permission. Review how owner identity and Session membership are
bound alongside the UID, and which boundary revalidates them.

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

| Concern                                 | Dispatcher queue                                                                                                             | Direct admission RPC                                                                                                                                              |
| --------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Runner unavailable or Sandbox suspended | Can retain accepted work, retry after readiness, and use the reviewed wake policy.                                           | No runner can acknowledge admission. The caller needs a clear reject/defer contract or another durable owner.                                                     |
| Lost response                           | Query/retry the same durable command ID and read Dispatcher state; runner admission can still be uncertain until reconciled. | Retry the same command ID against the runner's durable journal and distinguish unknown outcome from rejection.                                                    |
| Replica failure                         | Shared PostgreSQL rows, leases and generations recover queued/in-flight work.                                                | Caller retry or a new durable owner must survive the sender process and its failover. Runner-local journal only protects work after it is durably admitted there. |
| Ordering and cancellation               | Queue defines per-Session order, stop behavior and delivery state.                                                           | The API must define ordering, cancellation and whether a waiting caller can hold work without reintroducing a queue.                                              |
| Offline acceptance                      | Accepted independently of a live harness, subject to current queue and wake policy.                                          | Cannot promise offline acceptance without persistence somewhere.                                                                                                  |

A direct RPC can reduce one durable service database only if its caller/retry contract handles lost
responses, idempotency, duplicate opens, suspended/offline runners, concurrency and failover. Do not
turn a timeout into a false rejection or an admission receipt into proof of harness execution. The
runner's journal and History evidence have different retention and availability semantics. If
assessing a useful deployment without CNPG, explicitly inventory which other stateful services can be
omitted or given another persistence owner; runner-local persistence cannot accept work while the
runner is absent. Removing Dispatcher PostgreSQL alone does not remove CNPG. The current shared-CNPG databases are `app`,
`sandbox_commands`, `sandbox_service`, `actions`, `egress`, `notifications` and `session_dispatcher`;
the Indexing service also has a separate PostgreSQL dependency. See the
[service database inventory](../README.md#system-overview).

The decision should recommend one of: retain the separate queue; change its boundary/placement while
preserving its contract; or replace it with a specifically defined direct-admission contract. State
who owns idempotency, durable acceptance, unknown outcomes, retry responsibility and wake behavior
before authorizing any queue or database removal.

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

## Independent capability characterization

The current runner adapter does not select whether one native process should serve multiple
Agentplane Sessions. Treat each harness as its own investigation; neither result should wait for or
be inferred from the other.

- **Codex app-server:** the real-binary test scenario in
  [harness evidence](harness_evidence.md#codex-threads-on-one-app-server) asserts two independently
  started threads on one stdio process, concurrent turns, per-thread cancellation, selected
  per-thread options, and both threads resumed after a process restart with shared `CODEX_HOME`.
  CI remains the runtime gate. The runner adapter still owns one thread ID per process; this
  evidence characterizes Codex and does not choose whether the runner should multiplex.
- **Claude Code:** the adapter starts one stream-json process with one `--session-id`; inputs and
  interrupt controls have no Agentplane-side session selector. Native child/task events are not proof
  that one process can expose multiple independently controlled conversations. Characterize whether
  the pinned CLI supports that capability and, if so, how frames, cancellation and native state are
  scoped. Check concurrency/isolation, model and instruction options, auth/workdir/data separation,
  process failure and `--resume` behavior.

For both, the bounded result should identify supported API shape, concurrency and isolation limits,
cancellation scope, which options/auth/workdir/data are process-wide or per Session, and failure,
restart and resume semantics. Use the real pinned binaries and controlled loopback model endpoint
when code or authoritative documentation is inconclusive, following
[Native session discovery](../plans/native_session_discovery.md). Record “unknown” when evidence does
not settle a capability; do not convert absence from the current driver into an upstream limitation.

Feed each characterization and the identity alternatives into
[`RUNNER_HARNESS_OBJECTS_DESIGN`](../plans/task_dag.md#runner_harness_objects_design--expose-running-harnesses-separately-from-sessions). That decision
must state which lifecycle and capabilities belong to a process, persisted store and logical/native
Session, including whether one harness object may own multiple Sessions. No characterization,
identity decision or design node authorizes runtime changes by itself.
