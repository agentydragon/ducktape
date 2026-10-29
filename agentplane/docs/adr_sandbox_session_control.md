# Sandbox and Session control: who owns what

Status: **decided; see [Decisions](#decisions) for when each was settled.**

Scope: the responsibilities split across the services and custom resources that run a Sandbox (Pod
or VM) and deliver work to the Sessions in it. This covers the control Service, readiness and
runner health, quiesce, suspension, the Session message queue, and where each fact lives. It does
not redesign storage, the History Service, or policy.

## What has to happen

1. **Run a Sandbox.** Reconcile the `Sandbox` CR into a state volume and, while running, one Pod
   or VM. Kind (`pod` or `vm`) comes from the template.
2. **Reach its runner.** Callers address `<sandbox>-control`; the replica holding the runner's
   observation Lease answers.
3. **Say what state it is in.** Created, starting, booting, connected, ready, quiescing,
   suspended, tearing down, and why it is stuck when it is.
4. **Quiesce** (today's "seal"; one operation, names in D3). Before a Pod or VM goes away (suspend or delete), stop admitting work,
   interrupt running turns, flush every Session's journal to the ingress, and only then stop it.
5. **Deliver work to a Session whatever state its Sandbox is in.** A message to a suspended
   Sandbox's Session resumes the Sandbox and arrives once the runner is up, even if the sender is
   gone.
6. **Suspend idle Sandboxes.** Never delete on idleness.
7. **Create a Session** that does not exist yet, from a client-minted ID.
8. **Keep per-Session data out of Kubernetes**. Kubernetes holds Sandbox-level
   facts, including transitional conditions such as quiesced or not; Session-level records live
   in a service database.

## Current state

- **Sandbox Controller** (Go, `sandbox_controller/`): state PVC, Pod or VM, ServiceAccount, `PolicyBinding`, launch
  ConfigMap, finalizer, and the `{sandbox}-control` Service. It drives quiesce: it calls `Runner.Quiesce` through the
  gateway's runner proxy and records `status.quiescedIncarnation` on any quiesce and sets `Quiesced` on a deleting Sandbox only (`finalize`), beside the older sealed names. It
  reads the connection Lease's facts. `Ready` is `WorkloadReady` AND `RunnerConnected`. `QuiesceResponse.stage` has
  one reader, the controller.
- **Runner Gateway** (`runner_gateway/`): state observation and proxy of the runner's private `Runner` service. It writes per-Sandbox Leases and EndpointSlices; it does not create the `<sandbox>-control` Service (its `services` RBAC rule stays until the controller's image is pinned). Session openings and durable commands belong to the Dispatcher; the Gateway owns no Session registry or database.
- **Integration app** (`app/`): submits Sandbox desired state and operator-owned grants through the
  Kubernetes API, reads Sandbox views, and attaches the output-only VM console. It acts as the
  requesting operator; the Sandbox Controller owns status and reconciled resources.
- **Runner**: harnesses, journal, command admission, `Quiesce` (a durable lameduck keyed to the incarnation UID,
  dropped when `WatchState` adopts another). It publishes its quiesce stage and bootstrap result on `WatchState`. It starts the Sandbox's `bootstrap` at runner start, and `OpenSession`
  still waits for it. The per-command RPCs refuse work in lameduck with `ABORTED`.
- **History Service**: ingress (journals pushed by runners, acked by cursor), raw log, fold, reads; records which
  Sandbox wrote each Session. Folds `last_model_activity_at`.

The current sender path is queued: the app and Notification Service submit per-operation commands to
Session Dispatcher, which durably records them and delivers after Sandbox readiness. The app requests
Sandbox lifecycle changes through Kubernetes; the runner restores retained native state as it starts.
Live Runner reads use the UID/owner-fenced proxy through the per-Sandbox control Service. See the
[service-boundary contract](service_boundaries.md) and [Runner Gateway README](../runner_gateway/README.md)
for current callers and authorization gates.

## Where the lines could fall

The "session controller" bundles three jobs: workload and runner-aware lifecycle (wake, quiesce, suspend, delete),
idle policy, and delivery (a durable queue of messages and Session creations). Lifecycle and idle policy need the Sandbox CR,
the runner's connection facts and leader election, with no database; delivery needs a database, the sender's identity and
the gateway. So lifecycle and idle policy stay in the Sandbox Controller, where idle is declarative
(`spec.idleSuspendAfter`) and decided by the leader-elected reconciler that already drives quiesce, and a new service
holds only the queue. Rejected:

- One new service for all three: it would write desired state and run timers beside a controller that keeps only Pods,
  splitting lifecycle policy across two reconcilers of one CR.
- The queue in the gateway or in the History Service: the gateway would gain a database and the power to wake Sandboxes,
  the History Service routing and waking; both break an agreed invariant.

The controller may later split into a generic workload layer and a runner-aware layer; that moves files, not responsibilities.

## Proposed split

```mermaid
flowchart LR
    Sender[App, Notification Service, agents] -- OpenSession, SubmitInput and the other command RPCs --> K[Session Dispatcher]
    K --> KDB[(Dispatcher database)]
    K -- writes spec.running, wakeGeneration on send --> CR
    K -- watches Ready and incarnation --> CR
    K -- Runner calls through proxy --> GW[Runner Gateway]
    K -. placement when caller omits Sandbox .-> HS[History Service]
    App[App] -- creates, deletes, suspends, resumes --> CR[(Sandbox CR)]
    SC[Sandbox Controller] -- reconciles --> CR
    SC -- Quiesce through proxy --> GW
    GW -- Lease, EndpointSlice --> L[(Leases)]
    SC -- reads connectivity --> L
    Runner -- control stream --> GW
    Runner -- journal push --> HS
    Runner -- RunnerHeartbeat with facts --> GW
    SC -- idle timer from idleSince --> L
```

| Component          | Owns                                                                                                                                                          | Does not own                                                                                                                           |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| Sandbox CR         | Desired state (`spec.running`, template), current facts (`status`: incarnation, `quiescedIncarnation`, conditions)                                            | Anything per Session                                                                                                                   |
| Sandbox Controller | Pod or VM, state volume, identity, the `<sandbox>-control` Service, quiesce sequencing, idle suspension, deletion, folding every layer's fact into conditions | Runner connection, Session records, queues                                                                                             |
| Runner Gateway     | The runner connection and its authentication, the Lease and EndpointSlice, the proxy of `Runner`, per-method authorization                                    | Any Kubernetes object besides those two, Sessions, queues                                                                              |
| Runner             | Harnesses, journal, admission, quiesce mechanics, idle state                                                                                                  | Policy about when to suspend                                                                                                           |
| History Service    | Event log, fold, Session directory including the writing Sandbox                                                                                              | Routing, waking, suspending                                                                                                            |
| Session Dispatcher | The durable queue of messages and Session creations, wake-on-send, send-side authorization (who may wake), delivery states                                    | Creating or deleting Sandboxes (it reads them and patches only `spec.running` and `spec.wakeGeneration` to wake), idle policy, history |
| App                | Sandbox create, delete, suspend and resume and PolicyBinding (egress-grant) writes, through the Kubernetes API, with their checks; the VM console             | Conditions, quiesce, idle policy, delivery                                                                                             |

"Session Dispatcher" names the queue service; it is not a controller. Idle is a Sandbox-level fact (every Session in
the Sandbox idle), which is why its policy sits on the Sandbox. There is no Sandbox Service: requesters write `spec` through one
shared helper and the controller writes `status` (D4).

## Decisions

### D1. Who owns the per-Sandbox control Service

A Sandbox's connection has three objects: the Service, the Lease and the EndpointSlice. The
Service is static (name, port); the slice and Lease are the dynamic connection fact.

**Decided: the controller creates the Service; the gateway writes the Lease and slice.** The Service is part of the
Sandbox's identity, like its ServiceAccount, and exists from creation, so before a runner connects the caller sees no
endpoints rather than a DNS failure; only the connection is the gateway's to assert, and its RBAC shrinks. The controller
and gateway agree on name and port through one shared constant. Rejected: no per-Sandbox Service, with any gateway
replica forwarding to the holder: it adds a replica-to-replica hop and trust and reverses the operator's 2026-10-12 choice
of a dialed-hostname Sandbox name. Revisit it if the per-connection writes ever become a load problem.

### D2. How readiness and runner health reach the Sandbox's conditions

Facts have different truth sources, so each gets its own condition and `Ready` is their AND:

| Condition         | Source of truth                                                               | Meaning                                                                                                                                 |
| ----------------- | ----------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `Running`         | controller (Pod or VMI)                                                       | an incarnation exists and runs                                                                                                          |
| `WorkloadReady`   | controller (Pod Ready)                                                        | the incarnation's Pod is Ready                                                                                                          |
| `RunnerConnected` | gateway (connection Lease)                                                    | a runner of this incarnation holds a live connection                                                                                    |
| `Bootstrapped`    | runner (bootstrap result, republished at each connect)                        | the Sandbox's bootstrap ran to success; Sandbox-lifetime; False with reason `BootstrapFailed` if it failed or was interrupted           |
| `Ready`           | controller                                                                    | all of the above for the current incarnation; reason `BootstrapFailed` if bootstrap failed, `SuspendPending` while a suspend waits (D3) |
| `Quiesced`        | controller, from the runner's quiesce stage published on the connection Lease | False with reason `Draining`, then `Flushing`; True once every final cursor is acknowledged                                             |

The Sandbox's bootstrap runs once, at runner start and before any Session opens, and
`Ready` includes `Bootstrapped`. Its outcome lives on the state volume, so each incarnation's runner republishes
it when it connects; `Bootstrapped` is the one condition that is a Sandbox-lifetime fact rather than an
incarnation's. A failed or interrupted bootstrap never runs again (the runner raises `BootstrapFailedError`), so
`Ready` reads False with the terminal reason `BootstrapFailed`: the controller does not retry it and the
Sandbox is replaced, not repaired. The per-Session step is called "setup" and is not part of `Ready`.

Two rules make this safe. Conditions other than `Bootstrapped` are keyed to the incarnation UID, so a connection or boot
fact from incarnation N is never read as incarnation N+1's. And a condition is written by exactly
one actor: the controller writes them all; every other layer publishes a fact the controller reads.

**Decided: the Lease.** The gateway already writes it, Sandbox-owned, with the pod UID and boot ID. `RunnerConnected` is
its liveness (a gateway restart leaves a stale-but-honest record: `renewTime` plus duration; the controller requeues on
expiry), and runner-reported facts (`Bootstrapped`, `idleSince`, the quiesce stage of D3) ride its annotations. Rejected:
gRPC health from the gateway (the controller would hold a watch per Sandbox against a replica-sharded service, and a
gateway outage would read as every runner down; if that node is built its output should still land as a fact the
controller folds), and the gateway writing the Sandbox's status (two writers on one status, against "layers that act on
CRs do not edit them"). A gateway rollout drops every connection at once; the controller must
treat the reconnect window as connectivity lost, not as a Sandbox failure, and must not replace or
restart anything because of it (no automatic spec replacement).

**Runner facts and Lease writes.** `Runner.WatchState` publishes complete snapshots on fact
changes and periodically. The Gateway records the facts with authenticated liveness on the Lease;
readers use them only while the Lease names the current incarnation and remains live. Claiming a
new observation clears previous facts until the runner reports again. The controller filters Lease
watch events on facts and ownership changes rather than reconciling every renewal. Revisit write
cadence only if measured etcd pressure requires it.

## D3. Quiescing is one operation; names say which stage a Sandbox is in

**Quiescing is one operation on the whole runner, not on its Sessions**. It
puts the runner in a durable lameduck state, in which it accepts only the operations that drain it,
and drains it. The state survives a runner restart within the incarnation and ends with the incarnation
(below). What stays per
Session is only the bookkeeping the drain finishes: each journal's final cursor and whether the
ingress has acknowledged it.

| Once quiescing has begun, the runner                                                 |
| ------------------------------------------------------------------------------------ |
| refuses `OpenSession` and any command that starts or continues work                  |
| accepts guarded interrupts (draining work), and repeated `Quiesce`                   |
| answers a redelivered `command_id` it already admitted, rather than refusing it      |
| answers reads (`ListSessions`), which today wait for an in-progress `Seal`           |
| interrupts active turns, stops harnesses, finalizes journals, pushes through cursors |

**Lameduck belongs to an incarnation.** A runner restart inside an incarnation (a crashed runner container, a guest
reboot in the same VMI) keeps the runner quiesced; a new incarnation on the same state volume starts unquiesced. The
runner records the state under configured `state_dir`, which is the state volume and therefore outlives the incarnation, so the
marker names the incarnation it was entered in:

- **Incarnation identity.** The Gateway resolves the Sandbox's current Pod or virt-launcher
  incarnation and authenticates the runner certificate before observing `Runner.WatchState`.
  Its request carries that incarnation's UID. `runner_boot_id` cannot serve: it changes on every
  runner process start, restarts included.
- **Keying.** Entering lameduck writes the marker with the adopted incarnation UID. The runner
  refuses normal calls before `WatchState` adopts an incarnation. Same UID preserves the marker
  and keeps the runner quiesced across restart; a different UID drops the marker before accepting
  calls, so the new incarnation starts unquiesced.
- **Journals.** Each Session's journal sealed at its final cursor in the earlier incarnation. The
  new incarnation continues it after that cursor with an entry recording that it resumed;
  [Reopening a sealed journal](#reopening-a-sealed-journal) decides how.
- **Outside the runner.** Runner facts on the Lease are valid only while its `pod-uid` names the incarnation:
  the Gateway's observation claim (`runner_gateway/gateway/records.go`) rebuilds the Lease with only `pod-uid` and `runner-boot-id` on every
  accepted connection, and the controller's shared Lease facts decoder (`sandboxes/lease/facts.go`) accepts facts only while `pod-uid`
  equals the incarnation, so a stale `Quiesced` is never read for the new one. The controller's `quiescedIncarnation` is compared with
  the current `status.incarnation`; after a resume they differ, which is what makes the new
  incarnation unquiesced there as well.

**Names.** Today one word, "sealed", covers three different facts: the runner refuses new work, its
journals are final, and the ingress has acknowledged them. A `Sealed` condition that is True while
the runner is only refusing new work is wrong. The names:

| Retired name               | Name                                                                    | Meaning                                                                   |
| -------------------------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| RPC `Runner.Seal`          | `Runner.Quiesce`                                                        | The one operation; idempotent, so a repeat is safe                        |
| (no name)                  | reason `Draining`                                                       | Refusing new work; interrupting turns, stopping harnesses                 |
| (no name)                  | reason `Flushing`                                                       | Harnesses stopped, journals final; waiting for the ingress to acknowledge |
| `Sealed`, `SessionsSealed` | condition `Quiesced`, True only when every final cursor is acknowledged | Safe to remove the Pod or VM                                              |
| `status.sealedIncarnation` | `status.quiescedIncarnation`                                            | UID of the last incarnation that reached `Quiesced`                       |
| Sandbox phases `Sealing`   | `Quiescing`                                                             | Draining or Flushing                                                      |
| `Ready` while refusing     | `Ready` False, reason `Draining`                                        | Callers and the Dispatcher read "not accepting work" from `Ready`         |

`Quiesced` is False with reason `Draining`, then `Flushing`, until it is True; `NeverRan` and
`NotQuiesced` cover a Sandbox whose runner never connected and one stopped without quiescing. While incarnation N+1
runs, `Quiesced` is False with reason `NotQuiesced`: `quiescedIncarnation` still names N, which is not the current
incarnation, so the condition reads nothing from it. The
journal keeps the word "sealed": a journal is sealed at its final cursor, which is one step of
`Flushing` and per Session, so it never names a Sandbox condition. Per-Session cursors never appear
in Kubernetes. The remaining sections use the new names.

Quiesce refuses at entry and interrupts before it stops anything, inside the one operation.

**`Quiesce` is not `Seal`.** `Seal` blocks until the journals are sealed and returns each Session's final
cursor and `flushed`. `Quiesce` returns at lameduck entry and its response drops the cursors: progress goes on the
Lease and per-Session data stays out of Kubernetes. The runner already serves `Quiesce` as a straight alias of
`Seal`, so the controller already calls it and reads an empty `Unflushed` as "flushed" before it deletes the Pod.
The hazard is therefore not the rename but changing what `Quiesce` returns under the same name: a non-blocking
runner met by a reader of the old shape yields `Unflushed == []`, and the Pod is deleted before the ingress
acknowledges anything. One reader has that shape, the controller.

Rollout, reader before writer:

1. `QuiesceResponse` gains a `stage` field, and the controller reads both shapes (landed).
2. The gateway's deletion seal is retired, so the controller is the only reader (landed).
3. Only then does the runner's `Quiesce` stop blocking. The runner is the writer: the runner image and the VM
   guest's runner disk (its own image, published from the same commit) move only after the controller is pinned.

The legacy shape carries a removal node.

**Setup and bootstrap meet quiesce**. Orderly suspension, deletion quiesce and runner shutdown let
running Sandbox bootstrap and Session setup finish naturally. They must not deliberately interrupt
non-transactional initialization. Unexpected process loss can still leave a script interrupted;
that outcome is terminal and the script is never silently rerun. Running setup and bootstrap also
count as busy for idle suspension.

How the wait works. `RunnerFacts` carries a Sandbox-level fact for the setups running.
While an operator suspend waits for it and for the bootstrap, the controller holds `Ready` False with reason
`SuspendPending`. The Dispatcher already waits on `Ready`, so it stops delivering, new `OpenSession` included, and
the wait is bounded by the setups already running; the runner needs no mode of its own. That bound holds only if the
Dispatcher is the only caller of the runner's `OpenSession` (D4). A
setup that never ends keeps orderly suspension or deletion draining until it completes. An automatic
suspend needs none of it: a running setup is busy, so the conditional `Quiesce` refuses. The runner
also waits for bootstrap when there are no Session journals to seal.

### Reopening a sealed journal

**Decided (operator, 2026-10-14): choice 1.** A resumed Sandbox cannot run any Session today, and idle suspension, or any suspend not
followed by a delete, waits on this.

**What "sealed" is after a quiesce.** One fact in several places, only the last keyed to an incarnation:

- The runner journal's `Seal` row (`runner/journal/journal.go`): `open` refuses entries after it, append rejects
  writes after sealing, and Session sealing prevents further work (`runner/session/session.go`).
- The runner's per-Session acknowledgement file records `sealed`; `IngressPusher.track` skips a Session whose
  acknowledgement is sealed, and a push ends on a sealed first acknowledgement (`runner/transport.go`).
- The History Service stores the seal in the Session's feed state. `RecordFeedState` keeps it sticky ("conflicting
  seals"), and `Push` returns right after a sealed first acknowledgement, so it takes no entry from a sealed Session
  (`history_service/ingress/ingress.go`, `storage/store.go`). `Append` itself does not check it.
- The lameduck marker is the exception: it carries the incarnation UID and is dropped on a different one.

**What a resume needs.** The new incarnation mounts the same state volume, so the journal, its `Seal` row at cursor
`N`, the sealed acknowledgement and the History Service's seal at `N` are all still there. Its runner must accept work
(lameduck is already dropped), append `N+1` onward, and push to an ingress that accepts them. `Attached` carries no
incarnation, so the ingress cannot tell a continuation from a runner that wrongly kept writing past its seal.

**Choices.**

1. **A seal belongs to an incarnation.** The runner's seal, its acknowledgement file and the feed-state seal each
   record the incarnation UID that sealed. A runner under a different UID treats the seal as ended, reopens the journal
   and appends an entry recording the resume. `Attached` gains the incarnation UID; the ingress accepts a push whose UID differs from the seal's, when `last_cursor` equals the seal's
   cursor, and replaces the seal. Keeps a durable end-of-incarnation fact and the guarantee that nothing is written past
   it. Costs a wire field, three runner places and two History Service places, and the History Service must accept the
   continuation before any runner sends it (image before config).
2. **The journal is never sealed; finality is the lameduck marker.** Quiesce stops the harnesses and reports
   flushed once the ingress acknowledged the last cursor; the lameduck marker, already keyed to the incarnation, makes
   the Session refuse writes. Removes the `Seal` row, the `Sealed` frame and the feed-state seal. Loses the hard
   guarantee: the journal could append after the flush check and the Pod could go with unacknowledged entries, so
   every writer must honor the marker. The History Service learns an incarnation ended only from the next UID. Seals
   already on disk still need option 1's reopen path once.
3. **A new journal file per incarnation.** Rejected: cursors restart, so History Service cursors and every reader's
   position would need an incarnation component.

**Why 1.** Its seal ends the incarnation, not the Session; it keeps
the invariant that makes `Quiesced` safe, and option 2 would need option 1's path for existing seals anyway.

**Transitions the sequence must cover.**

- _Resume during `Quiescing`._ A Sandbox that is quiescing does not accept a resume command (operator,
  2026-10-13): the wake helper refuses `spec.running = true` with a retryable code until the Sandbox is
  `Suspended`, and the Dispatcher retries then. While it is quiescing `Ready` is False with reason `Draining`.
  The Dispatcher keys each attempt to the incarnation UID and does not treat incarnation N's `Draining` refusal
  as a failure of N+1. The invariant lives in the controller, not the helper: once an incarnation is in lameduck
  (the Lease stage for the current incarnation UID, or the controller has issued `Quiesce`), the controller drives
  it to `Quiesced`, removes it, and only then reads `spec.running` for N+1, whatever the spec says meanwhile. The
  helper's refusal is an early, advisory error, since a check-then-write across two processes cannot be atomic
  with the runner entering lameduck.
- _Delete before the runner was ever up._ Delete during `Creating`, `Starting`, `Booting` or `Resuming` skips
  quiesce only if the incarnation never ran (`NeverRan`). A runner that connected and then lost its connection
  (gateway outage) looks the same on the current Lease, so `NeverRan` reads the `pod-uid` annotation the gateway
  writes on the Sandbox-owned Lease at each accepted connection: the Pod UID of the runner that last connected, or
  empty if none ever did. No separate "set once" fact is needed.
- _Two incarnations on one volume._ `WatchState` adoption of a different UID drops the earlier lameduck marker, which is
  safe only if incarnation N is gone before N+1's runner writes. That holds by structure: the Pod is named after the
  Sandbox, so N+1 cannot exist while N does, and a halted VM's VMI blocks the next start. It is an invariant; giving
  incarnations unique names would need the wait added back.
- _Gateway flap mid-quiesce._ A rollout flaps `RunnerConnected` while a drain is under way. The controller
  re-issues `Quiesce` and never resets the drain; this is idempotent only because the runner's lameduck holds.
- _Background work._ Background processes and terminals do not count as busy for idle (operator, 2026-10-14), so a suspend, delete
  or node loss kills them. Quiesce stops them with the harnesses, with no grace beyond the interrupt deadline.
  The deadline is a Sandbox spec field the controller reads; its default is chosen by the node that builds it.

**Commands that meet lameduck.** Three states differ. A command admitted but not yet dispatched stays pending in
the journal (#10341); the next incarnation does not dispatch it, since the conversation has moved on, and the
Dispatcher fails it with the reason `ended_by_teardown`. A command dispatched and in flight ends its turn as
`ended_by_teardown`. A refused send is retried after Sandbox resume. Dispatcher does not queue interrupts; it returns
immediate delivery/no-op evidence. Runner admission and outcome remain durable.

**Order per Session.** Inputs and model changes deliver in FIFO order. `InterruptTurn` bypasses the
Dispatcher queue, names the exact active turn and never wakes a Sandbox. A stale target is rejected
with `FAILED_PRECONDITION` before runner admission. An admitted interrupt cannot affect a successor.

Sequence for suspend and delete:

```mermaid
sequenceDiagram
    participant C as Sandbox Controller
    participant R as Runner (through gateway proxy)
    participant I as Ingress (History Service)
    participant L as Connection Lease
    C->>R: Quiesce (one call, returns once lameduck is entered)
    R->>R: enter lameduck (durable): refuse all but draining operations [Draining]
    R-->>L: stage = Draining (Lease annotation, through the gateway)
    R->>R: interrupt active turns (InterruptTurn), wait up to a deadline
    R->>R: stop harnesses, seal journals
    R-->>L: stage = Flushing
    R->>I: push to the final cursor
    I-->>R: ack
    R-->>L: stage = Quiesced
    L-->>C: watch event
    C->>C: Quiesced, record quiescedIncarnation, then delete Pod or VM
```

Interruption is part of `Quiesce` rather than a second RPC: the runner already owns turn state. An
automatic suspend is conditional: `Quiesce` carries `if_idle_since`, the runner's own
`idleSince` token as the controller last observed it. The runner compares it under the lock that enters lameduck
and refuses when it differs or the runner is busy; the controller then resets its idle timer. Busy includes a delivery under way, marked at RPC entry before the first `await`: a `SubmitInput` between RPC entry and the journal write is already in the runner's set of admissions in flight, and the compare goes directly before lameduck entry with no `await` between. The two orderings are then exhaustive: a delivery precedes the compare and finds the runner busy, or follows lameduck entry and is refused. The token is checked
only on entry: a repeat `Quiesce` that finds the runner already in lameduck for this incarnation succeeds
whatever its token, because the controller re-issues `Quiesce` after a lost response. The token is opaque to the
controller, and the runner compares it at the precision the Lease carries: the gateway writes `idle-since` through a datetime (`Timestamp.ToDatetime(...).isoformat()`), so at most microseconds, and a runner that published finer values would never match its own echoed token. The controller measures elapsed time from its first observation of each distinct token, so clock skew and a
restarted runner cannot suspend early; after a controller restart it starts the timer on its first observation, and
suspending up to one `idleSuspendAfter` later is acceptable. Operator suspend and delete are unconditional and carry
no token. A message that arrives after the controller decided but before
lameduck therefore makes the runner busy, and the suspend is refused instead of draining a Sandbox with new work.

An unconditional quiesce needs a deadline after which a turn that ignores the interrupt is stopped anyway, and a
defined history entry showing a turn ended by teardown.

**The controller is told, not polling.** Until a runner publishes its stage the controller repeats
`Quiesce` every two seconds; it already reads the stage when it is on the Lease. The design is that `Quiesce` is called once and returns when the runner
has entered lameduck. From then on the runner publishes its stage (`Draining`, `Flushing`,
`Quiesced`) as a connection-Lease annotation, written on each transition by the gateway replica
holding its connection (D2's transport), and the controller reacts to the Lease watch event. The
runner, not the controller, knows when the ingress acknowledges the last cursor, so the signal
originates where the fact is, and the History Service is never a dependency of the controller.

Two things keep this level-triggered rather than reliant on one event: the runner republishes its
stage whenever it reconnects (a gateway rollout or a dropped stream loses nothing), and the
controller re-issues the idempotent `Quiesce` when it finds a Sandbox that should be quiescing
whose Lease shows no stage. A controller restart between its `Quiesce` call and the stage reaching the Lease
depends on the cause. For an operator suspend or delete the controller re-evaluates and re-issues `Quiesce`, which
is safe because a repeat into a runner already in lameduck succeeds. For an idle suspend a restarted controller
starts its idle timer fresh and has no token to send, so it issues nothing. The runner is already in lameduck and
the stage reaches the Lease (written at once, retried from the gateway's copy), but nothing records that the
controller meant to suspend: a wake may have raced it. The controller therefore seals that incarnation and, as the
Sandbox is to run, replaces it. The same holds when a wake bumps `spec.wakeGeneration` during the idle `Quiesce`,
which fails the status write that records the suspension on its stale resource version. It costs one incarnation and
loses no wake. The gap is the gateway's immediate Lease write. A runner that never progresses is caught by a deadline the controller
requeues for, not by periodic checks, and ends as a stuck `Quiescing` Sandbox whose finalizer an
operator removes by hand. Rejected: a server-streaming `Quiesce` held open by the controller (it
is lost on every gateway rollout and ties up a connection per quiescing Sandbox for no fact the
Lease does not already carry), and the History Service notifying the controller (a new
dependency the other path does not need).

**Refusal codes**. The runner's gRPC code says what the Dispatcher does; the gateway proxy
passes runner codes through. The first row is already true on `devel`, where the runner returns `ABORTED` for the lameduck refusal; only the conditional-`Quiesce` row is new.

| Case                                                             | Code                  | Dispatcher                                                                                                                                |
| ---------------------------------------------------------------- | --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| Runner in lameduck refuses work                                  | `ABORTED`             | back to `QUEUED_IN_DISPATCHER`, retry after resume                                                                                        |
| Conditional `Quiesce` refused (`if_idle_since` differs, or busy) | `ABORTED`             | none: the controller resets its idle timer                                                                                                |
| Journal sealed, setup failed, harness stopped, `BootstrapFailed` | `FAILED_PRECONDITION` | terminal: `FAILED` with the reason                                                                                                        |
| Redelivered `command_id` already admitted                        | success               | `RUNNER_ADMITTED`, or later, from the runner's answer                                                                                     |
| Runner `OpenSession` returns `ALREADY_EXISTS`                    | `ALREADY_EXISTS`      | terminal: `FAILED`; the runner returns the existing attachment for exact retries, so this status means a requested spec or setup conflict |

**Ambiguous delivery resolves on redelivery.** The runner's journal lives on the state volume and answers a
redelivered `command_id` with its existing admission, even after the harness stopped or the journal was sealed. A
call whose reply was lost across an incarnation boundary is therefore resolved by redelivering to the next
incarnation. `UNKNOWN` remains only for a Sandbox that was deleted. For `OpenSession` this holds
within one runner build only (D4).

### D4. The queue service: required or optional, one door or two, separate or merged

The queue service does one job: **delivery.** Accept a message durably, wake the
Sandbox, deliver once the runner is Ready, survive the sender leaving. It needs a database, the
Sandbox's name, the gateway and the Kubernetes API. Idle policy is the controller's (below).

**Required or optional.** Decided (operator, 2026-10-14): _required for senders, not for the platform._ Production
senders use the Dispatcher's command RPCs, so ordinary message delivery has one set of semantics:
accepted, then delivered, or failed with a reason. The runner proxy at the Gateway remains the
network path to the runner. Dispatcher owns ordinary work submissions (`OpenSession`, `SubmitInput`,
`ChangeModel`, `ChangeReasoningEffort`); the runner owns native preparation after admission. The
controller calls `Quiesce`; guarded `InterruptTurn` and reads remain separately authorized.

| `Runner` method                                       | Caller/path                                 |
| ----------------------------------------------------- | ------------------------------------------- |
| `SubmitInput`, `ChangeModel`, `ChangeReasoningEffort` | Dispatcher ordinary command delivery        |
| `InterruptTurn`                                       | Immediate guarded delivery; no Sandbox wake |
| `OpenSession`                                         | Dispatcher durable Session reservation      |
| `Quiesce`                                             | Sandbox Controller only                     |
| `ListSessions`, `GetStatus`                           | Authorized direct reads; no wake            |

### D5. Where each fact lives

| Fact                                                                                                                                                      | Home                                           |
| --------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------- |
| Desired running, template, incarnations, conditions, `quiescedIncarnation`, the controller's last `quiesceAttempt`, the rendered spec of each incarnation | Sandbox CR (Kubernetes)                        |
| Runner connected, boot state, `idleSince`, quiesce stage                                                                                                  | Lease annotations (Kubernetes, Sandbox-level)  |
| Session events, directory, writing Sandbox, summaries                                                                                                     | History Service database                       |
| Queued messages, their delivery state, queued Session creations                                                                                           | Dispatcher database                            |
| Session journal and cursors                                                                                                                               | Runner volume, mirrored by the ingress         |
| Per-Session final cursors and flush state                                                                                                                 | the runner journal and the ingress, never a CR |

Conditions and annotations carry Sandbox-level, transitional facts only. A per-Session list in
Kubernetes is the line not to cross.

## Sandbox lifecycle

Phase is derived, never stored, and the six wire phases of `SandboxPhase` stay: no wire change. The finer
distinctions are reasons on the `Running`, `Ready` and `Quiesced` conditions.

```mermaid
stateDiagram-v2
    [*] --> CREATING
    CREATING --> RUNNING: incarnation named
    RUNNING --> SUSPENDING: to stop (operator suspend or idle)
    SUSPENDING --> QUIESCING: Quiesce issued
    QUIESCING --> SUSPENDING: Quiesced, workload removed next
    SUSPENDING --> SUSPENDED: incarnation gone
    SUSPENDED --> CREATING: resume
    CREATING --> TEARING_DOWN: delete (no incarnation)
    RUNNING --> TEARING_DOWN: delete (NeverRan)
    RUNNING --> QUIESCING: delete (runner connected before)
    SUSPENDING --> QUIESCING: delete
    QUIESCING --> TEARING_DOWN: Quiesced, delete requested
    SUSPENDED --> TEARING_DOWN: delete (already quiesced)
    TEARING_DOWN --> [*]
```

| Name used in this document    | Wire phase                                                                           | Reasons                                                                                        |
| ----------------------------- | ------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------- |
| Creating                      | `CREATING` until the Pod or VM exists                                                | `Running` False, `Starting`                                                                    |
| Starting                      | `RUNNING` once the Pod or VM exists (`status.incarnation` is set even while Pending) | `Running` False, `Starting`                                                                    |
| Booting                       | `RUNNING`                                                                            | `Ready` False while the runner connects and the bootstrap runs                                 |
| Ready                         | `RUNNING`                                                                            | `Ready` True                                                                                   |
| Degraded                      | `RUNNING`                                                                            | `Ready` False, `Degraded` (runner crash loop; nothing replaced)                                |
| BootstrapFailed               | `RUNNING`                                                                            | `Ready` False, `BootstrapFailed` (terminal)                                                    |
| SuspendPending                | `SUSPENDING`                                                                         | `Ready` False, `SuspendPending`                                                                |
| Quiescing                     | `QUIESCING`                                                                          | `Quiesced` False, `Draining` then `Flushing`                                                   |
| Quiesced, workload going away | `SUSPENDING`                                                                         | `Quiesced` True                                                                                |
| Suspended                     | `SUSPENDED`                                                                          |                                                                                                |
| Resuming                      | `CREATING` until the Pod or VM exists, then `RUNNING`                                | `Running` False, `Starting`; an unschedulable or node-down resume is `RUNNING` with `Starting` |
| TearingDown                   | `TEARING_DOWN`                                                                       |                                                                                                |

`Starting`, `Stopped`, `Stopping`, `PodEnded`, `VMEnded`, `Sealing` and the conflict reasons exist today
(`sandbox_controller/SPEC.md` § Status); this design adds `Booting`, `Degraded`, `Draining`, `Flushing`, `NeverRan`,
`NotQuiesced` and `SuspendPending`. `_phase` keys `QUIESCING` on the `Running` reasons `Quiescing` and `Sealing`, not on
`Quiesced`.

`QUIESCING` covers a suspend drain as well as a deletion drain; the enum's comment claiming deletion only is wrong. A
Sandbox in `QUIESCING` or `TEARING_DOWN` takes no new Sessions or commands and cannot be suspended or resumed. A
delete from a state with a runner that connected in this incarnation quiesces first; `NeverRan` skips it. Removing the
finalizer by hand is the only break-glass out of a stuck `QUIESCING` or `TEARING_DOWN`. The phase derives from the
controller's published effective mode (below), not from `spec.running` alone, or an idle drain would read `RUNNING`
and an idle-suspended Sandbox `CREATING`. The wake helper's refusal to resume applies to `QUIESCING`, not to any one edge.

**Sandbox bootstrap.** The bootstrap is the launch file's script, run once per Sandbox, not per incarnation
The runner starts it when it starts, before any Session opens (`Runner.startup`; a
Sandbox with no bootstrap succeeds trivially). Its outcome and log live on the state volume, so a later
incarnation's runner reports the recorded outcome and does not rerun it.

```mermaid
stateDiagram-v2
    [*] --> NotRun
    NotRun --> Running: runner starts
    NotRun --> Succeeded: Sandbox has no bootstrap
    Running --> Succeeded: script exits 0
    Running --> Failed: script exits non-zero
    Running --> Interrupted: runner process dies mid-script
    Succeeded --> [*]
    Failed --> BootstrapFailed
    Interrupted --> BootstrapFailed
    BootstrapFailed --> [*]
```

- `Bootstrapped` is True from `Succeeded`, False with reason `BootstrapFailed` from `Failed` or `Interrupted`, and
  False with the progress reason while `NotRun` or `Running`. `Ready` includes `Bootstrapped`.
- `BootstrapFailed` is terminal: never retried, and the Sandbox is replaced rather than repaired. The runner
  refuses every `OpenSession` that would create a Session with `FAILED_PRECONDITION`.
- While `Running`, an `OpenSession` waits for the script today. The Dispatcher waits for `Ready`, so it does not
  reach the runner before then.
- Suspend is refused until the bootstrap is `Succeeded` or `BootstrapFailed`, so a suspend never interrupts it,
  and it also waits for every Session's setup to finish. Delete is always allowed and interrupts both.

**How idle suspension expresses itself.** Desired state stays in `spec.running`, and layers acting on CRs do not
edit them, so the controller's idle timer does not write the spec. It writes a status fact,
`status.suspendedBy: Idle`, recording the `spec.wakeGeneration` it suspended at; a writer (the Dispatcher or the app) wakes by
bumping `spec.wakeGeneration`, and the controller runs the Sandbox when `spec.running` holds and the generation
differs from the recorded one. "Suspended" then has two distinguishable causes, which the controller publishes in
`status.suspendedBy` together with the effective mode: `Requested` (`spec.running` false, whoever cleared it) and `Idle`.
A requester that clears `spec.running` of an idle-suspended Sandbox takes the reason over. An operator hold
(`spec.wakeOnSend: false`) is distinguishable from an idle suspend.

- Suspend and delete both run the D3 sequence; a suspended, already-quiesced Sandbox deletes without
  waking (`quiescedIncarnation`).
- A wedged quiesce is cleared by removing the finalizer by hand, which discards unflushed journals and the CR-owned state volume.
- Spec update is suspend, then resume, which re-derives the spec from the current template. The
  rendered spec of each incarnation is stored on the Sandbox for diffing.

**What `suspendedBy` costs (operator, 2026-10-14: the shape stays).** After it, `spec.running = true` no longer means
"meant to run". Every decision about effective state reads the controller's reported mode: the controller's reconcile,
converge and quiesce paths and `vm.go`; Python's `Sandbox.operating_mode`; the shared phase projection in
`kubernetes_views.py`; deletion; readiness; and everything fed by `SandboxPhase`. `spec.running` remains desired state
for suspend and resume requests; it is not an effective-state fallback. An unreconciled Sandbox has no reported mode,
gets `SANDBOX_PHASE_UNSPECIFIED`, and cannot be treated as ready, suspended, or safe to delete. Four points follow.

1. _One derivation site._ The controller publishes the effective mode in status once; Go, Python and the app read
   that, not a re-derived `spec.running && !(suspendedBy == Idle && generation == recorded)` in each.
2. _One wake verb._ The helper's resume sets `running = true` and bumps `wakeGeneration` for both causes, so the
   Dispatcher never has to know which kind of suspension it is waking.
3. _Which generation is recorded._ The one the controller observed when it began quiescing, not when it finished, so
   a bump during the drain triggers N+1 after `Quiesced`. The helper refuses the bump while quiescing, like `running`.
4. _Delete._ The app's delete of an idle-suspended Sandbox is allowed.

**Implementation, 2026-10-15.** Python and the API expose `status.operatingMode` as nullable until the controller's
first report. The phase projection preserves this as `Unspecified`; Runner Gateway refuses to route it, readiness stays
false, deletion returns conflict, and send-triggered wake stays queued without recording a wake request or deadline.

Message to a suspended Session:

```mermaid
sequenceDiagram
    participant S as Sender
    participant K as Dispatcher
    participant CR as Sandbox CR
    participant C as Controller
    participant R as Runner
    S->>K: SubmitInput(command_id, session, input)
    K->>K: persist, ack sender (accepted)
    K->>CR: patch spec.running and wakeGeneration (an idle-suspended Sandbox needs the bump)
    CR-->>C: change seen on the watch
    C->>C: new incarnation, Ready for this incarnation
    K->>R: SubmitInput (same command_id)
    R->>R: admit, prepare harness, reconcile and deliver
    R-->>K: admission
    K->>K: mark delivered
```

## Complexities not yet named

- **Wake cost and abuse.** Resume starts a Pod or VM and every harness. Notifications and agent
  messages can wake Sandboxes in bulk; rate limits, per-sender quotas and per-Session lazy
  harness preparation (only Sessions with pending work) are needed. A resume storm also hits scheduling
  and the state volume's node.
- **Resume can be impossible.** With node-local volumes the Sandbox can only resume where its PVC
  lives; the node may be down or full. Queued commands have no TTL (decided in D4); they wait in
  `QUEUED_IN_DISPATCHER` with the reason the Sandbox is not `Ready`, bounded in count by the per-Session cap. Poison
  commands, where a delivery keeps failing, get no special handling (decided, operator, 2026-10-14): the dispatch
  retries, shows its reason, and the sender cancels it. A refusal with a terminal code (`FAILED_PRECONDITION`) already
  fails the dispatch at once. To do: revisit once a poison command is seen in practice; an attempt cap on
  `DELIVERING_TO_RUNNER` returning to `QUEUED_IN_DISPATCHER`, or holding the Session's queue, are the candidates.
  One failing command at the head of a Session's queue blocks the rest until it is cancelled.
- **VM RAM suspend.** If a VM keeps harness processes alive across suspend, quiescing means something different
  (flush the journal, keep the harness). Settle this before RAM suspend assumes the Pod sequence.
- **Credentials across suspension.** Egress grants, PolicyBindings and projected tokens must still
  be valid when a Sandbox resumes hours later; a resume that comes up with expired grants looks
  like a broken Sandbox.
- **Multiple Sessions per Sandbox.** Idle is the conjunction over all of them, and waking Sandbox
  for one Session should not start the others' harnesses.
- **Session ID collision.** The client-minted ID must be unique across Sandboxes; the Dispatcher's `OpenSession`
  dispatch row covers the window before the History Service has seen the Session, and the History Service's
  first-writer binding covers the rest.

## Decisions

| Decision                                                                                                         | Status  | Date (UTC) | Where          |
| ---------------------------------------------------------------------------------------------------------------- | ------- | ---------- | -------------- |
| The Dispatcher is the required door for senders; system callers keep `Quiesce`, interrupts, stops and reads      | Decided | 2026-10-14 | D4             |
| The Dispatcher is its own service, not part of the gateway or the controller                                     | Decided | 2026-10-14 | D4             |
| Idle policy lives in the Sandbox Controller, declarative, from runner-reported idleness                          | Decided | 2026-10-14 | D4             |
| The controller creates the `<sandbox>-control` Service                                                           | Decided | 2026-10-13 | D1             |
| Runner facts reach the controller on the connection Lease's annotations                                          | Decided | 2026-10-13 | D2             |
| Facts ride a runner-only `RunnerHeartbeat`, sent on change and on connect                                        | Decided | 2026-10-14 | D2             |
| Quiescing is one operation on the whole runner and interrupts inside it; the renames of D3                       | Decided | 2026-10-13 | D3             |
| Resume is refused while a Sandbox is quiescing                                                                   | Decided | 2026-10-13 | D3             |
| Automatic suspend is a conditional `Quiesce` carrying `if_idle_since`; no ingress-health gate                    | Decided | 2026-10-14 | D3             |
| Suspend waits for the bootstrap and every Session's setup; delete does not                                       | Decided | 2026-10-13 | D3             |
| A waiting suspend holds `Ready` False with `SuspendPending`                                                      | Decided | 2026-10-14 | D3             |
| Refusal codes: `ABORTED` for lameduck and a failed conditional `Quiesce`, `FAILED_PRECONDITION` terminal         | Decided | 2026-10-14 | D3             |
| `InterruptTurn` bypasses the queue and retains its exact-turn guard                                              | Decided | 2026-10-14 | D3             |
| Delivery state names (`QUEUED_IN_DISPATCHER` and the rest)                                                       | Decided | 2026-10-14 | D4             |
| `ChangeModel` and `ChangeReasoningEffort` start a stopped harness                                                | Decided | 2026-10-14 | D4             |
| The Dispatcher watches the Sandbox CR                                                                            | Decided | 2026-10-14 | D4             |
| `spec.wakeOnSend: false` holds a Sandbox suspended against wake-on-send and gates the feature                    | Decided | 2026-10-13 | D4             |
| Per-Session idle for the UI is a folded History Service field; the Sandbox aggregate rides the Lease             | Decided | 2026-10-13 | D4             |
| Idle suspension is `status.suspendedBy: Idle` plus a `spec.wakeGeneration` bump to wake                          | Decided | 2026-10-14 | Lifecycle      |
| Every suspension records why in `status.suspendedBy` (`Idle` or `Requested`), and the web app shows it           | Decided | 2026-10-17 | Lifecycle      |
| The six wire phases stay; finer states are reasons on conditions                                                 | Decided | 2026-10-14 | Lifecycle      |
| There is no Sandbox Service: the app creates, deletes, suspends and resumes Sandboxes through the Kubernetes API | Decided | 2026-10-14 | Proposed split |
| The VM serial console moves into the app                                                                         | Decided | 2026-10-14 | Proposed split |
| The app stores nothing per Sandbox; session defaults resolve at launch from the preset label and config          | Decided | 2026-10-14 | D4             |
| The Dispatcher wakes a suspended Sandbox by patching `spec.running` and `spec.wakeGeneration` itself             | Decided | 2026-10-14 | D4             |
| Requesters write `spec`; the controller writes `status`                                                          | Decided | 2026-10-14 | Lifecycle      |
| Removing the finalizer by hand is the only break-glass; there is no force-seal                                   | Decided | 2026-10-13 | Lifecycle      |
| A seal belongs to the incarnation that sealed it; a new incarnation reopens the journal                          | Decided | 2026-10-14 | D3             |
| Names: Session Dispatcher, `OpenSession` (kept), `Quiesce`, `Quiesced`, `Draining`, `Flushing`                   | Decided | 2026-10-13 | below          |
| The queue holds about 1,000 queued commands per Session; past that, `RESOURCE_EXHAUSTED`                         | Decided | 2026-10-14 | D4             |
| Dispatch rows are kept while their Sandbox exists                                                                | Decided | 2026-10-14 | D4             |
| Queued commands have no TTL; a Sandbox gone from Kubernetes ends them as `FAILED`                                | Decided | 2026-10-14 | D4             |
| No special handling of poison commands for now; revisit when one is seen                                         | Decided | 2026-10-14 | Complexities   |

**Names.** Names already taken, so not used for the queue service: `inbox` is the Notification Service's (`inbox_id`,
`InboxView`), `outbox` is the Action Service's (`action_outbox`), "mailbox" is the task DAG's word for an agent's
receiving side, `relay` is the egress sidecar, `queue` names the runner's and Claude's input queue
([Claude queue tests](../native/claude/test_active_turn.py)), and `hold` is the History Service's retention hold. The Action Service also
has an Execution lifecycle with `pending_dispatch` and a per-replica dispatcher, so `GetDispatch` and `ListDispatches`
share a word with it; the name stands. "Delivery Service" is the runner-up but collides with the last hop the queue
service makes.
