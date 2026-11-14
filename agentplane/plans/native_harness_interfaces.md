# Native harness interfaces at the Environment Runtime

Status: **proposal for review**, tracked as
[`NATIVE_HARNESS_INTERFACES_DESIGN`](task_dag.md#native_harness_interfaces_design--expose-native-harness-interfaces-past-the-runtime).
Nothing here is decided or built.

The operator, 2026-11-14 20:34 UTC, is considering whether the Environment Runtime should "expose
codex and Claude's interfaces (both input and output) more directly rather than having it doing
translation to history's current neutral projections (see the "history becomes journal" thing) and
translating from generic commands to harness specific commands", adding that this "would make the
responsibility for doing the translation fall probably somewhere between session dispatcher and env
runtime". History's half is
[`JOURNAL_REPRESENTATION_DESIGN`](task_dag.md#journal_representation_design--history-as-stored-journals);
this note is the Runtime and command half. Vocabulary follows the
[Journal and Thread ADR](../docs/adr_journal_and_thread.md); a **kind** is Claude, Codex or the
central sampler.

## Today

### Commands

[`Command`](../proto/command.proto) has five operations. Dispatcher exposes four as RPCs and
delivers each as a typed Runtime RPC ([`ProxyRunner.Deliver`](../runner_gateway/dispatcher/runner_client.go))
or sampler RPC ([`RoutingRunner.Deliver`](../runner_gateway/dispatcher/central_runner.go)). The
Runtime's [adapters](../env/runtime/harness/adapter.go) translate them:

| Operation               | Claude (stream-json)                                                                             | Codex (app-server JSON-RPC)                                                              |
| ----------------------- | ------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------- |
| `SubmitInput`           | user frame; compatible inputs coalesce; during a tool, joins the tool-result continuation        | `turn/start`; a second one during a turn joins it. `turn/steer`, `thread/queue/*` unused |
| `InterruptTurn`         | `interrupt` control request, which names no turn; the Runtime fences it to the turn ID it minted | `turn/interrupt` with the native turn ID                                                 |
| `ChangeModel`           | `set_model` control request; effect on its response                                              | held until the next `turn/start` response proves it; a superseded one is a no-op         |
| `ChangeReasoningEffort` | `apply_flag_settings` carrying only `effortLevel`                                                | held, like the model                                                                     |
| `Compact`               | not routed                                                                                       | not routed                                                                               |

The central sampler hosts no harness and implements all five in its own session store. `Compact` is
reachable only through the sampler's own `Compact` RPC ([`sampler.proto`](../central_sampler/api/sampler.proto)),
which Dispatcher does not route and only sampler tests call.

### Output

The Runtime journals every native frame verbatim as `Native`, both directions, and its adapters
derive [`Event`](../proto/event.proto)s from them in the same sequence:

- **Control:** `CommandAdmitted`, `CommandFailed`, `CommandNoop`, `HarnessUserMessageConfirmed`,
  `ModelChanged`, `ReasoningEffortChanged`, `TurnStarted`, `TurnCompleted`, `Harness*`, `Setup*`,
  `JournalReopened`, `ConversationReconciled` (its item IDs derive from native IDs).
- **Content:** `ItemStarted` with three item kinds (assistant text, reasoning, tool call; unchanged
  since the first runner protocol), `TextDelta`, `ToolArgumentsDelta`, `ToolArguments`,
  `ToolOutputDelta`, `ItemCompleted`.

The central sampler writes the same vocabulary directly as its own output, plus `BoundaryRecord` and
`ExecutionProgress`.

### Who reads the neutral forms

| Consumer                                                                                                      | Reads                                                                                            | Needs content?         |
| ------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ | ---------------------- |
| Runtime itself ([journal](../env/runtime/journal/journal.go), [status](../env/runtime/session/status.go))     | active turn, model and effort in the checkpoint; `idle_since` for autosuspend; interrupt fencing | no                     |
| Dispatcher                                                                                                    | only the admission receipt a delivery RPC returns                                                | no                     |
| Wake and suspend                                                                                              | Dispatcher wakes on queued work; the Sandbox Controller suspends on the Runtime's `idle_since`   | no                     |
| [Notification inbox](../notification_service/internal/store/notices.go)                                       | admitted, confirmed, failed or no-op per notice                                                  | no                     |
| [`ObserveInput`](../session_client/observe_input.go) (`observe_input`, CLI)                                   | confirmation, failure, turn status                                                               | final text             |
| [`TurnProjection`](../session_client/turns.go) (`read_turns` in session_delegation and orchestration_v1, CLI) | turns and their inputs                                                                           | tool names, final text |
| History [fold](../history_service/internal/threadfold/fold.go), synced to the app's conversation view         | everything                                                                                       | yes                    |
| History [summaries](../history_service/internal/projection/summary.go) (sidebar)                              | last turn status, `idle_since`; "model activity" recency from content Events                     | yes                    |

Dispatcher's `HARNESS_CONFIRMED` and `NO_OP` delivery states have no writer
([`DISPATCHER_HARNESS_CONFIRMATION`](task_dag.md#dispatcher_harness_confirmation--settle-admitted-inputs-on-a-canonical-confirmation)).

Harness knowledge already lives past the neutral layer. History's delta
[settlement](../history_service/internal/storage/settlement.go) matches Claude and Codex native
delta frames. The app parses Claude `Bash` and Codex `commandExecution` arguments separately
([`shell_calls.ts`](../app/frontend/threads/shell_calls.ts)). Codex `imageView` completes without a
status, so the generic projection cannot mark it successful
([`CODEX_IMAGE_VIEW_ITEM`](task_freezer.md#codex_image_view_item--project-workspace-image-view-lifecycle)).
Content is also stored three times: one 1.6 MB tool output occupies a native completion, a semantic
completion and a projected output ([storage representations](storage_representations.md#measured-targets)).

## The earlier generic interface is the current one

The first runner protocol ([#5400](https://github.com/agentydragon/ducktape/blob/7df63405c9/x/agentplane/runner/SPEC.md),
2026-09-02) set the goal: "A caller drives a session through it without knowing which harness is
behind it: the only provider-specific field it ever sets is `SessionSpec.provider`." Its event
families are today's. The same day, the common-protocol plan
([#5395](https://github.com/agentydragon/ducktape/blob/ea133fef00/x/agentplane/plans/common_protocol.md))
bounded it to behavior the scripted tests prove on both binaries: "Do not add a generic state
machine, retry policy, neutral operation projector, or timeline schema to make the matrix appear
symmetric." Later changes stayed inside that bound:

- #6522 and #6946 (2026-09-13, 09-14) replaced the input events with `Command`, `CommandAdmitted`
  and causal effect Events.
- #10950 (2026-10-17) removed `ResumeSession` and `StopRunnerSession`; sending work starts the harness.
- [A2A](../docs/a2a.md) was rejected as the harness protocol: it would hide the evidence or need
  private extensions so extensive that it no longer supplies the neutral language.
- The central sampler (#12225, 2026-10-28) adopted the vocabulary as its native output, and
  `Compact` entered the common operation set though only the sampler implements it.
- [`ENVIRONMENT_RUNTIME_NATIVE_SURFACE`](task_dag.md#environment_runtime_native_surface--an-environment-runtime-surface-unmistakably-about-native-sessions)
  was deferred on 2026-11-11.

The generic surface works at its floor and has no way above it. The harness capabilities beyond it
sit in the freezer: Codex `turn/steer` and `thread/queue/*` and Claude `cancel_async_message`
([`INPUT_DELIVERY`](task_freezer.md#input_delivery--remaining-native-queue-and-recovery-evidence)),
[`HARNESS_MANUAL_COMPACTION`](task_freezer.md#harness_manual_compaction--user-triggered-compaction-from-the-frontend),
[`HARNESS_INTERACTIVE_CONTROLS`](task_freezer.md#harness_interactive_controls--durable-park-answer-recovery)
and [`NATIVE_SUBAGENT_THREADS`](task_freezer.md#native_subagent_threads--as-linked-agentplane-threads).
Each would need either a common abstraction the evidence does not support, which
[common protocol](../docs/common_protocol.md#harness-differences-remain-evidence) forbids, or a
per-kind command path, which does not exist.

## The neutral floor

Routing and admission never read harness output: Dispatcher routes by the Session record, its
placement and the live Identity. The floor is what the platform itself acts on. Names are today's
unless marked new; fields are a sketch.

**Commands in:** `SubmitInput{text}`, `InterruptTurn{turn_id}`, `ChangeModel{model}`,
`ChangeReasoningEffort{effort}`, each with a caller-chosen `command_id`. Everything else is per kind.

**Facts out**, written by the Runtime adapters or the central sampler:

| Fact                                                                         | Fields                                                                           | Read by                                                                        | Why it cannot stay native                                                                                              |
| ---------------------------------------------------------------------------- | -------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------- |
| `CommandAdmitted`                                                            | the whole `Command`                                                              | Dispatcher (`RUNNER_ADMITTED` receipt), notices, fold, `observe_input`         | It is the Runtime's own journal commit; no harness frame exists.                                                       |
| `CommandSettled` (new; see [below](#did-the-command-execute))                | `command_id`, outcome `EFFECTED`/`FAILED`/`NOOP`, `reason`, `turn_id` for inputs | the Runtime's pending set, notices, fold, `observe_input`                      | Mapping a native request back to its commands lives only in the Runtime journal's private `native_request` tables.     |
| `TurnStarted`, `TurnCompleted`                                               | `turn_id`, `model`; `status`, `error`                                            | interrupt fencing, `idle_since`, `read_turns`, `observe_input`, sidebar status | The Runtime mints Claude's turn IDs; Claude's protocol has none.                                                       |
| `ModelChanged`, `ReasoningEffortChanged`                                     | applied value and previous value                                                 | the journal checkpoint, the model picker                                       | Codex applies at the next `turn/start`, Claude on a control response; the checkpoint commits atomically with the fact. |
| `HarnessStarted`/`Exited`/`Lost`/`LaunchFailed`, `Setup*`, `JournalReopened` | pid, resumed, exit code, setup output                                            | Runtime status, journal view lifecycle rows, recovery                          | Process facts the harness cannot report about itself.                                                                  |
| `ConversationReconciled`                                                     | `turn_id`, per native item ID: retained, absent, unknown or revised              | journal view badges after process loss                                         | Compares what the Runtime journaled with what native history kept after a restart.                                     |
| `Native`                                                                     | direction, exact line                                                            | journal view, decoders, debugging                                              | It is the native form.                                                                                                 |

Leaving the floor: `ItemStarted`, `TextDelta`, `ToolArgumentsDelta`, `ToolArguments`,
`ToolOutputDelta`, `ItemCompleted`. `CommandSettled` absorbs `HarnessUserMessageConfirmed`,
`CommandFailed`, `CommandNoop` and the command IDs on `ModelChanged`, `ReasoningEffortChanged` and
`TurnCompleted`.
The sampler's `BoundaryRecord` and `ExecutionProgress` are its kind's own records.

### Did the command execute?

Tracked today, per layer:

- **Dispatcher:** `QUEUED_IN_DISPATCHER` → `DELIVERING_TO_RUNNER` → `RUNNER_ADMITTED`, which stores
  the Runtime's `CommandAdmitted` receipt, or `FAILED`, `CANCELLED_IN_DISPATCHER`, `UNKNOWN`. The
  `HARNESS_CONFIRMED` and `NO_OP` states have no writer.
- **Runtime:** its journal's `command` row records admission, whether a native write was planned,
  and a terminal cursor. Five signals settle a command: `HarnessUserMessageConfirmed` (inputs),
  `ModelChanged`, `ReasoningEffortChanged`, `TurnCompleted.interrupted_by_command_id`, and
  `CommandFailed`/`CommandNoop`. An unsettled command keeps the Sandbox busy (`idle_since`), and
  after a restart a dispatched but unsettled command stays pending rather than being resent.
- **Readers:** the History fold collapses the five into `PENDING`/`EFFECTED`/`FAILED`/`NOOP`
  (`ThreadCommandOutcome`), which drives the app's last progress light ("Agent confirmed message",
  "Applied"). The Notification inbox keeps one notice in flight until it is confirmed or failed
  ([`NOTIFICATION_COMMAND_OUTCOME_DESIGN`](task_dag.md#notification_command_outcome_design--notice-delivery-without-reading-conversation-history)).
  `observe_input` reports `input_confirmed`, then the correlated turn.

"Confirmed" already means different things per kind. Claude's evidence is the model response's
`message_start` naming the input, or the tool-result cohort it joined. Codex's is the `turn/start`
response; an input joining a running turn sits in that turn's pending input until the current
model response and its tool calls finish ([Codex notes](../docs/harness_protocols.md#tools-steering-and-interrupt-1)).

Options:

1. **Keep it as today:** five settling signals and one "confirmed" whose meaning differs by kind.
2. **One settlement in the floor (recommended):** `CommandSettled`, defined by the Runtime's own
   boundary, "the harness took it and the Runtime holds no more work for it", not "it reached the
   model". Same evidence as today; readers stop mapping five signals; the fold's outcome enum
   becomes the producer's. Whether the model saw it is a per-kind reading of `Native`.
3. **Admitted only:** drop settlement from the floor. The Runtime still computes it for idle and
   restart, so nothing is saved there; notices lose their pacing gate; the app light and
   `observe_input` need per-kind decoders plus a newly published native-request-to-command map.

Settlement is produced only where that map lives: the Runtime adapters, and the sampler for its
Sessions. Readers keep getting it from History. Whether the Dispatch record also carries it, so
notices and `observe_input` stop scanning History, stays with
[`DISPATCHER_HARNESS_CONFIRMATION`](task_dag.md#dispatcher_harness_confirmation--settle-admitted-inputs-on-a-canonical-confirmation).

## Options

- **A. Grow the neutral surface.** Add per-harness operations to the common set as `Compact` was,
  and keep content Events. Cheapest; keeps the triple storage and the leaks above, and each new
  capability bends the common set.
- **B. The neutral floor above, native content, per-kind commands.** Described below.
- **C. Raw pass-through.** Callers send native frames; the Runtime journals and forwards them; every
  consumer decodes per kind. It loses the control floor: nothing could fence a Claude interrupt
  without Runtime-minted turn IDs, and notices, `observe_input` and `idle_since` lose their facts.
  Codex's app-server is shared by the Runtime's Sessions, so the Runtime demultiplexes frames
  anyway. A caller-supplied frame can carry any native request, including Claude's
  `apply_flag_settings`, whose payload is a settings object, so the Runtime must allowlist anyway:
  B's typed per-kind set under another name.

### B concretely

**Commands.** `Command.operation` keeps the four operations every kind implements with an effect
fact: `SubmitInput`, `InterruptTurn`, `ChangeModel`, `ChangeReasoningEffort`. Beside them, one oneof
of `ClaudeCommand`, `CodexCommand` and `SamplerCommand`, each a closed set of typed operations the
adapter maps one-to-one onto a native request, for example Codex `TurnSteer` and `ThreadQueueAdd`,
Claude `CancelAsyncMessage`, sampler `Compact`. Codex types keep coming from the pinned binary's
generated schema ([`codex/protocol`](../env/runtime/internal/harnesses/codex/protocol/BUILD.bazel)).
A per-kind operation gets an effect Event once its native evidence is pinned, as today.

**Output.** The adapters keep the control floor and `Native`, and stop emitting content Events. The
item projection code (Claude's stream-event and assistant item handling, Codex's
`projectItemStarted`/`projectItemCompleted`) moves into a per-kind decoder library that readers
share. The sampler's content Events become its kind's own records.

**Order.** Readers first: the journal view from `JOURNAL_REPRESENTATION_DESIGN`, then per-kind
final-text and tool-name decoding for `read_turns`, `observe_input` and summaries. Only then do the
adapters stop emitting content. Per-kind commands are independent of the output half.

### Where translation lands in the two-binary shape

| Responsibility                                         | Owner                                                                          | Why                                                                                                                                                                                                               |
| ------------------------------------------------------ | ------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Native framing of commands; deriving the control floor | Environment Runtime adapters                                                   | A frame is journaled before it is written, its response correlated, fenced on the active turn, recovered after a crash from native files on the Sandbox volume, and demultiplexed from Codex's shared app-server. |
| Checking a per-kind command against the Session's kind | `session_dispatcher`, at admission                                             | It owns the Session record, which names the harness, and the queue: a Codex command for a Claude Session is refused before it waits behind a wake. Otherwise it queues the typed command as it is.                |
| Nothing                                                | `environments`                                                                 | It forwards Runtime calls and keeps no per-Session state; harness knowledge there would give a pass-through proxy state it has no use for.                                                                        |
| Rendering content                                      | Readers, through the decoder library: History's journal view, `session_client` | They already carry the per-kind knowledge that leaked. Dispatcher reads no Events today and need not start.                                                                                                       |

So the input side splits between `session_dispatcher` (kind check) and the Runtime (framing), and
`environments`, the component between them, takes none of it. The output side moves to readers.

### For and against B

For:

- Harness capabilities get a path without invented symmetry.
- Content is stored once, natively, once the fold goes.
- Per-kind knowledge lives in one decoder library per kind instead of the adapter, History
  settlement and the app.
- A harness pin that changes content shapes stops being a Runtime change.
- The Journal is then exactly what the harness emitted, as the ADR defines it.

Against:

- A caller mixing kinds, such as an agent delegating through `session_delegation`, switches on kind
  for anything above the floor; per-kind commands need per-kind tools or stay app-only.
- Read-time decoders must handle frames from every stored pin, where today only the live adapter
  must understand the current one. The scripted harness tests must cover the decoders.
- `read_turns` and `observe_input` gain per-kind code for final text and tool names.
- It spans the Runtime, History, the app and `session_client`, and its output half waits on
  `JOURNAL_REPRESENTATION_DESIGN`, which is unscheduled.
- The hardest Runtime code stays: input confirmation, recovery and turn fencing are all control.

## Recommendation

B, with the [floor above](#the-neutral-floor) and settlement option 2, as two independent halves. Input: add the per-kind command oneof; its first slice moves `Compact`
into `SamplerCommand`, proving the shape with no new native behavior, and later operations arrive
with whichever freezer capability is selected. Output: decide together with
`JOURNAL_REPRESENTATION_DESIGN` and land after its journal view. Renaming or splitting the Runtime's
gRPC surface stays with `ENVIRONMENT_RUNTIME_NATIVE_SURFACE`.

**Question for review:** adopt B: a neutral control floor, per-kind typed commands checked by
`session_dispatcher` and framed by the Runtime, and native content rendered by readers?
