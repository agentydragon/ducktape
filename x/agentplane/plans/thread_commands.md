# Durable Thread commands

Status: **proposed design.** This is the command/reconciliation contract behind
`NEWTHREAD_DURABLE`; it is not an implementation recipe for a browser-local sequence. Read it
before adding the unified composer, a Thread resume flow, or another direct app-to-runner mutation.

## Problem and design boundary

The easy path and the advanced paths must have the same reliability guarantee:

- A new Thread with a new Sandbox: type a prompt and press Enter.
- A new Thread in a selected existing Sandbox.
- An input to a running Thread.
- An input to a Thread whose Sandbox is suspended: resume it and continue the same Thread.
- A model switch, interrupt, or stop request for a running Thread.

An app HTTP acknowledgement must mean that the operator's intent is durable. It must not mean that
the tab happened to remain open long enough to wait for a Pod, attach a runner, and send a request.
Nor may a page reload select a different Thread or silently forget a pending input.

The app therefore owns a durable **Thread command outbox** and reconciles it into Kubernetes and
runner operations. Kubernetes remains authoritative for Sandbox/Pod lifecycle. The runner remains
authoritative for command receipt, harness state, and the transcript. The app must not create a
second lifecycle history that purports to replace either.

### Authority boundary

The desired Thread and the desired Sandbox are related, but they are not the same desired-state
record:

| Concern                                                                                                                     | Authority and durable record                                                             | What the other layers may retain                                                                                                                                                                            |
| --------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A Thread exists, is targeted at a Sandbox, and has commands waiting to run                                                  | Agentplane PostgreSQL: `Thread`, its target/session plan, and the `ThreadCommand` outbox | Kubernetes gets a correlation label; the runner gets a session id and commands.                                                                                                                             |
| A new Sandbox should exist with particular resources, preset-derived configuration, egress policies, and action policy sets | Kubernetes: the labeled `Sandbox` CR and its spec                                        | Before the CR is created, the Thread target holds the fully resolved creation input needed to materialize it. Afterward the app pins the resulting name/UID, but does not mirror or mutate lifecycle state. |
| Sandbox/Pod is provisioning, running, suspended, deleted, or otherwise observed                                             | Kubernetes status and Pod observations                                                   | The app projects a snapshot as a prerequisite for commands; it does not store a competing phase log.                                                                                                        |
| A runner command reached the harness and what it did                                                                        | runner replayable `Event` log, copied by the app                                         | The outbox retains delivery correlation and pending intent, not an invented receipt.                                                                                                                        |

Thus “we want to start a Thread” goes into Postgres as a `Thread` plus a first outbox command.
For a new Sandbox, reconciliation then creates or finds the correlated Kubernetes object. Once that
object exists, its CR is the only mutable Sandbox desired state; the app's record is only the
Thread's immutable bootstrap intent and the concrete object identity it selected.

## Terms

**Thread** is the durable, user-facing history. Its UUID is minted by the client before the first
POST and is the identity the URL, sidebar, and transcript use. It is never a synonym for a runner
session id.

**Runner session** is a runner's client-named, replaceable execution record. It hosts a harness for
a Thread at one point in time; it is not the Thread's lifetime boundary. A gRPC `Attach` stream is
transport only; it is not either a Thread or a runner session.

The current runner implementation treats one session record as one native conversation and can
reopen that same record after a runner-process restart. The target model allows a new runner session
to resume the Thread represented by an earlier session in the same Sandbox, but initial delivery
does not require that runner capability.

**Thread runner session** is the app's durable association of one Thread with one runner session on
one concrete Sandbox UID. It optionally names the predecessor session whose native continuation it
resumes. A Thread may have several of these over its lifetime, but one is the active command target
at a time. Calling it `ThreadRunnerSession` avoids overloading the runner protocol's word
“attachment.”

**Thread command** is one durable outbox item: an operator intent with a command-specific client id,
an ordered Thread scope, and immutable payload. Its outcome is projected from authoritative runner
events and Kubernetes observations; it is not a mutable “launch phase.”

## Domain records

The exact SQL shape can evolve, but these are the required identities and invariants.

```text
Thread
  thread_id                   product identity; chosen before its first command
  created_at, name, archived  app-owned presentation fields

ThreadSandboxTarget
  thread_id
  existing: sandbox_name + sandbox_uid
      or
  new: fully resolved SandboxCreationSpec
       (only until reconciliation materializes the Kubernetes Sandbox CR;
        the resulting object bears thread_id as its correlation label, then
        its sandbox_name + sandbox_uid are pinned here)

ThreadRunnerSessionPlan
  thread_id
  runner_session_id           client-minted identity for the first/successor runner session
  session_spec                immutable runner SessionSpec to use when opening it
  resumes_runner_session_id   optional predecessor requested for a future successor

ThreadRunnerSession
  thread_id
  sandbox_name + sandbox_uid
  runner_session_id           runner address, not Thread identity
  resumes_runner_session_id   optional prior session for this Thread in this Sandbox
  continuation                runner-owned continuation proof/handle from a successful resume

ThreadCommand
  thread_id
  command_id                  command-kind idempotency key
  ordinal                     one durable order within a Thread
  kind + immutable payload
  accepted_at

RunnerCommandJournal
  runner_session_id + command_id
  immutable command digest + target evidence
  state                       accepted | deferred | dispatch_planned |
                              native_effect_observed | settled
  native_correlation          provider id(s), batch mapping, and source sequences
  recovery_evidence           replay/observation facts used after a runner restart

HarnessUserMessage
  thread_runner_session_id
  harness_message_id          runner receipt identity; native message/command id when one exists
  text                        exact user-message text the harness confirms
  origin_input_ids[]          ordered `SubmitInput` ids that produced this message
  turn_id + admission         turn and proven native admission boundary
  source_sequences[]          runner Native evidence for this receipt
```

`ThreadRunnerSessionPlan` is intent; it may exist before either a concrete Sandbox UID or a runner
session does. `ThreadRunnerSession` is the actual association, written only when runner evidence
proves that the planned session is attached to the pinned Sandbox. This prevents a pre-minted
runner session id from being mistaken for either a Thread or an already-running harness.

For a new Thread, one transaction mints/inserts the `Thread` with its Sandbox target and initial
runner-session plan, then appends the first `SubmitInput` outbox item. The pre-minted `thread_id`
is the product identity; it is not a second request/launch UUID. For an existing Thread, the same
transaction only appends an outbox item. Inputs use the runner's already client-chosen `input_id` as
their command id; model changes use `switch_id`. New idempotency fields are required for runner
commands that do not yet have one.

`ThreadStartRequest` is the first, narrow persistence step toward this model: it captures the
initial target, runner-session plan, and input under `thread_id`. It must evolve into the atomic
Thread-creation-plus-first-outbox-item transaction, rather than become a parallel launch subsystem.

`SubmitInput` is a requested user input, not a claim that the native harness created one matching
user message. `HarnessUserMessage` is the actual emission record. It lets one native message
truthfully retain several requested-input origins instead of losing or inventing their provenance.

`RunnerCommandJournal` is runner-owned support state on the session's durable state volume. It is
neither a second Agentplane outbox nor the user-facing transcript: it makes a runner receipt and
the evidence needed to recover it survive process or harness replacement. The runner commits the
journal record before acknowledging `CommandReceived`, records any deferred scheduling decision and
native correlation before a non-idempotent native boundary, and records observed effect evidence
before settlement. The app copies the resulting receipt, settlement, and linked evidence into its
normal event projection; it does not try to recreate the runner's recovery decisions itself.

### The outbox is the desired side of reconciliation

```text
new Thread POST                         existing Thread POST
---------------                         --------------------
mint thread_id                          use existing thread_id
insert Thread + target + session plan   append ThreadCommand(SubmitInput)
append ThreadCommand(SubmitInput)        commit
commit
             \                         /
              durable Thread command outbox
                           |
                           v
 reconciliation: materialize/observe Sandbox target, then runner command
                           |
                           v
                runner's replayable event log / receipt
```

The reconciler repeatedly compares the ordered outbox with the copied runner event log. An input
with no matching runner receipt is pending; a supported durable input eventually reaches its proven
admission or rejection observation. Kubernetes state is a prerequisite observation, not an outbox
phase. This keeps one durable desired record and one authoritative actual record without inventing
a synthetic launch-status stream.

### Input provenance is not one-input/one-message

The runner reports a `HarnessUserMessageConfirmed` receipt when the harness has actually confirmed a
user-message emission/admission boundary:

```text
HarnessUserMessageConfirmed(
  harness_message_id,
  text = "…actual harness user message…",
  origin_input_ids = [input_123, input_125],
  turn_id,
  admission,
  source_sequences,
)
```

That exact text and the ordered origin list are the common fact the app needs. An input is settled
as admitted only through a receipt that names it in `origin_input_ids`; it is not settled merely
because a native protocol accepted an enqueue request. A rejected/withdrawn input gets its own
terminal result without a `HarnessUserMessage` record.

The current pinned Claude evidence shows compatible string requests coalescing with `\n`, with one
native batch identity but lifecycle evidence for every contributor. The pinned test must capture
that behavior before the runner depends on it. Codex's queued/joined inputs are expected to retain
separate native user-message records and thus separate one-origin receipts. The runner does not
replace either with a fictitious common queue; the provider-specific `Native` frames remain attached
to the receipt. If a tested version differs, the common record preserves the observed grouping
rather than a provider stereotype.

The runner journal must persist its requested-input-to-native-command mapping before native dispatch
so a restart can reconstruct this provenance. A coalesced confirmation is one actual message with
many origins; it is never permission to settle only the representative id or to resend
non-representative inputs.

### Runner command lifecycle

Every durable control command needs two runner-observed milestones, not one optimistic gRPC-write
outcome:

```text
ThreadCommand committed       runner durable receipt           semantic settlement
in Agentplane outbox   ->     CommandReceived(command_id,      -> CommandSettled(command_id,
                                handling = now | at_boundary)     succeeded | rejected | noop)
```

`CommandReceived` means the runner committed the command into its durable journal and replayable
session log, so Agentplane can stop retrying delivery and the command survives attachment loss.
`handling = at_boundary` is a
receipt, not completion: the runner has deliberately deferred an otherwise valid request. A
terminal `CommandSettled` means the command's product-level effect happened, was impossible, or
was already inapplicable. It includes a machine-readable reason for the latter two outcomes. The
corresponding semantic event is retained too: for example, a successful interrupt also has the
terminal `TurnCompleted`, and a successful model command states the selected model. A raw native
event remains linked as evidence rather than being flattened away.

The existing input events are a partial predecessor of this lifecycle: `InputSubmitted` means the
runner took an input, and `InputAccepted` currently means a provider-specific admission boundary.
They must evolve to the provenance-bearing `HarnessUserMessageConfirmed` receipt plus per-input
admitted/rejected settlement. The current `InputUncertain` event marks a recovery gap in the
existing protocol; it is not a terminal outcome that the Thread-command product normalizes. The
runner protocol must add command ids plus replayable receipt and settlement events before
`InterruptTurn` or `StopRunnerSession` enter the durable outbox. `switch_id` already supplies the
model command id, but does not yet supply its receipt/deferred semantics.

This is a provider-neutral command envelope, not a claim that Claude and Codex have identical
native behavior. The runner maps each provider's native request/response and later frames into this
contract, records the provider-specific facts as `Native` events, and exposes its scheduling choice
in `CommandReceived`. It must not call a native write, an RPC response, or an inferred state change
a receipt or settlement unless the pinned provider evidence supports that statement.

The journal is intentionally thin support, not a replacement provider scheduler or a claim of magic
exactly-once delivery. If the runner crashes after `dispatch_planned` but before it durably observes
the native effect, recovery uses a stable native idempotency key or an authoritative
native/transcript observation to decide whether to dispatch, wait, or settle. If neither exists for
an operation/provider, that operation has not passed the durable-command gate; the app leaves it
native/research-only rather than manufacturing an `uncertain` terminal result.

### Recovery to a terminal result is a capability gate

The desired contract has **no generic terminal “uncertain” outcome**. A runner process interruption
may leave an internal command recovery record, but recovery must eventually emit one of the terminal
settlements above. An operation is eligible for the durable Thread-command path only when its
provider implementation proves all of the following:

1. the runner durably records the command and its id before starting the native effect;
2. after a runner or harness restart, it can either repeat that exact native operation safely with
   the same id or read authoritative native state that proves whether the effect happened; and
3. it can write the correlated terminal settlement before reporting it to Agentplane.

For a model change, runner-owned next-turn selection may make this tractable, but the native timing
still needs proof. For an interrupt, the command must name the clicked `turn_id` and recovery must
observe that turn's terminal result; it may never fall through to interrupt a later turn. For input,
the pinned Claude UUID admits a queue item but is not a persistence fence, and Codex's correlated
history admission/durable queue still needs capture evidence. Neither is currently sufficient to
promise recovery to a terminal result after every crash window.

If one of those proofs is absent, keep the operation provider-native/research-only rather than
shipping it as a durable outbox command with a fabricated failure. Persisting a future Thread's
intent is still useful foundation work, but the “press Enter and it will complete after restart”
product guarantee waits on this gate.

#### Deterministic proof harness

This is testable against the pinned binaries; it is not a request to reason from packet timing.
Extend the existing Python harness tests rather than inventing a fake provider:

- `harness_tests/scripted_upstream.py`'s `next_request()` is an exact barrier where the harness has
  sent a request to the loopback LLM mock; inspect its provider-native correlation fields before
  choosing the next fault.
- `runner/test_restart.py` already starts a real runner process, kills its process group, and starts
  another runner on the same state directory. Add targeted gates before native write, after durable
  runner receipt, after the mock has observed the native effect, and after native settlement but
  before Agentplane's next observation.
- Run each script against both pinned binaries. Reattach/restart with the same input or command id,
  then assert the native transcript/history, mock request count and correlation id, runner event
  replay, final effect, and exact `HarnessUserMessage.origin_input_ids` grouping — not merely the
  runner's in-memory default.

The required crash matrix is: (1) outbox commit before any runner receipt; (2) runner receipt before
native dispatch; (3) native dispatch observed at the LLM mock before runner settlement; and (4)
runner settlement before the app copies it. The app-level version deliberately kills the delivering
replica between those points and lets a second replica reconcile the same outbox row. The runner
version kills/restarts the runner/harness pair. A candidate operation graduates only if every
reachable window converges to one terminal result and the mock/native evidence rules out a duplicate
effect. A failing window is a concrete provider capability gap to keep visible, not a reason to add
a generic `uncertain` state.

## Command kinds

| High-level command  | Desired outcome                                                         | Receipt and settlement                                                                                                                            |
| ------------------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `SubmitInput`       | A requested input becomes a proven harness user message or is rejected. | `InputSubmitted`, then `HarnessUserMessageConfirmed(origin_input_ids, text)` or a per-input rejection; eligible only after recovery is evidenced. |
| `ChangeModel`       | Future turns use the selected model after the command settles.          | Runner receipt says `now` or `at_boundary`; settle only once its model selection is active, or explain why it cannot be.                          |
| `InterruptTurn`     | Stop the clicked active turn, if it is still active.                    | Receipt targets that `turn_id`; settle with the causal interrupted `TurnCompleted`, or explicit `noop`/rejection.                                 |
| `StopRunnerSession` | Stop the harness but retain resumability.                               | Receipt, then settle with `HarnessExited` or an explicit terminal inability.                                                                      |
| `PauseThread`       | The Thread's Sandbox is suspended after any defined runner-stop policy. | Composite: settle/stop runner as specified, then Kubernetes suspend.                                                                              |
| `ResumeThread`      | The Sandbox and current runner session are usable without a new input.  | Kubernetes resume, wait for reachability; no harness restart. A successor is a later, explicit capability.                                        |

Thread creation is an atomic app write, not a runner command kind; its first `SubmitInput` starts
the outbox flow. The composer normally submits `SubmitInput`; it does not need an exposed “resume”
mode. A suspended target simply causes its prerequisites to be reconciled first. `ResumeThread`
exists for an explicit button or automation that wants a runnable Thread without sending text.

The labels are intentionally precise. “Stop current turn,” “stop harness,” “suspend Sandbox,” and
“archive Thread” affect different authorities and must not be compressed into one ambiguous Stop
button.

### Model changes across providers

`ChangeModel(command_id, model)` has one common promise: after it settles successfully, every
subsequently _started_ turn uses `model`. It deliberately makes no claim that an already-running
turn changes model. That gives one useful product meaning while preserving the providers' different
execution timing:

- Claude exposes a native set-model control path, but the recovered dispatch includes a pending/
  deferred branch and does not yet prove active-turn timing. The runner must test whether it can
  receipt as `now`, receipt as `at_boundary`, or reject it; it settles only when the native
  acknowledgement proves the setting took effect. The active turn remains recorded with the model
  it started with.
- Codex selects its model at turn start. During an active turn the runner records the command,
  receipts it as `at_boundary`, and applies it before the next turn starts. It must keep that
  pending selection durable across attachment loss and avoid starting a later turn with the old
  selection once the command is ahead of it.

The runner owns this per-session scheduler. A deferred model change is a barrier for starting later
turns, but it need not prevent a provider-supported steering input from joining the already-active
turn. This distinction avoids an app-side race between a model-picker click and an input that might
legitimately steer the current turn.

The current common `SwitchModel` contract is intentionally not that design: it rejects every active
turn in `session.py` before reaching either provider adapter. Its Codex adapter performs no native
runtime mutation; only the idle path updates the runner's stored default for a later `turn/start`.
Do not expose that present restriction as product policy or retrofit the outbox around it. First
capture/verify both native behaviors, then change the protocol atomically to the common
receipt/deferred/settlement contract above.

## Reconciliation

For each Thread, process commands in ordinal order subject to command-specific barriers. A model
change blocks a later new-turn start until it settles, while an interrupt is targeted to the turn
named at click time rather than waiting behind unrelated queued inputs. The reconciler is repeatable
and may run on every app replica:

1. Read the oldest eligible outbox command, Thread target, active `ThreadRunnerSession`, Kubernetes
   snapshot, and committed runner events.
2. Derive the missing prerequisite. For example, an input to a suspended Sandbox first needs a
   Kubernetes resume, which unfreezes the existing runner and harness. A new target first
   materializes a Kubernetes `Sandbox` CR from its resolved creation input, then finds it by its
   `thread_id` label and pins its UID. A separately stopped runner session needs `Open` from its
   immutable `ThreadRunnerSessionPlan`; the resulting `ThreadRunnerSession` is written only after
   runner proof. A lost session can use a successor only once that runner capability is implemented.
3. Perform the idempotent external operation. Do not hold a PostgreSQL transaction or row lock
   across a Kubernetes API or gRPC call.
4. Copy runner events and wake subscribers transactionally. Derive command outcome from the copied
   receipts and Kubernetes prerequisite state; re-read on every retry.
5. Finish when the authoritative observation satisfies the command or explicitly rejects it. A
   restarted runner recovers its own received-but-unsettled command before Agentplane attempts any
   new delivery; no app replica declares a made-up terminal outcome from a timeout.

Ordinary PostgreSQL row locks/unique constraints protect creation of a Thread, its target, command
ordinal, and `ThreadRunnerSession` association. They do not turn an external call into an atomic
transaction. A separate durable lease is premature while all required calls are safely repeatable:
Kubernetes create/find/resume is keyed by the stable target, runner `Open` is keyed by a stable
runner-session id, and `Input`/`SwitchModel` are keyed by their protocol ids. Add an expiring claim
only if a concrete later command has an externally observable, non-idempotent interval that cannot
be protected this way.

### Receipt, recovery, and settlement

For `SubmitInput`, “accepted by Agentplane” is the committed outbox item. “Taken by the runner” is
`InputSubmitted`. “Confirmed as a harness user message” is the provenance-bearing
`HarnessUserMessageConfirmed` record, and it may name several requested inputs. A successful gRPC
write or Claude queue admission alone is not enough. The current runner's `InputUncertain` is
evidence that this input contract has not yet met the recovery gate above. It must be eliminated by
an evidenced native deduplication/observation path before the Thread-command UI promises restart-safe
submission, not exposed as ordinary user workflow.

The same rule applies to model changes, interrupts, and runner stops: an app response starts
reconciliation; a durable runner receipt ends delivery retry; a terminal settlement is the
user-visible outcome. A runner crash after receipt but before settlement triggers runner-owned
recovery from its `RunnerCommandJournal`, using the same command id, target evidence, and observed
native correlation; it is not permission for Agentplane to silently resend an interrupt or model
change. `switch_id` is the existing model command id. Before `Interrupt` and `Shutdown` participate
in this durable path, extend their messages/events with command ids, target evidence, journaled
recovery evidence, and replayable receipt/settlement observations.

### Continuation across runner sessions

Sandbox suspension is not a runner-session or harness stop. Resuming a Sandbox unfreezes its
existing processes, so neither `ResumeThread` nor a pending input sends a native resume operation
in that case. It waits for runner reachability and then targets the existing live session.

Within one Sandbox, successor-session resume is a target capability, not an initial-delivery
prerequisite. A Thread may eventually stop or lose a harness, receive a new `runner_session_id`,
and tell that new session to resume the same Thread from its predecessor. The two sessions share the
Sandbox state volume, so the runner can resolve the predecessor's durable native continuation rather
than making the app understand Claude or Codex native identifiers.

Verify the native Claude and Codex behavior, then add an explicit runner operation with this shape
before using it in reconciliation:

```text
Open(new_runner_session_id, session_spec,
     resume_from_runner_session_id = previous_runner_session_id)
```

The runner validates that the predecessor exists in its state volume, has a compatible provider,
and belongs to the requested continuation lineage; it starts a new harness process in the new
session with the predecessor's native continuation. `Attached`/a durable runner event must identify
the source session and confirm whether the new harness actually resumed, so the app can persist a
`ThreadRunnerSession` successor only on proof, not an optimistic gRPC response. The provider-specific
native continuation remains runner-owned.

This does **not** make a received-but-unsettled command safe to replay into a successor session.
Runner `input_id` deduplication is session-scoped. The predecessor must first recover the command to
a terminal result using provider evidence; until then, successor creation is blocked rather than
silently resending it. Cross-Sandbox continuation is separate future work: it needs an exported,
provider-neutral continuation capability rather than the same-volume predecessor lookup above.

## Read model and UI

The query model combines, rather than replaces, authoritative data:

- Kubernetes snapshot: Sandbox exists/running/suspended/provisioning/deleted and Pod readiness.
- `ThreadRunnerSession` plus runner snapshot: which runner session is the current command target,
  harness state, active turn, and model.
- Thread command outbox: accepted commands and outstanding intent, joined to their runner receipts
  and settlements.
- runner Event transcript: input/model/turn/harness settlement and durable history.

A submitted composer bubble is rendered from its `SubmitInput` outbox item immediately and remains
through reload. The timeline preserves each requested input as its own operator action. Once the
runner confirms a `HarnessUserMessage`, the associated request bubbles link to one compact receipt
entry such as “harness confirmed user message ‘…’, from requests 123 and 125”; a Claude-coalesced
message never overwrites those separate requests with one misleading bubble. A lost SSE connection
causes a snapshot re-read, never discarded local-only progress. The URL changes to the pre-minted
Thread id at submission and remains that route through Sandbox provisioning, resume, and runner
replacement.

Control commands appear on that same Thread timeline as compact system entries, rather than being
hidden in an ephemeral toolbar state. An entry first says “model change requested” or “interrupt
requested”; after `CommandReceived` it says either “runner accepted” or, for example, “queued for
the next turn boundary”; only `CommandSettled` says “model changed,” “turn interrupted,” “nothing
to interrupt,” or an explicit rejection. During runner recovery it remains a pending “recovering
receipt” entry rather than becoming a made-up terminal state. The toolbar may show the current
active model and turn, but it must not erase a pending command. Timeline entries are read from the
outbox plus runner events on every reload and after every live-stream reconnect.

#### Protocol evidence is inspectable

The timeline's normal presentation is intentionally concise, but every pending or settled input and
control entry has a details affordance. It displays the app outbox command id and ordinal; runner
journal state (`accepted`, `deferred`, `dispatch_planned`, `native_effect_observed`, or `settled`);
the semantic receipt/settlement; native correlation ids; and ordered source sequence links. A linked
raw-frame view shows retained provider frames and their arrival/order metadata next to the runner's
interpreted receipt. This is a diagnostic view, not a second state machine: values come from the
reloaded app snapshot and runner event/journal projection, so reopening or reconnecting cannot
change what the user sees.

Raw frames use the existing authorization and redaction rules: enough payload and correlation to
explain a receipt, never credentials, tokens, or provider-only material the viewer is not authorized
to inspect. The high-level timeline remains the primary view; the evidence view makes its claims
auditable when provider queuing, batching, restart recovery, or control scheduling is in question.

The simple path shows a preset, prompt, and Enter. Advanced controls expose the selected existing
Sandbox, model/system-prompt/preset overrides, egress policies, and action policy sets. This does
not retire the manual Sandbox surface: direct create-without-Thread, select/open a runner session,
inspect, suspend/resume, delete, egress, and diagnostic controls remain available. Threadless
Sandboxes remain visible in the sidebar.

For the React client, introduce a server-state cache such as TanStack Query when the command API is
implemented. Query keys should separate `thread`, `thread commands`, `thread transcript`, and
`sandbox`. SSE/NOTIFY updates invalidate or replace server snapshots; mutations optimistically add
only the command the server has acknowledged. No component should own a provisioning sequence or
infer completion from a timer.

## Event storage migration

Today `trajectory.Thread` is keyed by `(sandbox, session_id)` in practice and `Event` has a
Thread-scoped sequence. That is a session-shaped implementation, not the intended domain model.

Before allowing multiple runner sessions per Thread:

1. Introduce `ThreadRunnerSession`, backfill one row per existing Thread, and make new Thread ids
   independent of a runner session. Model predecessor/successor lineage on one Sandbox.
2. Change stored event identity to `(thread_runner_session_id, runner_sequence)`. Add a
   Thread-scoped display order only if the product needs one linear transcript order; allocate it
   transactionally and allow only one active writer session per Thread. Pending command entries
   retain their Thread outbox ordinal until their correlated runner event supplies the durable
   timeline position.
3. Move feed state to the runner-session record. Derive a Thread's current state from its active
   session instead of overwriting a single Thread feed state.
4. Move list, read, archive, title, and transcript routes to Thread ids; retain old
   Sandbox/session routes as manual/diagnostic views during migration.

This is an atomic API change within the monorepo: do not leave a production code path that sometimes
treats a runner session id as a Thread id.

## Delivery plan

1. **Persist initial intent (current foundation).** Land typed existing/new Sandbox targets,
   pre-minted Thread id, initial runner-session id/spec, and first input. No lifecycle-status table
   and no browser-owned wait loop.
2. **Normalize Thread versus runner session.** Add/backfill `ThreadRunnerSession`, event/feed
   migration, Thread-id read endpoints, and tests for old transcript visibility.
3. **Implement the Thread/outbox transaction and reconciler.** New Thread plus first input, an
   input to an existing Thread, and the Sandbox-unfreeze-before-input path; use same-session `Open`
   only for a separately stopped harness. Test app restart, competing replicas, Sandbox
   delete/recreate, recovered input delivery, and lost browser response. Do not advertise the
   restart-safe input completion guarantee until its native recovery evidence passes.
4. **Define the common runner command lifecycle.** Extend the pinned Python harness scripts and
   runner/app restart tests through every crash window, then add a runner-owned command journal and
   replayable receipt/deferred/settlement events and ids atomically for the operations whose recovery
   converges. Test Claude's active-turn set-model behavior and implement the observed
   `now`/`at_boundary`/rejection outcome; implement Codex's turn-boundary scheduling and
   `InterruptTurn` against a named active turn. Do not retain today's active-turn model rejection as
   the product contract.
5. **Define successor-session continuation and runner stop.** Verify native harness resume, then
   add a successor-session resume operation plus proof event and the same receipt/settlement shape
   for `StopRunnerSession`. Keep cross-Sandbox continuation out of scope.
6. **Ship the additive UI.** One composer for new/existing Thread paths, command-backed pending
   input bubbles and control-timeline entries, including coalesced-message provenance and inspectable
   receipt/raw-frame evidence, live snapshots, and visual tests. Preserve the manual Sandbox UI
   throughout.
7. **Add higher-level controls.** Surface model change, interrupt, runner stop, and explicit
   resume/pause only after each command contract is proven.

Each step is independently reviewable. The reconciler does not wait on the unified UI, and the UI
does not directly sequence Kubernetes plus runner calls.

## Acceptance matrix

Visual and integration tests must cover at least:

- New Thread/new Sandbox after Enter, while Kubernetes is provisioning, after runner receipt, and
  after a reload at each point.
- New Thread in an existing running Sandbox.
- Input to a running Thread.
- Input to a suspended Sandbox: prompt retained while Kubernetes unfreezes the existing processes,
  then delivered to the existing runner session without a native harness resume.
- After the runner continuation contract lands: a stopped/lost harness replaced by a new runner
  session in the same Sandbox, with the same Thread transcript resumed and a recorded
  predecessor/successor relation.
- Existing transcript while the Sandbox is running, suspended, and deleted.
- An input and each supported control command through a runner/harness restart between receipt and
  settlement, recovering to the same admitted, rejected, or no-op terminal result without a second
  native effect. Every resulting harness user-message receipt preserves exact text and all ordered
  requested-input origins. A provider/operation without that proof stays outside the durable command
  UI.
- A control command before runner receipt, after receipt while deferred, and after terminal
  settlement, all surviving reload and live-stream reconnect.
- The same pending and settled entries with protocol details expanded: app outbox identity, runner
  journal stage, semantic receipt/settlement, and ordered linked raw frames survive snapshot reload
  without a locally invented state transition.
- Claude model change during an active turn, proving its actual immediate/deferred/rejected result;
  and Codex model change received during an active turn then settled at the next turn boundary. No
  later turn starts with the old model after the Codex command is ahead of it.
- An interrupt targeted at the clicked turn, including interrupted completion, already-ended/no-op,
  and provider rejection; it cannot target a later turn after recovery. Runner stop once its
  contract lands.
- A threadless manually created Sandbox alongside Thread-first rows, with its direct lifecycle
  controls still reachable.
- A disconnected/reconnected SSE client and two app replicas reconciling the same command without
  duplicate external effects.
