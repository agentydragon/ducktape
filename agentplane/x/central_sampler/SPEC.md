# Central sampler contract

## Identity and admission

`Sampler` is a separate authenticated gRPC API. `OpenSession` binds a canonical public Session UUID
to a central `SessionSpec`, caller ServiceAccount UID and fixed `SandboxDestination` plus expected
incarnation UID. The Dispatcher uses its one configured central sampler target. Session specs,
including browser, Actions, and Dispatcher opening inputs, contain no service selection. Model and effort must be server-configured. Every RPC
uses TokenReview and the allowed-account policy; Session UUID knowledge grants no authority.
Reusing an identifier with a different payload conflicts. The integration app owns neither this
admission nor the continuation database.

`SubmitInput`, `ChangeModel`, `ChangeReasoningEffort`, `InterruptTurn` and `Compact` admit durable,
idempotent commands. A returned admission is not execution completion. `GetCommand` exposes its
outcome; `FollowEvents` replays committed evidence. `GetSession` and `ListSessions` provide persisted
inventory without the executor or History. Interrupts target the current turn and bypass ordinary
queued input while work is running. Compaction preserves standing instructions and opaque provider
continuation, replacing context transactionally with the compaction command outcome.
Each interrupt request also records its target turn. Completing or failing that turn settles its
requests atomically, so a late interrupt cannot cancel the next queued turn under the same owner.

Input submitted while an input turn runs steers that turn, as in Codex: at the next sampling step,
after the previous step's tool outputs and queued `notify()` outputs, the queued `SubmitInput`
commands at the head of the queue join the turn in admission order. Each becomes a user message in
the conversation, a `HarnessUserMessageConfirmed` with the active turn's ID and an `APPLIED`
command outcome, in one transaction, so it is recorded exactly once. A turn whose model just
answered continues instead of ending. Input admitted behind another kind of command waits for a later
turn, and a pending interrupt of the turn takes precedence. While the model waits on a running
`exec` or `wait` observation, queued input asks the host to yield it (`operation/yield`) so the cell
keeps running and the model reads the input at once. As in Codex, `yield-observation` is offered as an
optional capability and used only when the host acknowledges it; otherwise the input waits for the
observation to return. **Deviation:** Codex also cuts an in-flight provider stream for new input; the sampler
lets the sampling request finish, because a cut stream leaves its intent uncertain. Codex keeps
`notify()` outputs and user input in one arrival-ordered queue; the sampler records a step's
`notify()` outputs before its steering input.

Dispatcher routes `HARNESS_CENTRAL_SAMPLER` directly to a configured sampler. Its native ProxyRunner
continues to serve wrapped Codex/Claude. There is no central-to-native fallback. Initial open
requires a ready incarnation; later central commands do not wake or wait for executor readiness.
V1 fixes the binding and does not expose executor replacement.

Non-empty setup scripts (at most 65,536 UTF-8 bytes) execute once through the bound exec-server
with `/bin/sh -eu -c`, before any provider request or queued command. Setup stdout and stderr
retain separate byte streams and lifecycle events in History. A failed or interrupted setup blocks
continuation. A durable attempt marker prevents replay after restart, including an uncertain lost
completion acknowledgement. Successful setup is not repeated when later commands attach.

## Runtime and effects

One shared service runs the provider loop and owns one lazy upstream Codex JavaScript host process.
The host starts when the first code-mode Session needs it and stays up until runtime shutdown or
host failure. It multiplexes logical Session IDs over one reader/writer, with globally allocated operation and
delegate IDs; each Session retains its own executor/delegate and process-handle namespace. Each
sampler Session has a stable upstream protocol-session ID across idle drivers and lease generations.
Protocol-session ownership is separate from the active owner and executor binding. Each code-mode
cell runs in a fresh V8 isolate/thread with no JavaScript globals shared across cells; provider
continuation stays in the sampler. Interrupting one Session does not stop the
shared process. A host failure fails pending work for every attached Session. Future opens can start
a new host, but failed in-flight provider or executor work is never replayed.

Pod CPU and memory limits are aggregate bounds for the Go service and the shared host, so container
exhaustion can interrupt all live Sessions. `store()` / `load()` values and live JavaScript cells
remain process memory and cannot be restored after host exit. Completed-cell outcomes and provider
continuation are checkpointed in PostgreSQL. Lost live cells and unresolved effects block automatic
continuation; executor operations whose transport outcome is unknown are settled as abandoned and
remain inspectable without blocking the JavaScript cell, model step, or Session. Model-facing `exec`
and `wait`, yielded process handles, stdin and external interrupts use the pinned upstream protocol.
Process handles inherit upstream's detached retention limit: pinned Codex 0.162.1 retains a
disconnected native session for 30 seconds, then expires it and terminates processes still running
in that session. This is upstream behavior, not sampler or adapter persistence. The adapter's gRPC
`OpenSession` stream owns its local WebSocket connection to that Codex session; stream lifetime and
native-session lifetime are distinct. Closing the stream closes the WebSocket without issuing a
process-termination request.

`notify(value)` follows Codex: the host's `notification/delivered` acknowledgement is sent only after
the text is durably queued for the active turn; without one it is rejected as "no active turn", and
blank text is a no-op. Queued outputs become extra `custom_tool_call_output` items for the emitting
`exec` call, after the current step's own tool outputs and before the next provider request, with
Codex's 12,000-token middle truncation. Outputs queued after the last step are appended when the
turn completes; an interrupted or effect-uncertain turn discards them, as Codex does on abort.
**Deviation:** a failed turn also discards them, because its unresolved intent must precede any
later output. Boundary evidence records each output as `recorded` or `discarded`.

For routes configured with `exec_encoding: freeform`, the model sees Codex's Code Mode Only
surface at the pinned version: freeform `exec` (Lark grammar,
optional `// @exec:` pragma with `yield_time_ms` and `max_output_tokens`) described by Codex's
template with a TypeScript declaration per nested tool, and function `wait` with Codex's schema
and defaults. Results carry Codex's status header and token-budget truncation. Deviations: the
`exec` description omits Codex's deferred-tool guidance and shared MCP TypeScript types until the
sampler has MCP/Action tools and tool search or deferred tools; `wait`
accepts only a live cell this Session recorded (rejected before dispatch rather than reported as a
missing cell), unknown or `null` tool arguments are rejected, and audio truncation estimates
tokens from data URL size rather than decoded duration.

A flat top-level `models` map allowlists model route IDs. Each entry allows reasoning efforts,
declares `api: responses`, and sets independent `responses.dialect` (`lite` or `standard`) and
`exec_encoding` (`freeform` or `json`) settings. A single top-level `llm_proxy_url` and credential
source configure the connection;
upstream routing belongs to the proxy. Unsupported APIs and missing or unknown
protocol settings are rejected. Only Responses is implemented; future adapters own their native
wire and continuation formats. JSON encoding exposes a single
function `exec`. Exactly one of `code` (execute JavaScript) or `cell_id` (resume a yielded cell)
is required; both accept optional `yield_time_ms` and `max_output_tokens`, and only the resume
variant accepts `terminate`. JSON source does not accept the native exec pragma. The adapter
validates arguments before dispatch and uses the same host operations, journal and recovery
rules as freeform calls. Calls are accepted only with their configured name and item type.
Model changes cannot change API, dialect or exec encoding within a Session. Route contracts must remain fixed
for retained Sessions; changing a contract requires a new route ID. Model changes require the
target to allow the current effort; effort changes must be allowed by the current model.
Admission validates the projected model and effort after earlier queued commands under the
Session transaction lock.

Lite mode retains the Codex wire contract described below. Standard mode sends top-level `tools`,
omits the Lite header, `additional_tools`, private client metadata and `reasoning.context`, and
requests reasoning only when an effort is selected. The standard request adapter emits text-only
function results as strings, preserving multimodal content arrays and opaque provider items.
In standard mode or with JSON exec encoding, repeated `notify()` outputs for a completed call
become asynchronous developer messages identifying the original call, without modifying the
durable Codex transcript. Standard mode has no compaction
implementation: explicit compaction is rejected at admission, and automatic compaction fails the
turn before sending a private provider request.

Streamed `response.output_item.done` items are authoritative for execution and
continuation, matching Codex. `response.completed.output` is retained as evidence
but does not replace or supplement them: Antigravity through LiteLLM can rewrite
reasoning and add terminal-only messages.

Nested `exec_command` and `write_stdin` match Codex 0.162.1 unified exec for one remote environment:
upstream schemas and result objects (`chunk_id`, `wall_time_seconds`,
`exit_code`, numeric `session_id`, `original_token_count`, `output`), yield bounds, `max_output_tokens`
truncation and the unified exec environment overlay. Every executor process (nested tools and the
setup script) inherits the executor's environment under Codex's default shell environment policy,
with the Session's variables set over it, as wrapped Codex's commands do. Commands run in the executor-reported shell, as
a login shell unless `login: false`; a requested `shell` must match it, and an executor that reports
none falls back to `/bin/sh`. Processes have no stdin pipe without `tty`; there a sole Ctrl-C
interrupts. Session numbers are random in upstream's range and unique among a conversation's
processes until their exit is reported. Argument and session errors reject the nested call in
JavaScript instead of failing the Session, as do exec-server rejections, which have no effect.

**Deviation:** `exec_command` omits upstream's approval parameters (`sandbox_permissions`,
`justification`, `prefix_rule`); Agentplane's Sandbox, egress and Action Service own those
boundaries, and sampler approvals are out of scope.

TODO: Revisit the approval parameters upstream `exec_command` includes if the sampler gains an
approval flow.

TODO: Evaluate a stronger kernel-enforced isolation boundary for cells or Sessions if aggregate
container limits prove insufficient.

The provider boundary uses a small typed adapter for Codex Responses requests, streamed events,
terminal responses, items and content parts. The public OpenAI SDK types do not model Codex's
`additional_tools` input item or the private `remote_compaction_v2` request and response items, so
the sampler constructs those wire shapes directly. Buffered SSE framing retains events as raw
JSON. Provider-owned fields remain opaque on items, content parts and terminal responses; durable
continuation preserves image, audio, compaction and reasoning data and numeric precision.
Provider requests ask for `reasoning.summary: "auto"`, as wrapped Codex does for Agentplane's model
routes; summary text streams to History as the reasoning item's text, its parts joined by newlines.
Compaction requests carry the same summary and context but no effort.

Database-clock leases and generations fence stale sampler writes. They cannot atomically fence an
external shell effect: a dispatched operation with no observed result is uncertain. The persisted
intent/result journal distinguishes this from an operation known not to have been dispatched.
Graceful shutdown cancels execution and waits for ownership acquisition and release to finish;
a restarted replica can claim immediately without waiting for the old lease to expire.
Executor failure leaves continuation and retained conversation outside the failed Sandbox.

Explicit provider rejections, terminal provider failures and model-tool validation errors before
JavaScript dispatch record a rejected intent. When no unresolved effects or live execution remain,
the command and turn fail without blocking the Session; subsequent input may continue without replaying the failed
operation. Accepted tool calls that were never dispatched receive truthful failure outputs.
The original driver error is retained in command and turn failure reasons and, for blocked sessions,
in `GetSession.blocked_reason`.
Unresolved transport loss, streams without a terminal response and uncertain execution remain
blocking failures. A transport failure in a nested executor call is instead returned as a rejected
JavaScript tool promise with the exec-server method, intent ID and process ID where available. Its
intent result records `remote_outcome: unknown` and `automatic_replay: false`; its process row is
marked `abandoned`, retaining observed output and exit-code evidence without claiming the remote
process exited. Abandoned handles are excluded from scheduling and global blocking but remain
inspectable; `write_stdin` rejects them before sending input. JavaScript may catch the rejection and
continue, or an uncaught rejection produces the normal `Script failed` result for the model to handle.
Before a later executor operation, the broker reconnects under the original Sandbox incarnation
fence and resumes the same retained exec-server session. If resume fails, that initialize intent is
also settled as unknown and the requested operation is rejected as not dispatched. Only cursor-based
`process/read` may be retried within the original call, because it does not consume remote output.
If idle process reconciliation encounters a transport failure while reading a handle, it also marks
that process abandoned with its last observed cursor and buffered output, then lets the scheduler
continue.
Nested `view_image` has no external effect: missing, unreadable, oversized (over 32 MiB)
or non-image files reject the cell's tool promise with upstream's message and resolve the intent.
Its reads and result journal digests, not image bytes. Images a cell emits are prepared like upstream
tool output (bounded to high- or original-detail size, labelled with their real MIME type) before
entering the conversation; ones that cannot be prepared become upstream's placeholder text.
A submitted input is effected in History when it is confirmed and retained in the conversation;
a later turn failure may still leave `GetCommand` as `FAILED` or `UNKNOWN`. The failure settles the turn
without changing that confirmed input outcome, while queued commands follow the existing failure policy.

Process exit can precede stdout/stderr closure. Setup, tool reads and idle reconciliation retain the
process until `process/read` reports closed streams and an empty output page, preserving late output.
Idle owners independently read process output and reconcile exit in bounded pages. Observed exit
releases driver/host capacity without a model poll. Unread output per handle remains in PostgreSQL as
upstream's 1 MiB head and tail with an omission count, and can be consumed by an empty `write_stdin`,
including after the old executor session expires. Unresolved outcomes block continuation without
advancing the retained read cursor. An abandoned process keeps its last observed cursor and buffered
output for inspection, but retains no runtime slot. Truly running processes and live JS cells retain a
runtime slot.

A transport failure during cursor-based `process/read` permits one reconnect and retry within the
call's deadline. The sampler resumes the retained exec-server session under the same Sandbox
incarnation fence and reuses the read intent, process ID and cursor. The read does not consume remote
output; only its durable result advances the sampler's cursor. An expired session, changed session
ID, failed reconnect or second transport failure settles the affected intent and process as unknown;
the tool promise rejects, and a future operation may attempt the same-session reconnect again.
Process starts, stdin writes, signals and other effectful operations are never automatically retried.

When a Session is blocked, `GetSession.blocked_reason` names the unresolved intent or live handle and
points operators to the linked boundary evidence. An authenticated caller for the same account can
call `RecoverSession` with the current `attached.last_cursor` from `GetSession` and an operator note
to abandon a blocked cell/runtime failure. Its transaction locks the conversation before its session
and requires an expired owner lease and an exact cursor match. It supports all unresolved cell,
nested tool, and executor intents or lost live cells/processes;
provider sampling, compaction, and setup uncertainty remain unsupported. Recovery stores an
`abandoned` result with unknown remote outcome and no automatic replay, completes unanswered model
calls with matching aborted outputs, retains provider history, and adds a model-visible warning.
It marks live cells and nonterminal processes `abandoned` without killing remote processes or
reporting them exited. It advances the owner generation and durable JavaScript session epoch in the
same transaction, fencing late writes and forcing a new logical JavaScript session on any replica.
The continuation cannot access prior `store()` / `load()` values or live-cell handles; old shared-host
memory may remain inaccessible until that host exits. Recovery never retries an uncertain provider
or executor operation; see [blocked Session recovery](docs/blocked_session_recovery.md).

The sampler uses one generated typed `Executor` client for direct local-test and Gateway targets.
The `Executor` service contract terminates at the Sandbox's Runner, which reaches only its configured
loopback adapter; Gateway proxies the same service after authorizing the sampler and checking the
Sandbox UID and incarnation. The configured Gateway target selects the per-Sandbox control Service
and thus the Gateway replica holding that Runner's observation Lease. No caller-chosen executor URL
crosses the API. The Runner's adapter holds its local WebSocket on the server-streaming
`OpenSession` connection. An active stream blocks executor Sandbox autosuspend until it closes.
Dispatcher does not relay tools.

The adapter permits at most eight open or opening `OpenSession` streams per Runner. This is a limit
on live adapter connections, not Codex native sessions, sampler conversations, or the sampler's
`max_live_sessions` setting. The sampler releases its connection when no live cells or processes
require it; Runner drain or observer fencing closes it as well. The adapter introduces no durable
session persistence or process policy. A stream loss leaves the native Codex session detached for up
to 30 seconds, after which pinned Codex 0.162.1 expires it and terminates remaining processes. See
the [Executor API resource and lifecycle contract](../../executor/README.md) for connection budgets
and ownership.

Each unary operation carries the sampler's Sandbox scope, execution-session ID and durable nested
intent ID as `request_id`. The ID correlates the typed call and its exact native-frame evidence; it
does not provide deduplication or a cached receipt. A lost reply to a mutation remains unknown and
the JavaScript promise is rejected without replay. Cursor-based `process/read` may reconnect to the
same execution session and retry once with the same ID and cursor. The adapter returns exact native
frames to the sampler's existing boundary journal without exposing native JSON as an execution API.
For `view_image` block reads, frame payloads are stored as byte counts and SHA-256 digests so raw
image bytes remain excluded from the journal. Dispatcher does not relay tools. Every provider
request carries Codex's `gpt-6.1-sol` base instructions as
`instructions`, whatever the model. Standing instructions are supported; remote
AGENTS.md discovery, subagents, skills/MCP, approvals/hooks and a Claude sampler are deferred.

The conversation carries Codex's `<environment_context>` user message: the Session cwd, the name of
the executor-reported shell that `exec_command` launches (omitted for an executor that reports none,
although `exec_command` then falls back to `/bin/sh`) and the sampler's local date. The full context
precedes the first input; afterwards an update is recorded before a turn's input, or before a
sampling step after that step's queued `notify()` outputs, whenever a value changed (a date rollover
renders only the date), and compaction re-injects the full context before the compaction item. Timezone and
Codex's network, filesystem, permission-profile and subagents sections are omitted; Agentplane owns
those boundaries. **Deviation:** the executor connects lazily, so a shell first learned after the
initial context renders an update; Codex resolves the shell before its first render.

## Evidence and continuation

Provider requests/responses, tool intents/results, commands and context checkpoints commit in the
sampler DB. History is a separate retained evidence/read model. A transactional event outbox emits
raw `BoundaryRecord` observations plus source-linked normalized reasoning, text, tool and turn
records. `ExecutionProgress` preserves code-cell visibility where upstream native APIs lack it.
Thread is a lossy display projection; it is not suitable for reconstructing provider continuation.
Raw records use at most 1 MiB byte fragments with a shared record ID, zero-based fragment index
and total count. Concatenating payload bytes in index order reconstructs the full UTF-8 JSON
record; derived Events reference every fragment sequence. Normalized display strings cap at
256 KiB with an explicit truncation marker. Exact full content remains in raw evidence and
continuation. Recovery selects only queued work/live handles and caps concurrent active Session
drivers at the configured Session limit; it releases completed driver tasks. Idle logical Sessions
may exceed that active-driver cap. Those Sessions share the one lazy Codex host process.

The exporter uses existing `RunnerIngress.Push` with an explicitly authorized sampler announcement,
then replays original immutable entries. History's acknowledgement advances its durable local
checkpoint. Reconnecting reconciles the remote cursor before sending, including the crash window
after History commit but before local acknowledgement. It does not retry model/tool operations.
Bounded pages and concurrent send/ack handling apply backpressure independently of sampling.
The exporter reuses its connection and drains pending pages without an unconditional timer delay.
Bounded scheduling quanta share capacity between Sessions; committed outbox notifications wake idle
exporters across replicas, and listener reconnection triggers durable catch-up. Only failures incur
retry backoff. Each stream covers its announced snapshot; newer commits receive a fresh announcement
so History also retains the final Session metadata. Caught-up archives retain no streams or tasks.
History unavailability delays display while the sampler retains its outbox.

Public Session ID is the inference correlation key. Executor Sandbox/incarnation are distinct
metadata. Explicitly trusted LLM ingress callers may claim a canonical Session UUID for Langfuse;
arbitrary workload identity metadata remains rejected/stripped. Credentials and auth headers are
not evidence payloads. Existing History reader authorization continues to govern retained reads.

## Implementation and database ownership

The service and provider/tool loop run in Go; the pinned upstream JavaScript host is a child
process of the sampler. Production requires no Python interpreter. The existing gRPC API and
sampler-owned `sampler_*` table layout remain the boundary for clients and durable state. The Go
initializer creates that schema from its Goose SQL baseline. Repeated initialization preserves
Session identities, conversation, effect-journal, process, command, evidence and export state.
Acceptance tests initialize through the actual Go binary and inspect its database separately.

Retained provider items use append-only `sampler_conversation_item` rows. The current model context
remains a replaceable JSON value so compaction can swap it in the same transaction as its command
result. Appending a response writes only its new retained items instead of rewriting the full
transcript.
