# Native harness interfaces at the Environment Runtime

Status: **proposal for review**, tracked as
[`NATIVE_HARNESS_INTERFACES_DESIGN`](task_dag.md#native_harness_interfaces_design--expose-native-harness-interfaces-past-the-runtime).
Nothing here is decided or built. The operator, 2026-11-16 06:58 UTC: the Runtime's translation is
the status quo "so it would stay by default", and no migration is planned "within next say week or
so".

The operator, 2026-11-14 20:34 UTC, is considering whether the Environment Runtime should "expose
codex and Claude's interfaces (both input and output) more directly rather than having it doing
translation to history's current neutral projections (see the "history becomes journal" thing) and
translating from generic commands to harness specific commands". On 2026-11-16 06:38 UTC the
operator named two coherent shapes, with no point in between: "(a) env runtime just forwards the
harness's own protocol and does not to basically any massaging, or (b) env runtime does all the
massaging like it does today"; a harness-neutral shared commands layer, if there is one, raises
"whether it should live in env runtime or in e.g. environment service or session dispatcher".
At 06:58 UTC the operator added that the abstraction "is leaky and would get more so if we want to
allow more fancy features ever", and that "as long as i want agentplane to provide one webui that
works across harnesses, i will need to make something that takes neutral commands and talks to
codex/claude, question would just be where is would go". History's half is
[`JOURNAL_REPRESENTATION_DESIGN`](task_dag.md#journal_representation_design--history-as-stored-journals);
this note is the Runtime and command half, which the operator calls "the input side of the same
thing" as a user-facing Thread that is a managed object rather than a projection of the harness log.
Vocabulary follows the
[Journal and Thread ADR](../docs/adr_journal_and_thread.md); a **kind** is Claude, Codex or the
central sampler.

## Today

### Commands

[`Command`](../proto/command.proto) has five operations. Dispatcher exposes four as RPCs and
delivers each as a typed Runtime RPC ([`HarnessHostClient.Deliver`](../session_dispatcher/dispatcher/runtime_client.go))
or sampler RPC ([`SessionDeliveryRouter.Deliver`](../session_dispatcher/dispatcher/central_delivery.go)). The
Runtime's [adapters](../env/runtime/harness/adapter.go) translate them:

| Operation               | Claude (stream-json)                                                                             | Codex (app-server JSON-RPC)                                                              |
| ----------------------- | ------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------- |
| `SubmitInput`           | user frame; compatible inputs coalesce; during a tool, joins the tool-result continuation        | `turn/start`; a second one during a turn joins it. `turn/steer`, `thread/queue/*` unused |
| `InterruptTurn`         | `interrupt` control request, which names no turn; the Runtime fences it to the turn ID it minted | `turn/interrupt` with the native turn ID                                                 |
| `ChangeModel`           | `set_model` control request; effect on its response                                              | held until the next `turn/start` response proves it; a superseded one is a no-op         |
| `ChangeReasoningEffort` | `apply_flag_settings` carrying only `effortLevel`                                                | held, like the model                                                                     |
| `Compact`               | not routed                                                                                       | not routed                                                                               |

The central sampler hosts no harness and implements all five in its own session store. `Compact` is
reachable only through `SamplerAdmin.Compact` ([`sampler_admin.proto`](../sampler/api/sampler_admin.proto)),
which Dispatcher does not route.

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

### What the Runtime decides beside the process

Beyond framing one request per command, the adapters decide things that need the harness's output in
order with the Runtime's own writes. The per-kind packages are about 2,800 lines of Go.

- **Turn identity and interrupt fencing.** Claude has no turn IDs, so the Runtime mints them.
  Journal admission checks an interrupt's turn against the active turn in the same transaction,
  because Claude's `interrupt` acts on whatever turn is running.
- **Input placement.** Claude inputs coalesce or join a tool-result continuation; a Codex input
  during a turn joins it. `BlocksCommandDispatch` holds later commands until a native response
  advances the current one.
- **Deferred settings.** Codex model and effort changes wait for the next `turn/start` response.
- **Harness requests.** The Runtime allows Claude's `can_use_tool` and refuses other Claude control
  requests and every Codex server request
  ([Claude](../env/runtime/internal/harnesses/claude/adapter.go), [Codex](../env/runtime/internal/harnesses/codex/adapter.go)).
- **Settlement and restart.** The journal maps each native request to its commands
  ([`native_requests.go`](../env/runtime/journal/native_requests.go)); a dispatched but unsettled
  command stays pending after a restart rather than being resent.
- **Busy.** An active turn or pending command blocks autosuspend ([`status.go`](../env/runtime/session/status.go));
  the Sandbox Controller suspends only on the Runtime's `idle_since`.
- **Recovery.** After process loss the Runtime compares its journal with the harness's continuation
  files ([Claude](../env/runtime/internal/harnesses/claude/history.go), [Codex](../env/runtime/internal/harnesses/codex/history.go))
  and emits `ConversationReconciled`.
- **Shared Codex app-server.** One app-server serves every Codex Session in the Sandbox. The Runtime
  mints request IDs unique across Sessions and routes each frame by thread
  ([`router.go`](../env/runtime/internal/harnesses/codex/router.go)).

### Who reads the neutral forms

| Consumer                                                                                                                        | Reads                                                                                            | Needs content?         |
| ------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ | ---------------------- |
| Runtime itself ([journal](../env/runtime/journal/journal.go), [status](../env/runtime/session/status.go))                       | active turn, model and effort in the checkpoint; `idle_since` for autosuspend; interrupt fencing | no                     |
| Dispatcher                                                                                                                      | only the admission receipt a delivery RPC returns                                                | no                     |
| Wake and suspend                                                                                                                | Dispatcher wakes on queued work; the Sandbox Controller suspends on the Runtime's `idle_since`   | no                     |
| [Notification inbox](../notification_service/internal/inbox/notices.go)                                                         | admitted, confirmed, failed or no-op per notice                                                  | no                     |
| [`ObserveInput`](../action_service/internal/sessionclient/observe_input.go) (`observe_input`)                                   | confirmation, failure, turn status                                                               | final text             |
| [`TurnProjection`](../action_service/internal/sessionclient/turns.go) (`read_turns` in session_delegation and orchestration_v1) | turns and their inputs                                                                           | tool names, final text |
| History [fold](../history_service/internal/threadfold/fold.go), synced to the app's conversation view                           | everything                                                                                       | yes                    |
| History [summaries](../history_service/internal/projection/summary.go) (sidebar)                                                | last turn status, `idle_since`; "model activity" recency from content Events                     | yes                    |

The Dispatcher reads no Events and records nothing past `RUNNER_ADMITTED`; its unused confirmation
states were deleted rather than given a writer.

The neutral operations already differ in meaning per kind: Claude applies a model change on its
control response, Codex at the next `turn/start`, and "input confirmed" is Claude's model response
naming the input but Codex's `turn/start` response.

Harness knowledge already lives past the Runtime. History's delta
[settlement](../history_service/internal/storage/settlement.go) matches Claude and Codex native
delta frames. The app parses Claude `Bash` and Codex `commandExecution` arguments separately
([`shell_calls.ts`](../app/frontend/sessions/shell_calls.ts)). Codex `imageView` completes without a
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

The generic surface works for the four operations every kind shares and has no way past them. The
harness capabilities beyond it sit in the freezer: Codex `turn/steer` and `thread/queue/*` and Claude
`cancel_async_message`
([`INPUT_DELIVERY`](task_freezer.md#input_delivery--remaining-native-queue-and-recovery-evidence)),
[`HARNESS_MANUAL_COMPACTION`](task_freezer.md#harness_manual_compaction--user-triggered-compaction-from-the-frontend),
[`HARNESS_INTERACTIVE_CONTROLS`](task_freezer.md#harness_interactive_controls--durable-park-answer-recovery)
and [`NATIVE_SUBAGENT_THREADS`](task_freezer.md#native_subagent_threads--as-linked-agentplane-threads).
Each would need either a common abstraction the evidence does not support, which
[common protocol](../docs/common_protocol.md#harness-differences-remain-evidence) forbids, or a
per-kind command path, which does not exist.

## (a) The Runtime forwards the native protocol

The Runtime keeps launching and supervising the harness, journaling every frame verbatim, delivering
journals to History and routing the shared Codex app-server's frames by thread. Command framing,
Event derivation and everything under
[What the Runtime decides](#what-the-runtime-decides-beside-the-process) leave it.

### Where the shared commands layer would sit

| Home                                                    | Under (a)                                                                                                                                                                                                                                                                                |
| ------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `session_dispatcher`                                    | Fits. It owns the Session record, which names the harness, the queue, and the per-Session delivery lease, so it is already the one writer per Session. It also routes central Sessions to the sampler ([`SessionDeliveryRouter`](../session_dispatcher/dispatcher/central_delivery.go)). |
| `environments`                                          | Does not fit. Any replica forwards any Runtime call and keeps no per-Session state ([Runtime proxy contract](../environments/runtimeproxy/SPEC.md)), and central Sessions never pass through it.                                                                                         |
| Each caller (app, notifications, delegation tools, CLI) | Every caller would reimplement each kind's driver, and none is the single writer that fencing and settlement need.                                                                                                                                                                       |

So the input half lands in `session_dispatcher`, and rendering content lands in readers (History's
journal view, `session_client`).

### What moves to `session_dispatcher`

It becomes a live protocol driver per Session. It would:

- read each Session's native output as it happens, from the Runtime or History; today it reads no
  Events;
- keep turn state and the native-request-to-command map in PostgreSQL, and read the Runtime journal
  back after a Runtime restart to learn which frames reached the harness;
- answer each Claude `can_use_tool`;
- send the handshake (`initialize`, `thread/start` or `thread/resume`) whenever the Runtime starts a
  harness;
- tell the Runtime while a turn runs, for example through a keep-alive hold, since the Runtime no
  longer knows;
- allowlist native requests, since one frame can carry anything the harness accepts (Claude's
  `apply_flag_settings` takes a whole settings object).

### For and against (a)

For:

- Translation fixes roll with a Deployment. Today an adapter change reaches a Session only when its
  Sandbox next starts on the new template image.
- A harness feature needs a Dispatcher change, not a Runtime image.
- The Runtime journals only native frames and its own process records.

Against:

- Decisions taken against the live stdout move to a reader with a delayed view. An interrupt decided
  against Claude turn T can land on the turn after it.
- A running turn now needs the Dispatcher: tool permissions wait for its answer and the next
  command waits for it to see settlement. A Dispatcher roll or outage stalls turns, where today it
  only delays new deliveries ([roll-independence](../docs/principles.md#components-roll-and-fail-independently)).
- If its busy hold lapses while the Dispatcher is down, the Sandbox can suspend mid-turn.
- The Dispatcher gains a live dependency on Runtime or History output.

## (b) The Runtime translates everything (today)

The shared layer stays in the Runtime adapters, and in the central sampler for its own Sessions.
`session_dispatcher` routes by placement and never reads harness output; `environments` forwards.
Nothing moves.

What (b) still needs:

- **Harness-specific commands.** A frozen capability enters as an operation the Runtime frames: a
  common one where every kind has the same meaning, as `Compact` did, otherwise one typed for its
  kind. `session_dispatcher` can refuse an operation of another kind from the Session record, as
  validation rather than translation.
- **One settlement event** (optional, independent). `CommandSettled` (`command_id`, outcome
  `EFFECTED`/`FAILED`/`NOOP`, `reason`, `turn_id`) replaces the five signals that settle a command today:
  `HarnessUserMessageConfirmed` for inputs, `ModelChanged`, `ReasoningEffortChanged`,
  `TurnCompleted.interrupted_by_command_id` and `CommandFailed`/`CommandNoop`. It fires where the
  journal already marks a command terminal, so no meaning changes; `HarnessUserMessageConfirmed`
  stays as the input fact. Readers (History fold, notices, `session_client`, `orchestration_v1`)
  accept it first and keep the old events while any Sandbox runs an older Runtime image; then the
  sampler and the template image change.
- **Content.** The Runtime keeps deriving content Events beside `Native`. A reader that needs more
  than the projection, such as the app's shell-call parsing or History's delta settlement, reads
  `Native`. Which of the two History stores is `JOURNAL_REPRESENTATION_DESIGN`'s choice.

Against (b): an adapter fix reaches a Session only when its Sandbox restarts, and every harness
feature needs a Runtime image.

## A feature that leaks further: Agentplane-hosted tools

The operator's example of a feature worth wiring in is Agentplane-hosted tools offered to the model
directly, not only through the `agentplane` CLI. Each harness has two routes
([driver tools](../docs/driver_tools.md)):

- **An MCP server the harness connects to itself.** No translator is involved; the harness speaks
  MCP to the server.
- **Tools the driver hosts.** Claude declares them in `initialize` and sends each MCP message as an
  `mcp_message` control request; Codex takes `dynamicTools` on `thread/start` and sends each call as
  an `item/tool/call` server request. The Runtime declares neither today and refuses both requests.
  Whoever answers sits inside the turn: Claude times a call out when the driver sets a per-server
  timeout, but Codex has no deadline, so an unanswered call wedges the turn. The two also differ on
  naming, failure reporting and whether the tool set can change mid-session, so a common
  declaration covers only the call round trip.

Under (b) the Runtime answers next to the process and can impose its own deadline. Under (a) the
Dispatcher answers, so a Dispatcher outage wedges Codex turns that are mid-call.

## Which harnesses run in a Sandbox

The operator, 2026-11-16 06:58 UTC: Codex in a Sandbox "is not super important", since OpenAI lets
our own code call Codex's backend API and the central sampler can do that; Claude officially
expects its CLI for subscription quotas. So the in-Sandbox translator that has to last is Claude's,
while the central sampler already takes the neutral operations as its own interface.

## Recommendation

(b), with the shared layer in the Runtime and in the central sampler for central Sessions.
`environments` hosts it under neither shape.

The Runtime is the one component that sees the harness's output in order with its own writes, holds
the process and its continuation files, and keeps running through control-plane rolls. Under (a)
those duties do not go away; they move to `session_dispatcher`, which would have to read output live,
answer harness requests and keep Sandboxes awake, so each Dispatcher roll would stall running turns.
(a)'s gain, adapter fixes without a Sandbox restart, does not outweigh that. Harness-specific
features enter (b) as Runtime-framed operations typed per kind, so the common set holds only what
the cross-harness web UI needs. Driver-hosted tools are the first feature likely to force the
question, because their answers sit inside a running turn; revisit when one is wanted.

**Open question:** where the translator from neutral commands to each harness lives long-term: the
Runtime (b, today) or `session_dispatcher` (a). Nothing is needed before the next feature that
crosses the common set.
