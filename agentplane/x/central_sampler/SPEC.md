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

The executor `OpenSession` initialization handshake has its own positive
`executor_initialize_timeout_seconds` setting, defaulting to 30 seconds independently of
`rpc_timeout_seconds`. A timeout cancels the stream and records the initialization outcome as
unknown; it does not claim that the executor did not initialize.

Non-empty setup scripts (at most 65,536 UTF-8 bytes) execute once through the bound exec-server
with `/bin/sh -eu -c`, before any provider request or queued command. Setup stdout and stderr
retain separate byte streams and lifecycle events in History. A failed or interrupted setup blocks
continuation. A durable attempt marker prevents replay after restart, including an uncertain lost
completion acknowledgement. Successful setup is not repeated when later commands attach.

## Runtime and effects

Each sampler replica runs the provider loop and owns one lazy upstream Codex JavaScript host process.
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
If `process/read` loses its transport after observing output, its nested tool promise rejects with a
JSON rejection string containing the original failure, `remote_outcome: unknown`,
`automatic_replay: false`, the last observed `after_seq` cursor, process and executor intent IDs,
and a text view of at most 8 KiB of retained bytes plus an omission marker. Byte counts distinguish
bytes omitted by the retained head/tail buffer from bytes hidden only in the error view. The complete
retained bytes are base64 in the `tool` result `BoundaryRecord` found through `FollowEvents` by its
payload `intent_id`. The process remains abandoned and no exit is inferred. Other nested failures
retain their ordinary string messages. Large result records use the normal `record_id` and
`fragment_index` reassembly contract.

The pinned code-mode host V1 passes `delegate/response` errors to JavaScript as primitive strings:
the delegate response has a `message` field but no structured error or cause field. Catching code
must parse the JSON string for these fields; native `Error` properties require a host protocol change.

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

A flat top-level `models` map allowlists public route IDs and reasoning efforts. Each entry binds a
named `connection`, explicit `provider_model`, `api` (`responses` or `anthropic_messages`) and
`exec_encoding`. Connections declare a base URL and `auth.type` (`bearer` or `anthropic_api_key`),
with exactly one inline credential or rotating credential file. URLs cannot contain credentials,
query strings or fragments. Callers cannot select connections. Responses settings declare a
`lite` or `standard` dialect; Anthropic settings declare native request features and require JSON
exec. Unknown APIs, mixed API settings and invalid feature combinations are rejected.
JSON encoding exposes a single
function `exec`. Exactly one of `code` (execute JavaScript) or `cell_id` (resume a yielded cell)
is required; both accept optional `yield_time_ms` and `max_output_tokens`, and only the resume
variant accepts `terminate`. JSON source does not accept the native exec pragma. The adapter
validates arguments before dispatch and uses the same host operations, journal and recovery
rules as freeform calls. Configured calls are dispatched only when both their name and item type
match the selected route. A syntactically valid call using a name not exposed by that route is
retained in provider history and answered with the matching function or custom tool output and an
unsupported-call error; it is not dispatched, and the model may continue. Malformed call shapes,
unsupported output items, duplicate call IDs, and configured names used with the wrong item type
remain validation failures.
Model changes cannot change connection, API, dialect or exec encoding within a Session. Anthropic
also requires the same provider model and request options to preserve signed continuation. Route contracts must remain fixed
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

Each conversation is pinned to an explicit API: `responses` or `anthropic_messages`. Responses
context contains native Responses items; Anthropic context contains native Messages API messages.
Neither protocol is stored inside the other's item envelope. Protocol adapters own input rendering,
call/result pairing, completion, effort controls, and compaction. They derive generic tool invocations
with call IDs and native source-record ordinals; these execution decisions are not extra conversation
records. The shared runtime owns effect intents, execution, leases, interruption, and recovery.
The native API packages receive execution snapshots and a provider-neutral evidence sink. Each
stream event and its derived History observations commit atomically before parsing continues;
evidence failure stops the stream without acknowledging the pending effect.

Anthropic preserves signed thinking, redacted thinking and compaction content without translating
their fields.
Native text/thinking/tool observations reference the original event evidence. `message_stop` with
`end_turn` completes a turn; `tool_use` requires at least one complete object input. Eager JSON
fragments are observed but never executed before terminal validation. Errors, unsupported blocks,
inconsistent stop reasons and incomplete JSON cannot execute partial calls. A missing terminal
event leaves the sampling intent outcome unknown.

Anthropic's stable `system` contains base and standing instructions. Environment and active-handle
changes append durable user context. Automatic prompt caching uses configured `5m` or `1h` TTL.
Cache diagnostics reference the last ordinary provider message ID in append-only conversation,
including after restart and compaction. Native usage preserves cache counters and compaction
iterations. Context estimates include input, cache-creation, cache-read and output tokens.

With `per_message_effort`, the launch `output_config.effort` stays fixed. Applying an effort command
stores an inline `role: system`, empty-content output-config control, enabled with
`mid-conversation-output-config-2026-07-01`. Binding validation uses
`thinking-binding-controls-2026-08-01`. Strict `exec` schemas and `eager_input_streaming` are per-tool
settings; host-side XOR, type and numeric validation remains authoritative before dispatch.

Anthropic manual and automatic compaction use `compaction: {type: summarize}` and
`compact-2026-09-04`. The request uses the same model, system and tools as ordinary sampling.
The sampler compacts only a settled prefix, optionally retaining complete recent turns unchanged.
It never inserts a control between the summary and kept turns. A result must contain exactly one
nonempty signed compaction block and shrink serialized active context. A single transaction swaps
context and settles the command under the original-context/lease fence; original conversation and
History survive. Summarized effort stops applying at the provider, so restoration is deferred until
the first new user message after kept turns. Native compaction puts the summary first and does not
prepend the Responses environment reinjection. Background compaction and dynamic definitions are
deferred.

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
On service shutdown, the runtime stops new Session claims and lets each active top-level effect
reach its durable result before releasing ownership. A live JavaScript cell may continue its own
nested executor calls until that cell settles. The owner keeps renewing its lease during drain;
the next owner can then claim immediately and resume the retained command from its committed
conversation. `shutdown_drain_seconds` defaults to 40 and `shutdown_budget_seconds` to 55;
the latter must fit within the Pod's 60-second termination grace and leave at least 15 seconds
after the drain deadline for cancellation and cleanup. At the drain deadline, the runtime cancels active work
and retains any unresolved intent and partial evidence; the next owner blocks rather than
replaying an uncertain provider or executor operation. Claim, release and every result write
remain fenced, including late writes from the old generation. During shutdown, lease release has
a five-second timeout; an unreleased owner remains fenced by lease expiry. Shutdown waits for
ownership acquisition and release to finish.

Executor failure leaves continuation and retained conversation outside the failed Sandbox.

The conversation records the actual Code Mode host process generation after its logical Session
opens. A host restart within one Pod rotates that generation. Before the next model step, a new
owner compares it under the lease fence. If it differs and no effect is unresolved, the sampler
advances the durable JavaScript epoch and appends a model-visible notice that prior `store()` /
`load()` values and live handles were lost. Pending model tool calls proven undispatched receive
failed outputs paired to their exact call IDs before the notice; unrelated calls follow their
ordinary dispatcher path. The transaction clears the old host marker, so a retry cannot duplicate
the notice. The marker is set again only after a logical Session opens on a real host. With an
unresolved cell, tool, executor or live handle, host loss remains an unknown outcome requiring
explicit recovery; it is never interpreted as a known no-dispatch call. A first use with no marker
and an ordinary same-host reacquisition do not add a notice.

Invalid `exec`/`wait` arguments and unknown, completed or foreign JavaScript cell handles return
error text in the matching model tool output without dispatching to the runtime or executor.
The call intent is completed and the model may continue in the same turn, preserving JavaScript
state and other live cells/processes. JavaScript syntax errors and exceptions likewise return the
normal `Script failed` output. Internal store, host-protocol and transport failures are not
classified as model input errors.

Explicit provider rejections and terminal provider failures record a rejected intent.
When no unresolved effects or live execution remain,
the command and turn fail without blocking the Session; subsequent input may continue without replaying the failed
operation. Accepted tool calls that were never dispatched receive truthful failure outputs.
The original driver error is retained in command and turn failure reasons and, for blocked sessions,
in `GetSession.blocked_reason`.
An explicit Code Mode `operation/response` with `status: "error"` is returned to the model as a failed
tool result with the host message and method/phase evidence. For `session/execute`, the host's operation
error is emitted before it starts a cell, so the result records that no execution started; errors in the
later `execute/initialResponse` phase remain operational. An explicit wait or terminate error does not
establish whether the request had effects: it leaves the observed cell state unchanged and reports neither
a successful observation nor a confirmed termination. Missing replies, transport loss, malformed or
unknown response statuses, missing error messages, and journal/store failures remain operational failures.
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
ID, failed reconnect or second transport failure settles the affected intent and process as unknown.
The exact native rejection `unknown session id <retained-id>` with JSON-RPC code `-32600` is definitive
session loss only when both its native response frames and semantic rejection result are durably
recorded. The sampler then abandons live process handles while retaining observed output, clears the
stale session binding, and durably records a model notice before opening a fresh exec-server session
under the same Sandbox incarnation. If the model's outer exec call is still pending, the notice is
queued and appended after that call's tool result, before the next sample; otherwise it is appended
directly. The operation that discovered the lost session is returned as not dispatched and is never
replayed; only later model-requested operations may use the fresh session. A generic `Unavailable`,
other rejected status, unknown outcome, changed session ID, failed resume or incomplete response
evidence is not proof of session loss and never triggers fresh-session substitution. After those
failures, a future operation may attempt to resume the retained session again. Process starts, stdin
writes, signals and other effectful operations are never automatically retried.

A confirmed native executor rejection becomes a rejected promise for that nested tool call after
native-frame evidence and the executor intent result are committed. A rejected `process/start` also
releases its local process row only after that row is recorded as exited; evidence or process
settlement failures remain operational errors. A rejected `process/read` abandons the stale handle
with its last observed cursor and buffered output, records the remote process outcome as unknown, and
does not replay the command or report it exited. The same settlement applies during idle process
reconciliation, which continues with unrelated live handles. The delegate binding remains available
for later tool calls; uncaught JavaScript promise rejections still follow the normal cell failure
behavior.

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
or executor operation. An optional operator-supplied `replacement_incarnation_uid` rebinds only the
incarnation of the same Sandbox, clears retained native-session identity, and records old/new UIDs
after old handles are abandoned. Future calls still authenticate that exact incarnation; see [blocked Session recovery](docs/blocked_session_recovery.md).

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
request carries Codex's `gpt-6.1-sol` base instructions as Responses `instructions` or Anthropic
`system`, whatever the model. Standing instructions are supported; remote
AGENTS.md discovery, subagents, skills/MCP and approvals/hooks are deferred.

The conversation carries Codex's `<environment_context>` user message: the Session cwd, the name of
the executor-reported shell that `exec_command` launches (omitted for an executor that reports none,
although `exec_command` then falls back to `/bin/sh`) and the sampler's local date. The full context
precedes the first input; afterwards an update is recorded before a turn's input, or before a
sampling step after that step's queued `notify()` outputs, whenever a value changed (a date rollover
renders only the date). Responses compaction re-injects the full context before the compaction item;
Anthropic preserves its signed summary as the first message. Timezone and
Codex's network, filesystem, permission-profile and subagents sections are omitted; Agentplane owns
those boundaries. **Deviation:** the executor connects lazily, so a shell first learned after the
initial context renders an update; Codex resolves the shell before its first render.

## Evidence and continuation

Provider requests/responses, tool intents/results, commands and context checkpoints commit in the
sampler DB. Each provider intent stores its final serialized request before dispatch. Transport
bytes, HTTP status/request ID, ordered native events, partial streams, errors, final content and
usage are retained; credentials and authentication headers are excluded. Parsing buffers are capped
at 64 MiB per response; received bytes are recorded before parsing, including incomplete SSE tails.
A DB write failure prevents further effect dispatch or leaves an already-dispatched intent unknown. History is a separate retained evidence/read model. A transactional event outbox emits
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
then replays original immutable entries. Any sampler replica can export any Session, independently
of agent-loop ownership; any History replica can receive it. Concurrent copies of the same journal
are valid. An acknowledgement covers the Session's durable History prefix, including entries another
copier supplied, and may exceed this stream's sent page or announced snapshot. Acknowledgements
never decrease within a call, must not report a seal, and cannot exceed the current committed
sampler journal. The exporter validates newer cursors against a fresh source read. Its sent cursor
remains separate from the acknowledged cursor: entries within one stream stay consecutive even
when a peer advances History ahead of it. Gaps and conflicting retained entries remain errors.

The durable local export checkpoint records a completed snapshot, not an intermediate data ACK.
It advances monotonically to the announced snapshot's cursor only after History covers that cursor
and the RPC ends successfully, confirming the snapshot was committed or superseded. A partial
quantum or failed RPC leaves the Session pending, even when History already has all its entries.
Reconnecting reconciles the remote cursor before sending, including the crash window after History
commit but before local acknowledgement; metadata-only catch-up therefore needs no entry replay.
Exported sampler metadata changes advance the journal in the same transaction, so snapshots are
ordered by cursor. Newer commits receive a fresh announcement. A sampler Pod replacement does not
seal its Sessions' logical journals. History export never retries model/tool operations.

Bounded pages and concurrent send/ack handling apply backpressure independently of sampling.
The exporter reuses its connection and drains pending pages without an unconditional timer delay.
Bounded scheduling quanta share capacity between Sessions. Committed outbox notifications from any
writer wake the idle exporter, and listener reconnection triggers durable catch-up. Only failures
incur retry backoff, per Session, exponential, and reset by progress. Caught-up archives retain no
streams or tasks. History unavailability delays display while the sampler retains its outbox.

Public Session ID is the inference correlation key. Executor Sandbox/incarnation are distinct
metadata. Explicitly trusted LLM ingress callers may claim a canonical Session UUID for Langfuse;
arbitrary workload identity metadata remains rejected/stripped. Credentials and auth headers are
not evidence payloads. Existing History reader authorization continues to govern retained reads.

## Implementation and database ownership

The service and provider/tool loop run in Go; the pinned upstream JavaScript host is a child
process of the sampler. Production requires no Python interpreter. The existing gRPC API and
sampler-owned `sampler_*` table layout remain the boundary for clients and durable state. The Go
service applies its Goose SQL baseline before serving. Repeated startup preserves
Session identities, conversation, effect-journal, process, command, evidence and export state.
Acceptance tests start the actual Go binary and inspect its database separately.

`sampler_conversation.api` pins the native protocol. Ordered, immutable native records live in
`sampler_conversation_item.record`. `sampler_conversation.context` optionally holds the compacted
active context as a protocol-owned object (`items` for Responses; `messages` and deferred effort
restoration for Anthropic). With no replacement context, the archive is the active context.
Compaction atomically verifies the expected active context and lease before replacing it and
settling its intent/command. It never rewrites the archive. New responses append only new records.

Migration 8 is the final native-schema baseline. A fresh, empty sampler database creates native
tables directly. An existing database is adopted only with complete Goose version-7 history and
the native API, status constraints, columns, and indexes already installed. Adoption preserves all
prior Goose rows, Session identities, provider payloads, raw exchange evidence, commands, events,
and unresolved-effect state without copying data. It adds a nullable Code Mode host marker to
the adopted conversation table in the same transaction, which briefly requires a table schema
lock. Startup migration connections cap DDL lock waits at five seconds; timeout rolls back
migration 8 so a later startup can retry. A partial version-7 conversion or older database fails
startup and requires the previous image or explicit repair. Startup no longer classifies or converts
legacy provider records. Later
schema changes use new additive Goose migrations. Schema rollback requires restoring the
pre-migration database backup together with the old service image.

The staging sampler Deployment uses two rolling replicas. New migrations must remain compatible
with serving old replicas during a rollout. Baseline 8's host-instance marker is nullable.
