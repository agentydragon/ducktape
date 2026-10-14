# Sandbox and Session control: who owns what

Status: **proposal, for the operator to accept or redirect; every boundary and name is open.** Nothing here is decided; each fork
names a recommendation and what would change it. Questions for the operator are collected in
[Open decisions](#open-decisions).

Scope: the responsibilities split across the services and custom resources that run a Sandbox (Pod
or VM) and deliver work to the Sessions in it. This covers the control Service, readiness and
runner health, sealing, suspension, the Session message queue, and where each fact lives. It does
not redesign storage, the History Service, or policy.

## What has to happen

1. **Run a Sandbox.** Reconcile the `Sandbox` CR into a state volume and, while running, one Pod
   or VM. Kind (`pod` or `vm`) comes from the template.
2. **Reach its runner.** Callers address `<sandbox>-control`; the replica holding the runner's
   dial-out connection answers.
3. **Say what state it is in.** Created, starting, booting, connected, ready, sealing,
   suspended, tearing down, and why it is stuck when it is.
4. **Quiesce** (today's "seal"; one operation, names in D3). Before a Pod or VM goes away (suspend or delete), stop admitting work,
   interrupt running turns, flush every Session's journal to the ingress, and only then stop it.
5. **Deliver work to a Session whatever state its Sandbox is in.** A message to a suspended
   Sandbox's Session resumes the Sandbox and arrives once the runner is up, even if the sender is
   gone.
6. **Suspend idle Sandboxes.** Never delete on idleness (operator, 2026-10-13).
7. **Create a Session** that does not exist yet, from a client-minted ID.
8. **Keep per-Session data out of Kubernetes** (operator, 2026-10-13). Kubernetes holds Sandbox-level
   facts, including transitional conditions such as sealed or unsealed; Session-level records live
   in a service database.

## Current state

- **Sandbox Controller** (Go, `sandbox_controller/`): state PVC, Pod or VM, ServiceAccount,
  `PolicyBinding`, launch ConfigMap, finalizer. It drives seals: it calls `Runner.Seal` through the
  gateway's runner proxy and records `status.sealedIncarnation` and `SessionsSealed`. It publishes
  `Running` and, since #10326, `Ready` (workload readiness only; it says nothing of the runner's
  connection).
- **Runner Gateway** (`runner_gateway/`): the `RunnerControl.Connect` listener and the proxy of the
  runner's `Runner` service. It creates, per Sandbox, a Lease (holder, epoch, liveness), a headless
  selector-less `<sandbox>-control` Service and an EndpointSlice pointing at the holding replica
  (`connection_records.py`). The records are owned by the Sandbox. It still wraps `Runner` with
  Session registry, readiness checks (`destinations.py`) and a database until
  `GATEWAY_WRAPPER_RETIRE` and `GATEWAY_DATABASE_DROP`. Its API promises that session and command
  RPCs never provision, resume or wake a Sandbox. It also seals a deleting Sandbox itself
  (`sealing.py`, `deletion_seals.py`): a second actor, and a second reader of the runner's seal response,
  beside the controller, until deletion sealing is the controller's alone.
- **Sandbox Service** (`sandbox_service/`): `CreateSandbox`, `SuspendSandbox`, `ResumeSandbox`,
  `DeleteSandbox`, grants. It writes the Sandbox's desired state; the controller validates it. It is
  to fold into the controller later (`AGENTPLANE_SANDBOX_RESOURCE`).
- **Runner**: harnesses, journal, command admission, `Seal` (durably refuses new and resumed
  Sessions, stops harnesses, seals every journal; repeated until flushed). The Sandbox's
  `bootstrap` runs inside the first `OpenSession`.
- **History Service**: ingress (journals pushed by runners, acked by cursor), raw log, fold, reads;
  records which Sandbox wrote each Session. Folds `last_model_activity_at`.
- **In flight** (verify status before relying on it): the Sandbox Service's `Sealing` and
  `Tearing down` phases (#10328); runner connectivity folded into `Ready`, reported by the gateway
  over gRPC health (approved DAG node, after #10326).

Message sending has no queue today: the app calls `SubmitCommand`, which forwards to a running
harness or fails. Nothing resumes a Sandbox on a message.

## Where the lines could fall

The "session controller" bundles three jobs that need different things, so the first question is
where to cut, not what to call the result.

| Job                                                                   | Needs                                                                     |
| --------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| Workload and runner-aware lifecycle: wake, seal, suspend, delete      | The Sandbox CR, the runner connection facts, leader election; no database |
| Idle policy: decide a Sandbox has been idle long enough               | The same runner facts and a per-Sandbox setting; no database              |
| Delivery: durable queue of messages and Session creations per Sandbox | A database, the sender's identity, the gateway                            |

| Cut                                                                                                                               | Result                                                                                                                                                                                                            |
| --------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1. One new service takes all three (the earlier plan-burn-down sketch)                                                            | The new service writes desired state, runs timers and holds a database; the controller keeps only Pods, so lifecycle policy is split across two reconcilers of the same CR                                        |
| **2. Lifecycle and idle policy stay in the Sandbox Controller; a new service holds only the queue**                               | Idle is declarative (`spec.idleSuspendAfter`) and decided by a leader-elected reconciler that already drives seals; the queue service has one job and one store; no timer state outside Kubernetes and the runner |
| 3. As 2, and later split the controller into a generic workload layer and a runner-aware layer above it (`SANDBOX_GENERIC_SPLIT`) | Compatible with 2: the runner-aware layer is where seal, connectivity and idle already sit, so the split moves files, not responsibilities                                                                        |
| 4. No new service: queue in the gateway, or in the History Service                                                                | The gateway gains a database and the power to wake Sandboxes; the History Service gains routing and waking. Both break an agreed invariant                                                                        |

Recommend **2**, and the rest of this document assumes it. Cut 1 is rejected only because idle
policy needs nothing the queue has and everything the controller has; if the operator prefers one
component that "keeps Sandboxes and their messages", D4 shows what would move.

## Proposed split

```mermaid
flowchart LR
    Sender[App, Notification Service, agents] -- OpenSession, SubmitInput and the other command RPCs --> K[Session Dispatcher]
    K --> KDB[(keeper database)]
    K -- ResumeSandbox on send --> SS[Sandbox Service]
    K -- Runner calls through proxy --> GW[Runner Gateway]
    K -. placement when caller omits Sandbox .-> HS[History Service]
    SS -- writes spec.running --> CR[(Sandbox CR)]
    SC[Sandbox Controller] -- reconciles --> CR
    SC -- Quiesce through proxy --> GW
    GW -- Lease, EndpointSlice --> L[(Leases)]
    SC -- reads connectivity --> L
    Runner -- control stream --> GW
    Runner -- journal push --> HS
    Runner -- Heartbeat with facts --> GW
    SC -- idle timer from idleSince --> L
```

| Component          | Owns                                                                                                                                                          | Does not own                                                        |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| Sandbox CR         | Desired state (`spec.running`, template), current facts (`status`: incarnation, `quiescedIncarnation`, conditions)                                            | Anything per Session                                                |
| Sandbox Controller | Pod or VM, state volume, identity, the `<sandbox>-control` Service, quiesce sequencing, idle suspension, deletion, folding every layer's fact into conditions | Runner connection, Session records, queues                          |
| Runner Gateway     | The runner connection and its authentication, the Lease and EndpointSlice, the proxy of `Runner`, per-method authorization                                    | Any Kubernetes object besides those two, Sessions, queues           |
| Runner             | Harnesses, journal, admission, seal mechanics, idle state                                                                                                     | Policy about when to suspend                                        |
| History Service    | Event log, fold, Session directory including the writing Sandbox                                                                                              | Routing, waking, suspending                                         |
| Session Dispatcher | The durable queue of messages and Session creations, wake-on-send, send-side authorization (who may wake), delivery states                                    | Sandbox objects (it asks the Sandbox Service), idle policy, history |

"Session Dispatcher" is a placeholder name for the queue service; it is not a controller. Idle is a
Sandbox-level fact (every Session in the Sandbox idle), which is why its policy sits on the Sandbox.

## Decisions

### D1. Who owns the per-Sandbox control Service

Today the gateway creates the Service, the Lease and the EndpointSlice on the first connection. The
Service is static (name, port); the slice and Lease are the dynamic connection fact.

| Option                                                                    | For                                                                                                                                              | Against                                                                                                              |
| ------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------- |
| A. Gateway owns all three (today)                                         | One writer of connection state; nothing to coordinate                                                                                            | The gateway needs `create` on Services; the name appears only after a runner connects; a stale-owner replace dance   |
| **B. Controller creates the Service; gateway writes the Lease and slice** | The Service exists, Sandbox-owned, from creation; before a runner connects the caller sees no endpoints, not a DNS failure; gateway RBAC shrinks | The controller and gateway must agree on name and port (one shared constant)                                         |
| C. No per-Sandbox Service; any gateway replica forwards to the holder     | No Kubernetes object per connection; one address for callers                                                                                     | Adds a replica-to-replica hop and trust; reverses the operator's 2026-10-12 choice of a dialed-hostname Sandbox name |

**Decided (operator, 2026-10-13): B.** The Service is part of the Sandbox's identity, like its ServiceAccount; only the
connection is the gateway's to assert. The cost is small either way, so this is the least
consequential fork. C is the option to revisit if the per-connection writes ever become a load
problem.

### D2. How readiness and runner health reach the Sandbox's conditions

Facts have different truth sources, so each gets its own condition and `Ready` is their AND:

| Condition         | Source of truth                                                               | Meaning                                                                                                                       |
| ----------------- | ----------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `Running`         | controller (Pod or VMI)                                                       | an incarnation exists and runs                                                                                                |
| `WorkloadReady`   | controller (Pod Ready)                                                        | what `Ready` reports today (#10326)                                                                                           |
| `RunnerConnected` | gateway (connection Lease)                                                    | a runner of this incarnation holds a live connection                                                                          |
| `Bootstrapped`    | runner (bootstrap result, republished at each connect)                        | the Sandbox's bootstrap ran to success; Sandbox-lifetime; False with reason `BootstrapFailed` if it failed or was interrupted |
| `Ready`           | controller                                                                    | all of the above for the current incarnation; reason `BootstrapFailed` if bootstrap failed                                    |
| `Quiesced`        | controller, from the runner's quiesce stage published on the connection Lease | False with reason `Draining`, then `Flushing`; True once every final cursor is acknowledged                                   |

The Sandbox's bootstrap runs once, at runner start and before any Session opens (operator, 2026-10-13), and
`Ready` includes `Bootstrapped`. Its outcome lives on the state volume, so each incarnation's runner republishes
it when it connects; `Bootstrapped` is the one condition that is a Sandbox-lifetime fact rather than an
incarnation's. A failed or interrupted bootstrap never runs again (the runner raises `BootstrapFailedError`), so
`Ready` reads False with the terminal reason `BootstrapFailed`: the controller does not retry it and the
Sandbox is replaced, not repaired. The per-Session step is called "setup" and is not part of `Ready`.

Two rules make this safe. Conditions other than `Bootstrapped` are keyed to the incarnation UID, so a connection or boot
fact from incarnation N is never read as incarnation N+1's. And a condition is written by exactly
one actor: the controller writes them all; every other layer publishes a fact the controller reads.

Transport for the gateway's and runner's facts:

| Option                                                                        | For                                                                                                                                                                    | Against                                                                                                                 |
| ----------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| gRPC health from the gateway (the approved node)                              | Explicit; carries any shape of fact                                                                                                                                    | The controller holds a watch per Sandbox against a replica-sharded service; a gateway outage reads as every runner down |
| **Lease and its annotations** (already written by the gateway, Sandbox-owned) | No new API; Kubernetes-native watch; survives gateway restarts as a stale-but-honest record (`renewTime` plus duration); the Lease already carries pod UID and boot ID | Controller must requeue on expiry; each fact the runner adds costs a Lease write, so write transitions only             |
| Gateway writes the Sandbox's status                                           | Fewest hops                                                                                                                                                            | Two writers on one status; breaks "layers that act on CRs do not edit them"                                             |

**Decided (operator, 2026-10-13): the Lease.** `RunnerConnected` is the Lease's liveness, and runner-reported facts
(`Bootstrapped`, `idleSince`, the quiesce stage of D3) ride its annotations. If the approved
gRPC-health node is built, its output should still land as a fact the controller folds; the choice
here is only the transport. A gateway rollout drops every connection at once; the controller must
treat the reconnect window as connectivity lost, not as a Sandbox failure, and must not replace or
restart anything because of it (no automatic spec replacement, operator 2026-10-13).

**etcd write cost of runner facts (accepted by the operator, 2026-10-13).** The gateway already renews every connected Sandbox's Lease: its loop wakes every `heartbeat_s` (10 s) and
renews once `touch_s` (15 s) has passed, so the effective cadence is 20 s and each connected Sandbox costs
about three etcd writes a minute before this design adds anything. `idleSince` therefore adds no writes: the gateway holds the latest value in memory and
puts it on the Lease in the next scheduled renewal, so the annotation change rides a write that happens
anyway. `idleSince` changes whenever the runner goes idle, which can be frequent while an agent works, which
is why it is not written on its own; the controller needs it only to minutes of precision, so up to one renewal
interval of lag is harmless. The quiesce stage and the bootstrap result are rare and are written to the Lease
at once. Cost on the reading side: the controller's Lease watch already sees a renewal event every 20 s per
connected Sandbox and must filter on annotation or holder changes rather than reconcile on each. Rejected
here: the controller subscribing to a gateway gRPC watch for idle. It saves no write that matters next to the
renewals, and it needs the controller to find and track every gateway replica, resynchronise after a gateway
or controller restart, and depend on the gateway being up to decide anything; the Lease is a record that
outlives both. Revisit only if the renewal write rate itself becomes a problem, and then lengthen `touch_s`
before adding a second channel.

**How the runner's facts reach the gateway (decided by the operator, 2026-10-14): on its heartbeat, event driven.**
The runner's facts (quiesce stage, `idleSince`, bootstrap result, the running-setups fact of D3) ride the
runner's heartbeat as `optional RunnerFacts facts`. `Heartbeat {}` is shared with the gateway's own heartbeat
(`ServiceFrame.heartbeat`), so the field goes on a runner-only `RunnerHeartbeat`, which `RunnerFrame.heartbeat`
takes under the same field number: wire-compatible both ways, and a gateway that predates the field ignores it
(reader before writer). The runner sends one immediately when a fact changes and with its first after each
connect, so nothing polls and a reconnect republishes.

- `facts`, when present, is the runner's whole current state. A heartbeat without `facts` repeats nothing; inside
  `facts`, `idleSince` absent means busy, so there is no separate `busy` fact.
- The gateway holds each connection's latest facts and every Lease write, renewals included, carries them. A
  runner that sends only on change never resends, so a failed immediate write (a stage change, the bootstrap
  result) is retried from the gateway's copy, not by the runner. The copy is per connection, so a new
  connection starts empty. `accept()` rebuilds the Lease with only the pod UID and boot ID annotations, and the
  facts come back with the runner's first heartbeat on the new connection (a different incarnation starts with
  none, D3 Outside the runner).
- Stage transitions and the bootstrap result are written to the Lease at once. Only `idleSince` is coalesced into
  the regular renewal.

### D3. Quiescing is one operation; names say which stage a Sandbox is in

**Quiescing is one operation on the whole runner, not on its Sessions** (operator, 2026-10-13). It
puts the runner in a durable lameduck state, in which it accepts only the operations that drain it,
and drains it. The state survives a runner restart within the incarnation and ends with the incarnation
(below). What stays per
Session is only the bookkeeping the drain finishes: each journal's final cursor and whether the
ingress has acknowledged it.

| Once quiescing has begun, the runner                                                 |
| ------------------------------------------------------------------------------------ |
| refuses `OpenSession`, `ResumeSession` and any command that starts or continues work |
| accepts interrupts and `StopRunnerSession` (both drain), and repeated `Quiesce`      |
| answers a redelivered `command_id` it already admitted, rather than refusing it      |
| answers reads (`ListSessions`), which today wait for an in-progress `Seal`           |
| interrupts active turns, stops harnesses, finalizes journals, pushes through cursors |

**Lameduck belongs to an incarnation.** A runner restart inside an incarnation (a crashed runner
container, a guest reboot in the same VMI) keeps the runner quiesced; a new incarnation on the same
state volume starts unquiesced. Today the runner records the state as an empty `sealed` marker file
under `--state-dir`, which is the state volume and therefore outlives the incarnation, so a resumed
Sandbox's runner would find the marker and refuse every new and resumed Session. The marker must name
the incarnation it was entered in:

- **Incarnation identity.** The runner does not choose it. The gateway already authenticates the
  connecting Pod and refuses one that is not the Sandbox's current incarnation
  (`runner_gateway/runner_control.py`); its `Welcome` carries that incarnation's UID (the Pod, or a
  VM's virt-launcher Pod, as `status.incarnation` names it). `runner_boot_id` cannot serve: it
  changes on every runner process start, restarts included.
- **Keying.** Entering lameduck writes the marker with the incarnation UID the runner last saw in a
  `Welcome`. The runner serves nothing before it connects, since every call arrives over the control
  stream, so on each `Welcome` it compares: same UID, the marker stands and the runner stays
  quiesced across the restart; different UID, the marker belongs to an earlier incarnation and is
  removed before any call is accepted, and the runner starts unquiesced.
- **Journals.** Each Session's journal sealed at its final cursor in the earlier incarnation. The
  new incarnation continues it after that cursor with an entry recording that it resumed; how a
  sealed journal is reopened and what the ingress does with the next cursor range is
  `SESSION_INCARNATIONS`'s to define, and the earlier incarnation's acknowledgement stays valid for
  it.
- **Outside the runner.** The Lease's stage annotation carries the incarnation UID it describes, and
  the gateway clears the stage when it accepts a connection for a different incarnation, so a stale
  `Quiesced` is never read for the new one. The controller's `quiescedIncarnation` is compared with
  the current `status.incarnation`; after a resume they differ, which is what makes the new
  incarnation unquiesced there as well.

**Names.** Today one word, "sealed", covers three different facts: the runner refuses new work, its
journals are final, and the ingress has acknowledged them. A `Sealed` condition that is True while
the runner is only refusing new work is wrong. Proposed:

| Today                      | Proposed                                                                | Meaning                                                                   |
| -------------------------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| RPC `Runner.Seal`          | `Runner.Quiesce`                                                        | The one operation; idempotent, so a repeat is safe                        |
| (no name)                  | reason `Draining`                                                       | Refusing new work; interrupting turns, stopping harnesses                 |
| (no name)                  | reason `Flushing`                                                       | Harnesses stopped, journals final; waiting for the ingress to acknowledge |
| `Sealed`, `SessionsSealed` | condition `Quiesced`, True only when every final cursor is acknowledged | Safe to remove the Pod or VM                                              |
| `status.sealedIncarnation` | `status.quiescedIncarnation`                                            | UID of the last incarnation that reached `Quiesced`                       |
| Sandbox phases `Sealing`   | `Quiescing`                                                             | Draining or Flushing                                                      |
| `Ready` while refusing     | `Ready` False, reason `Draining`                                        | Callers and the Dispatcher read "not accepting work" from `Ready`         |

`Quiesced` is False with reason `Draining`, then `Flushing`, until it is True; `NeverRan` and
`NotQuiesced` cover a Sandbox whose runner never connected and one stopped without quiescing. The
journal keeps the word "sealed": a journal is sealed at its final cursor, which is one step of
`Flushing` and per Session, so it never names a Sandbox condition. Per-Session cursors never appear
in Kubernetes. The remaining sections use the proposed names; the Current state section keeps today's.

Today the runner refuses new Sessions immediately, but commands to a Session whose journal is not
yet sealed are still admitted, and a turn is stopped rather than interrupted through the command
protocol. The change is to refuse at entry and interrupt before stopping, inside the same
operation.

**`Quiesce` is not today's `Seal`.** `Seal` blocks until the journals are sealed and returns each Session's final
cursor and `flushed`. `Quiesce` returns at lameduck entry and its response drops the cursors: progress goes on the
Lease and per-Session data stays out of Kubernetes. The runner already serves `Quiesce` as a straight alias of
`Seal`, so the controller already calls it and reads an empty `Unflushed` as "flushed" before it deletes the Pod.
The hazard is therefore not the rename but changing what `Quiesce` returns under the same name: a non-blocking
runner met by a reader of the old shape yields `Unflushed == []`, and the Pod is deleted before the ingress
acknowledges anything. Two readers have that shape, the controller and the gateway's deletion seal.

Rollout, reader before writer:

1. `QuiesceResponse` gains a `stage` field. The controller reads both shapes: `stage` absent means legacy
   blocking, with `sessions` authoritative.
2. The gateway's deletion seal is retired, so the controller is the only reader.
3. Only then does the runner's `Quiesce` stop blocking. The runner is the writer: the runner image and the VM
   guest's runner wheel (`nix/artifact-pins.json`, pinned separately) move only after the controller is pinned and
   the gateway's deletion seal is retired.

The legacy shape carries a removal node.

**Setup and bootstrap meet quiesce** (operator, 2026-10-13). A Sandbox is not suspendable until its Sandbox-level
bootstrap has succeeded or failed, and a suspend also waits for every Session's setup to complete; deletion waits
for neither. Quiesce would otherwise cancel a running setup (`SetupInterrupted`) and `Runner.resume` refuses a
Session whose setup is `FAILED` or `INTERRUPTED` (`SetupFailedError`), and an interrupted bootstrap is terminal,
so a suspend during either would permanently lose the Session or the Sandbox. Running setup and bootstrap also
count as busy for idle suspension. Delete interrupts them, since the Sandbox is going away.

How the wait works (operator, 2026-10-14). `RunnerFacts` carries a Sandbox-level fact for the setups running.
While an operator suspend waits for it and for the bootstrap, the controller holds `Ready` False with reason
`SuspendPending`. The Dispatcher already waits on `Ready`, so it stops delivering, new `OpenSession` included, and
the wait is bounded by the setups already running; the runner needs no mode of its own. That bound holds only if the
Dispatcher is the only caller of the runner's `OpenSession`. Today it is not: the gateway proxy lets every
configured caller account call every method except `Quiesce` and `Seal`, and the gateway's own wrapper calls it
too. So the gateway must authorize `OpenSession`, and the other methods that start or continue work, for the
Dispatcher's account alone; that is a requirement of this design with its own task. A
setup that never ends makes the suspend wait forever and leaves delete working, which is intended. An automatic
suspend needs none of it: a running setup is busy, so the conditional `Quiesce` refuses.

**Sealed journals cannot restart today.** `Session._sealing` is initialised from `journal.sealed_cursor` and
`ensure_running` then raises `JournalSealedError`, so every Session of a suspended Sandbox is unresumable until
`SESSION_INCARNATIONS` lands. Idle suspension, and any suspend that is not followed by a delete, is a one-way
door for every Session until then, and `SANDBOX_IDLE_EXPIRY` is gated on that node.

**Transitions the sequence must cover.**

- _Resume during `Quiescing`._ A Sandbox that is quiescing does not accept a resume command (operator,
  2026-10-13): the Sandbox Service refuses `spec.running = true` with a retryable code until the Sandbox is
  `Suspended`, and the Dispatcher retries then. While it is quiescing `Ready` is False with reason `Draining`.
  The Dispatcher keys each attempt to the incarnation UID and does not treat incarnation N's `Draining` refusal
  as a failure of N+1. The invariant lives in the controller, not the Service: once an incarnation is in lameduck
  (the Lease stage for the current incarnation UID, or the controller has issued `Quiesce`), the controller drives
  it to `Quiesced`, removes it, and only then reads `spec.running` for N+1, whatever the spec says meanwhile. The
  Service's refusal is an early, advisory error, since a check-then-write across two processes cannot be atomic
  with the runner entering lameduck.
- _Delete before the runner was ever up._ Delete during `Creating`, `Starting`, `Booting` or `Resuming` skips
  quiesce only if the incarnation never ran (`NeverRan`). A runner that connected and then lost its connection
  (gateway outage) looks the same on the current Lease, so `NeverRan` comes from a fact persisted at the first
  accepted connection: a Lease annotation set once, naming the incarnation.
- _Two incarnations on one volume._ A `Welcome` with a different UID drops the earlier lameduck marker, which is
  safe only if incarnation N is gone before N+1's runner writes. Node-local block storage gives no RWO exclusion
  on one node, so the controller creates N+1 only after N's Pod or VMI is fully terminated.
- _Gateway flap mid-quiesce._ A rollout flaps `RunnerConnected` while a drain is under way. The controller
  re-issues `Quiesce` and never resets the drain; this is idempotent only because the runner's lameduck holds.
- _Background work._ Background processes and durable SSH sessions count as busy for idle, but a suspend, delete
  or node loss kills them. Quiesce stops them with the harnesses, with no grace beyond the interrupt deadline.
  The deadline is a Sandbox spec field the controller reads; its default is chosen by the node that builds it.

**Commands that meet lameduck.** Three states differ. A command admitted but not yet dispatched stays pending in
the journal (#10341); the next incarnation does not dispatch it, since the conversation has moved on, and the
Dispatcher fails it with the reason `ended_by_teardown`. A command dispatched and in flight ends its turn as
`ended_by_teardown`. A refused send is retried after resume. A `StopRunnerSession` that
completes as a no-op (nothing running, or Sandbox suspended) ends in the delivery state `NO_OP` (D4). An
`InterruptTurn` is never stored: its outcome (`NO_OP`, or delivered) is its RPC response, in the same state
names, with no durable record.

**Order per Session** (operator, 2026-10-14). Inputs and model changes to one Session deliver in FIFO order.
`InterruptTurn` is delivered at once when the Sandbox is `Ready`, else it ends `NO_OP`; it is never queued or stored
and bypasses the FIFO, so stopping a runaway turn depends on neither the Dispatcher's database nor anything ahead of it
(the command names its turn, and a runner does not carry an interrupt forward to a later one). `StopRunnerSession`
stays durable and goes ahead of the line: it cancels the Session's earlier undelivered inputs, which end
`CANCELLED_IN_DISPATCHER` with the reason `stopped`, because delivering them would restart the harness the user
just stopped.

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
automatic suspend is conditional (operator, 2026-10-14): `Quiesce` carries `if_idle_since`, the runner's own
`idleSince` token as the controller last observed it. The runner compares it under the lock that enters lameduck
and refuses when it differs or the runner is busy; the controller then resets its idle timer. The token is checked
only on entry: a repeat `Quiesce` that finds the runner already in lameduck for this incarnation succeeds
whatever its token, because the controller re-issues `Quiesce` after a lost response. Operator suspend and delete
are unconditional and carry no token. A message that arrives after the controller decided but before
lameduck therefore makes the runner busy, and the suspend is refused instead of draining a Sandbox with new work.

An unconditional quiesce needs a deadline after which a turn that ignores the interrupt is stopped anyway, and a
defined history entry showing a turn ended by teardown.

**The controller is told, not polling.** Today the controller repeats `Seal` every few seconds
until every Session reports flushed. Instead, `Quiesce` is called once and returns when the runner
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
starts its idle timer fresh and has no token to send, so it issues nothing: the runner is already in lameduck, the
stage reaches the Lease (written at once, retried from the gateway's copy), and the controller adopts the quiesce
from there. The gap is the gateway's immediate Lease write. A runner that never progresses is caught by a deadline the controller
requeues for, not by periodic checks, and ends as a stuck `Quiescing` Sandbox whose finalizer an
operator removes by hand. Rejected: a server-streaming `Quiesce` held open by the controller (it
is lost on every gateway rollout and ties up a connection per quiescing Sandbox for no fact the
Lease does not already carry), and the History Service notifying the controller (a new
dependency the other path does not need).

**Refusal codes** (operator, 2026-10-14). The runner's gRPC code says what the Dispatcher does; the gateway proxy
passes runner codes through, and only its own stream close uses `ABORTED` today.

| Case                                                             | Code                  | Dispatcher                                                      |
| ---------------------------------------------------------------- | --------------------- | --------------------------------------------------------------- |
| Runner in lameduck refuses work                                  | `ABORTED`             | back to `QUEUED_IN_DISPATCHER`, retry after resume              |
| Conditional `Quiesce` refused (`if_idle_since` differs, or busy) | `ABORTED`             | none: the controller resets its idle timer                      |
| Journal sealed, setup failed, harness stopped, `BootstrapFailed` | `FAILED_PRECONDITION` | terminal: `FAILED` with the reason                              |
| Redelivered `command_id` already admitted                        | success               | `RUNNER_ADMITTED`, or later, from the runner's answer           |
| Runner `OpenSession` of an existing Session (`ALREADY_EXISTS`)   | `ALREADY_EXISTS`      | `ListSessions` says whether it is on this runner; if so, opened |

**Ambiguous delivery resolves on redelivery.** The runner's journal lives on the state volume and answers a
redelivered `command_id` with its existing admission, even after the harness stopped or the journal was sealed. A
call whose reply was lost across an incarnation boundary is therefore resolved by redelivering to the next
incarnation. `unknown` remains only for a Sandbox that was deleted. For `OpenSession` this holds
within one runner build only (D4).

### D4. The queue service: required or optional, one door or two, separate or merged

The queue service does one job: **delivery.** Accept a message durably, wake the
Sandbox, deliver once the runner is Ready, survive the sender leaving. It needs a database, the
Sandbox's name, the gateway and the Sandbox Service. Idle policy is the controller's (below).

**Required or optional.** Recommended: _required for senders, not for the platform._ Everything
that sends work to a Session (app composer, Notification Service, agent messaging) goes through the
Dispatcher's command RPCs, so there is one set of delivery semantics: accepted, then delivered, or failed
with a reason. A second direct path for the same senders means "sometimes queued, sometimes an
immediate error", which the suspended composer story cannot present. The runner proxy at the
gateway stays the only runner surface, and system callers still use it directly: the Dispatcher itself,
the controller's `Quiesce`, operator diagnostics and acceptance tests. Per-method authorization at the
gateway is where "only these callers may call `DeliverCommand`" is enforced; it is a policy edit,
not a new path.

What remains of the runner surface for direct use once the Dispatcher exists:

| `Runner` method  | After the Dispatcher                                                             |
| ---------------- | -------------------------------------------------------------------------------- |
| `DeliverCommand` | Dispatcher (senders); direct for system callers and tests                        |
| `OpenSession`    | Dispatcher (its own `OpenSession`); direct for system callers                    |
| `ResumeSession`  | Dispatcher, as an internal step of each command RPC (the harness half of waking) |
| `Quiesce`        | Sandbox Controller only                                                          |
| `ListSessions`   | direct; reads need no wake                                                       |

A deployment with no Dispatcher is a valid reduced configuration (a Sandbox that is started by hand
and never suspended), because the runner, gateway and controller do not depend on the Dispatcher. It
just has no wake-on-send and no idle suspension. That optionality costs nothing in code: absent
callers.

**The Dispatcher's RPC surface.** Callers state intent; waking, resuming and delivering are internal steps:

- `OpenSession(session_id, sandbox, spec, setup_script?)`: stores the Session's creation durably and
  returns `QUEUED`. The client mints the Session ID, as everywhere (invariant: one ID, client-minted),
  which makes it idempotent: a retry with the same payload returns the same record, and another Sandbox,
  spec or setup script for a known ID is refused. The Dispatcher waits for the Sandbox to be `Ready`
  (resuming it where the sender may wake it) and calls the runner's `OpenSession`, which is itself
  idempotent within one runner build (the runner composes the stored instructions from its own platform text, so a
  redelivery to an incarnation of another image can answer `ALREADY_EXISTS` for a Session that matches; the
  Dispatcher treats that as opened after a `ListSessions` check that the Session is on the runner). The Dispatcher's `OpenSession` is a different RPC from the runner's of the same name
  (kept by the operator, 2026-10-13): it queues, the runner's executes.
- One RPC per command, mirroring the runner's: `SubmitInput`, `ChangeModel`, `ChangeReasoningEffort`,
  `InterruptTurn` and `StopRunnerSession`, each taking `(command_id, session_id, <that command's fields>)`.
  Each stores the command durably and returns `QUEUED`; none waits for delivery. `command_id` is the
  idempotency key across all of them: a retry of the same operation and payload returns the same record, and
  the same ID under another operation or payload fails `ALREADY_EXISTS`, as the runner's own admission
  does. The Dispatcher resolves the Session's Sandbox from its own record or the History Service's Session
  directory, resumes the Sandbox if suspended (where the sender may wake it), waits for `Ready`, resumes a
  stopped harness, and calls the runner's RPC of the same name. An unknown Session fails `NOT_FOUND`; a
  command queued behind its Session's `OpenSession` waits for it. A notification is a `SubmitInput` from a
  different caller, authorized by caller identity, so it has no RPC of its own. `InterruptTurn` and `StopRunnerSession` never wake a suspended Sandbox (operator, 2026-10-13): with nothing running there is nothing to interrupt or stop, so they complete as no-ops. `InterruptTurn` is the exception to "stores the command durably": it is delivered at once or reports `NO_OP` in its response.
- `GetDispatch`, `WatchDispatches` (stream, by Session or Sandbox), `ListDispatches`, `CancelDispatch`
  (not once delivery is ambiguous). A Session's open is a dispatch like a command, with the same states.

**Delivery states** (operator, 2026-10-14: each name says where the command is). One vocabulary for every
dispatch, a command or a Session creation, recorded by the Dispatcher:

| State                     | Meaning                                                                                                                                       | Survives an incarnation ending                         |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| `QUEUED_IN_DISPATCHER`    | Stored durably, not yet sent. A reason says what it waits for: Sandbox suspended, resuming, not `Ready`, or `wakeOnSend` off                  | yes (Dispatcher database)                              |
| `DELIVERING_TO_RUNNER`    | A call to the runner is in flight                                                                                                             | no: back to `QUEUED_IN_DISPATCHER` if the call is lost |
| `RUNNER_ADMITTED`         | The runner journaled it (today's `CommandAdmitted`). Terminal for commands with no model-context step                                         | yes (runner journal on the state volume)               |
| `HARNESS_CONFIRMED`       | It reached the model's context (today's `harness_user_message_confirmed`). Only `SubmitInput` gets here                                       | yes (History Service)                                  |
| `NO_OP`                   | Nothing to do, e.g. `InterruptTurn` or `StopRunnerSession` on a suspended Sandbox or with nothing running; a reason says where it was decided | yes                                                    |
| `FAILED`                  | Terminal, with a reason that names where it failed, e.g. `ended_by_teardown` for an admitted-but-undispatched command                         | yes                                                    |
| `CANCELLED_IN_DISPATCHER` | Cancelled before delivery began, by its sender or by a later `StopRunnerSession`; a reason says which                                         | yes                                                    |
| `UNKNOWN`                 | Only for a deleted Sandbox, whose journal can no longer answer a redelivery                                                                   | yes                                                    |

Transitions: `QUEUED_IN_DISPATCHER → DELIVERING_TO_RUNNER → RUNNER_ADMITTED → HARNESS_CONFIRMED`. A refusal because
the runner is quiescing returns `DELIVERING_TO_RUNNER` to `QUEUED_IN_DISPATCHER`, a retry rather than a failure.
`QUEUED_IN_DISPATCHER → NO_OP` for a stop that finds nothing to do; an interrupt that finds nothing to do reports `NO_OP` in its response. Redelivery of a `command_id`
after a lost reply resolves from the runner's journal, which is why `UNKNOWN` is rare.

**Where a new Session's spec comes from.** `OpenSession` carries it, from the caller (the app resolves its
presets into a concrete `SessionSpec` first; the Dispatcher knows no preset). The Dispatcher stores it with the
Session record and does not interpret it.

**The Dispatcher never creates Sandboxes.** The Sandbox named in `OpenSession` must exist; a missing one
fails the dispatch. Creating, deleting and templating Sandboxes stays with the Sandbox Service. The Dispatcher
only asks it to resume a suspended one. It needs a Sandbox _name_, never an address; the gateway resolves the
name from the dialed host.

**Placement.** Callers pass the Sandbox when they know it (the app resolves Thread to Session to
Sandbox through the History Service already). The Dispatcher falls back to the History Service's
Session directory when they do not. A Session that is only queued for creation is known to the
Dispatcher alone until its runner pushes, so the Dispatcher's own record is authoritative until then.

**History Service dependency.** Delivery does not need it: admission is the runner's receipt, and
placement is passed or looked up. Placement lookup and the reading of final-cursor acks (already required
for any suspend or delete) are the only touches. For idle policy the controller should not derive
activity from history at all (below), so idle policy and the Dispatcher run with the History Service down, except
that, as today, nothing can finish sealing while the ingress is down. An outage therefore stops a quiesce from
finishing, not messaging: a Sandbox keeps accepting messages until its quiesce enters lameduck. There is no
ingress-health gate before an automatic suspend (operator, 2026-10-14, accepted as a trade): an automatic
quiesce started during a History Service outage can sit in lameduck, refusing new work, until the outage ends.

**Idle signal.** Two different things get called "last active":

- _Busyness_: is any turn, command, background process or attached terminal active in this runner
  right now? The runner knows authoritatively and the controller must have it without the History
  Service.
- _Last model activity_: a folded timestamp the UI and notification quiet-window read. The History
  Service owns it.

Recommend suspension uses the first: the runner publishes `idleSince`, absent while it is busy
(carried like other runner facts in D2), and the Sandbox Controller runs the timer against
`spec.idleSuspendAfter`, then suspends through the D3 sequence. The timer is restartable because
the facts live in the runner and Lease, not in controller memory. A message sent to an idle Sandbox
that is about to suspend is safe: the suspend is conditional on the `idleSince` token (D3), and a sealing runner refuses
the message with a retryable code that the Dispatcher redelivers after resume. The Dispatcher keeps no pending mark
and the controller reads nothing from it. `SESSION_ACTIVITY_STREAM` (the UI's activity facts) uses the same runner idle definition (operator, 2026-10-13), so there is one definition of idle; the History Service's folded `last_model_activity_at` stays a separate timestamp for the UI and the notification quiet window.

**Per Session and per Sandbox (decided by the operator, 2026-10-13).** The UI shows the idle time of a _Session_; suspension needs the aggregate over _all Sessions_ in the Sandbox (operator, 2026-10-13). The runner computes both from the same per-Session facts: a Sandbox is busy while any Session is busy (or a background process or attached terminal runs), and its `idleSince` is when the last of them went idle. Only the Sandbox aggregate goes on the Lease. Per-Session idle times stay out of Kubernetes (the boundary in requirement 8): their number is unbounded, they change with every turn, and no controller decision needs them. The activity stream is the History Service's: it already folds the runner's events into `SessionSummary` (`last_turn_status`, `last_model_activity_at`) and serves `watch_session_summaries`, so a per-Session `idleSince`, derived from the turn start and end events it ingests, is one more folded field on that summary and the stream carries it to listeners (the UI, the notification service). It is display-only: suspension never reads it. A background process or attached terminal belongs to no Session, so it counts only in the Sandbox aggregate. While a Sandbox is suspended nothing new is ingested, so each Session reads idle since its last turn ended.

**Separate service or merge with the gateway.** "Both required" is not an argument to merge:
required-ness says nothing about failure domain, state or replica shape.

| Option                    | For                                                                                                                                                                                                                                                             | Against                                                                                                                                                         |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Separate service**      | Gateway stays stateless and database-free (`GATEWAY_DATABASE_DROP`), keeps working through a Dispatcher or History outage; Dispatcher has one active owner per Sandbox without constraining gateway replicas; gateway keeps its "never wakes a Sandbox" promise | One extra hop; a second Deployment with its own database                                                                                                        |
| In the gateway            | One fewer service                                                                                                                                                                                                                                               | Gives the gateway a database and the power to resume Sandboxes; couples queue ownership to connection-holder replicas, which are sharded for a different reason |
| In the Sandbox Controller | Nearest the desired state                                                                                                                                                                                                                                       | A leader-elected reconciler with no database would gain a queue; Kubernetes would hold per-Session data. Idle policy, which needs no database, does belong here |
| A CR per queued message   | No new store                                                                                                                                                                                                                                                    | Per-Session data in Kubernetes, against the stated boundary; etcd write rate                                                                                    |

Recommend **separate** (cut 2). Multiple Dispatcher replicas shard Sandboxes by a per-Sandbox Lease so each
Sandbox has one active owner, as the gateway fences connection epochs.

**Who may wake.** The Dispatcher knows the sender's identity, so it enforces which kinds of sender may
resume a Sandbox (user message and notification yes; agent message no unless decided,
`AGENT_MESSAGE_RECEPTION`). Its ServiceAccount holds the Sandbox Service grant to resume; the
Sandbox Service stays the only writer of desired state.

### D5. Where each fact lives

| Fact                                                                                                              | Home                                             |
| ----------------------------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| Desired running, template, incarnations, conditions, `quiescedIncarnation`, the rendered spec of each incarnation | Sandbox CR (Kubernetes)                          |
| Runner connected, boot state, `idleSince`, quiesce stage                                                          | Lease annotations (Kubernetes, Sandbox-level)    |
| Session events, directory, writing Sandbox, summaries                                                             | History Service database                         |
| Queued messages, their delivery state, queued Session creations                                                   | Dispatcher database                              |
| Session journal and cursors                                                                                       | Runner volume, mirrored by the ingress           |
| Per-Session seal cursors and flush state                                                                          | the quiesce response and the ingress, never a CR |

Conditions and annotations carry Sandbox-level, transitional facts only. A per-Session list in
Kubernetes is the line not to cross.

## Sandbox lifecycle

Phase is derived, never stored (as for the Sandbox Service today):

```mermaid
stateDiagram-v2
    [*] --> Creating
    Creating --> Starting: workload created
    Starting --> Booting: workload running
    Booting --> Ready: runner connected and Bootstrapped
    Booting --> BootstrapFailed: bootstrap failed or interrupted
    Ready --> Quiescing: suspend (SuspendPending until bootstrap and every Session setup are done)
    Ready --> Degraded: runner crash loop
    Degraded --> Ready: runner recovers
    Quiescing --> Suspended: Quiesced, suspend requested
    Suspended --> Resuming: spec.running true (refused while Quiescing)
    Resuming --> Booting: new incarnation running
    Suspended --> Archived: SANDBOX_ARCHIVE
    Creating --> TearingDown: delete
    Starting --> TearingDown: delete (NeverRan)
    Booting --> Quiescing: delete (runner connected before)
    Ready --> Quiescing: delete
    Degraded --> Quiescing: delete
    BootstrapFailed --> Quiescing: delete
    Quiescing --> TearingDown: Quiesced, delete requested
    Suspended --> TearingDown: delete (already quiesced)
    TearingDown --> [*]
```

`Booting` covers the runner connecting and the Sandbox bootstrap running; it ends in `Ready` or, for good, in
`BootstrapFailed`. A delete from a state with a runner that connected in this incarnation quiesces first;
`NeverRan` (no accepted connection recorded) skips it. Removing the finalizer by hand is the only break-glass out
of a stuck `Quiescing` or `TearingDown`.

**Sandbox bootstrap.** The bootstrap is the launch file's script, run once per Sandbox, not per incarnation
(operator, 2026-10-13). The runner starts it when it starts, before any Session opens (`Runner.startup`; a
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
- Not built yet: the runner publishing bootstrap progress and result on the connection Lease, and the
  controller folding `Bootstrapped` into `Ready`.

`Resuming` carries a reason for what it waits on: the state volume's node is down or full, or the
workload is unschedulable. `Degraded` is a reason on `Ready`, derived from the runner restarting
repeatedly; nothing replaces the spec automatically.

**How idle suspension expresses itself.** Desired state stays in `spec.running`, and layers acting on CRs do not
edit them, so the controller's idle timer does not write the spec. It writes a status fact,
`status.suspendedBy: Idle`, recording the `spec.wakeGeneration` it suspended at; the Sandbox Service wakes by
bumping `spec.wakeGeneration`, and the controller runs the Sandbox when `spec.running` holds and the generation
differs from the recorded one. "Suspended" then has two distinguishable causes, `Desired` (spec.running false)
and `Idle`, and an operator hold (`spec.wakeOnSend: false`) is distinguishable from an idle suspend.

- Suspend and delete both run the D3 sequence; a suspended, already-quiesced Sandbox deletes without
  waking (`quiescedIncarnation`).
- A wedged seal is cleared by removing the finalizer by hand. That is a break-glass force delete for broken Sandboxes only: it discards unflushed journals and the CR-owned state volume. No force-seal exists to build around.
- Spec update is suspend, then resume, which re-derives the spec from the current template. The
  rendered spec of each incarnation is stored on the Sandbox for diffing.

Message to a suspended Session:

```mermaid
sequenceDiagram
    participant S as Sender
    participant K as Dispatcher
    participant SS as Sandbox Service
    participant C as Controller
    participant R as Runner
    S->>K: SubmitInput(command_id, session, input)
    K->>K: persist, ack sender (accepted)
    K->>SS: ResumeSandbox
    SS->>C: spec.running = true
    C->>C: new incarnation, Ready for this incarnation
    K->>R: ResumeSession, then DeliverCommand (same command_id)
    R-->>K: admission
    K->>K: mark delivered
```

## What this makes adoptable

Each DAG node fits without reshaping the split. Node edits belong to the Plans burn-down lane.

| Node                                                        | Where it lands                                                                                                                |
| ----------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `SESSION_QUEUE_DESIGN`, `SESSION_MESSAGE_QUEUE`             | Dispatcher; this document answers the new-service fork and the dependency questions                                           |
| `SESSION_ACTIVITY_STREAM`                                   | Built on the runner's idle definition, the same definition idle suspension uses (operator, 2026-10-13)                        |
| `SANDBOX_IDLE_EXPIRY`                                       | Controller: `spec.idleSuspendAfter`, runner `idleSince`, the D3 sequence; gated on `SESSION_INCARNATIONS`                     |
| `SESSION_REVIVE`, `SESSION_INCARNATIONS`                    | Controller starts the incarnation; History Service records it from the runner's announcement                                  |
| `BOOTSTRAP_PROGRESS_CONTRACT`                               | Runner fact on the Lease, republished at each connect; `Bootstrapped` is part of `Ready`; `BootstrapFailed` is terminal       |
| `CONTROLLER_SEALS`, `SANDBOX_LIFECYCLE`                     | Already the controller's; D3 adds the interrupt step                                                                          |
| `SANDBOX_GENERIC_SPLIT`                                     | The controller keeps Pod, VM, volume; sealing and runner facts become an optional layer on top; the Dispatcher is outside     |
| `SANDBOX_SPEC_EDIT`, `SANDBOX_IMAGE_REPLACE`                | Suspend then resume; no change to the split                                                                                   |
| `STATE_VOLUME_BLOCK`, `WORKSPACE_VOLUME`, `SANDBOX_ARCHIVE` | Controller; the Dispatcher only waits for `Ready`                                                                             |
| `VM_RAM_SUSPEND`                                            | Needs its own answer to "seal before suspend": see below                                                                      |
| `THREAD_SUCCESSOR_DELIVERY`, `COMMAND_DELIVERY_STATES`      | The Dispatcher's delivery states (D4) are the vocabulary; redelivery resolves ambiguity, `unknown` only for a deleted Sandbox |
| `AGENT_MESSAGING`, `SUSPENDED_COMPOSER`                     | Senders of the command RPCs                                                                                                   |

## Complexities not yet named

- **Suspend versus an arriving message.** A message accepted while the Sandbox is sealing must wait
  for the _next_ incarnation to be Ready; delivering to the sealing runner loses it or errors. Key
  every delivery attempt to the incarnation UID, and make the runner refuse once quiescing begins with a
  distinct code.
- **At-least-once with ambiguity.** The Dispatcher persists the command before acknowledging the
  sender; the runner deduplicates on `command_id`. A call whose reply was lost across an
  incarnation boundary is redelivered to the next incarnation, whose journal answers with the existing
  admission; only a deleted Sandbox leaves it `unknown` (`THREAD_SUCCESSOR_DELIVERY`).
- **Competing desires.** An operator suspend, a queued message, an idle timer and a deletion can all
  be live at once. Decide precedence: deletion beats everything; an explicit operator suspend
  needs a way to say "hold, do not wake" or it is undone by the next notification.
- **What idle means.** Background processes, durable SSH processes, an attached terminal or
  console, and a running setup or bootstrap must count as busy, or a Sandbox is suspended under a running job.
  Busyness does not cover an operator suspend, delete or node loss; D3 says what quiesce does then.
- **Wake cost and abuse.** Resume starts a Pod or VM and every harness. Notifications and agent
  messages can wake Sandboxes in bulk; rate limits, per-sender quotas and per-Session lazy
  `ResumeSession` (only Sessions with pending work) are needed. A resume storm also hits scheduling
  and the state volume's node.
- **Resume can be impossible.** With node-local volumes the Sandbox can only resume where its PVC
  lives; the node may be down or full. The queue needs a visible, bounded wait with a reason, and a
  message TTL.
- **Failed or deleted targets.** A message for a deleted Sandbox or Session, a failed revival, a
  poison command: each needs a terminal state the sender can read, not a silent retry.
- **History outage blocks suspend and delete.** Intended, since sealing waits for the ingress ack,
  and an automatic quiesce started during the outage can sit in lameduck until the History Service returns
  (accepted, no ingress-health gate). The controller must back off, not retry every few seconds per Sandbox.
- **Interrupt semantics.** A turn interrupted by teardown needs a history entry distinguishing it
  from a user interrupt, and a harness that ignores the interrupt needs the deadline in D3.
- **VM RAM suspend.** If a VM keeps harness processes alive across suspend, "seal before suspend"
  means something different (flush the journal, keep the harness). Settle this before
  `VM_RAM_SUSPEND` assumes the Pod sequence.
- **Rolling out together.** The gateway rollout reconnects every runner at once and flaps
  `RunnerConnected`; debounce it. Runner, gateway, controller and Dispatcher roll independently, so
  protocol additions (interrupt in `Quiesce`, Lease annotations) must be accepted by readers before
  writers emit them (image before config).
- **Credentials across suspension.** Egress grants, PolicyBindings and projected tokens must still
  be valid when a Sandbox resumes hours later; a resume that comes up with expired grants looks
  like a broken Sandbox.
- **Time.** `idleSince` is an opaque token from the runner; the controller measures elapsed time from its first
  observation of each distinct token, so clock skew and a restarted runner cannot suspend early. After a
  controller restart it starts the timer on its first observation; suspending up to one `idleSuspendAfter` later
  is acceptable.
- **Why-not observability.** "Why hasn't my message arrived" needs per-message state (queued,
  waiting for Ready, delivering, delivered, unknown, failed with reason) readable by the sender and
  the same Sandbox conditions as above.
- **Multiple Sessions per Sandbox.** Idle is the conjunction over all of them, and waking Sandbox
  for one Session should not start the others' harnesses.
- **Session ID collision.** The client-minted ID must be unique across Sandboxes; the Dispatcher's
  record covers the window before the History Service has seen the Session, and the History
  Service's first-writer binding covers the rest.

## Open decisions

1. **Dispatcher as the required door for senders, with direct runner access for system callers?**
   Recommend yes (D4).
2. **Dispatcher as its own service rather than in the gateway?** Recommend yes (D4).
3. **Idle policy in the Sandbox Controller (declarative, from runner-reported busyness) rather
   than in the queue service or driven by history?** Recommend yes (cut 2, D4).
4. ~~Lease annotations rather than gRPC health as the runner-fact transport?~~ **Decided
   (operator, 2026-10-13): Lease annotations** (D2). This replaces the mechanism of the earlier
   gateway-health connectivity node, not its goal.
5. ~~Controller creates the control Service?~~ **Decided (operator, 2026-10-13): yes** (D1).
6. **Interrupt inside the one `Quiesce` operation, and the renames in D3 (`Quiesced`, `Draining`, `Flushing`, `quiescedIncarnation`)?** Recommend yes (D3).
7. ~~A way for an operator to hold a Sandbox suspended against wake-on-send?~~ **Decided
   (operator, 2026-10-13): `spec.wakeOnSend: false`.** A Sandbox with it set is not woken by a send:
   the Dispatcher queues the message and the Sandbox stays suspended. It doubles as the feature gate
   for wake-on-send, so the Dispatcher can land with waking off.
8. **Name.** Session Dispatcher, `Quiesce`, `Quiesced`, `Draining` and `Flushing` are confirmed
   (operator, 2026-10-13). The Dispatcher has an explicit `OpenSession` (the spec and setup script are
   real input) and one RPC per command, each taking a Session ID; waking and resuming are internal (D4). The queue service is "Session Dispatcher" and a dispatch is one queued command or
   Session creation. The names to avoid are taken:
   `inbox` is the Notification Service's (`inbox_id`, `InboxView`), `outbox` is the Action
   Service's (`action_outbox`), "mailbox" is already the task DAG's word for an agent's receiving
   side (`AGENT_MESSAGING`), `relay` is the egress `SidecarRelay`, `queue` names the runner's and
   Claude's input queue ([input queueing](claude_input_queue.md)), and `hold` is the History
   Service's retention hold. No `Dispatcher` or `dispatch` identifier exists in the code today;
   "Delivery Service" is the runner-up but collides with the runner's `DeliverCommand`, the last hop
   it makes.
9. ~~Suspend during setup or bootstrap?~~ **Decided (operator, 2026-10-13):** suspend waits for the Sandbox bootstrap and for every Session's setup; deletion does not (D3).
10. ~~How idle suspension is expressed?~~ **Decided (operator, 2026-10-14): `status.suspendedBy: Idle`**: the controller writes `status.suspendedBy: Idle` and the Sandbox Service wakes by bumping `spec.wakeGeneration`, so `spec.running` is never edited by a layer that acts on the CR (Sandbox lifecycle).
11. **Do `ChangeModel` and `ChangeReasoningEffort` start a stopped harness?** The Dispatcher resumes a stopped
    harness as an internal step of each command RPC, so a change to a stopped Session would start a harness only
    to change a setting. The alternative applies the change to the retained spec and takes effect at the next
    resume, with no wake. Not ruled yet.
