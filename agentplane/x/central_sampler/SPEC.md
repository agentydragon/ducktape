# Central sampler contract

## Identity and admission

`Sampler` is a separate authenticated gRPC API. `OpenSession` binds a canonical public Session UUID
to a central `SessionSpec`, caller ServiceAccount UID and fixed `SandboxDestination` plus expected
incarnation UID. The named service/provider and model/effort must be server-configured. Every RPC
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

One shared service runs the provider loop; one separately supervised, resource-limited upstream
JS host serves each live Session. Completed-cell state is checkpointed in PostgreSQL. Model-facing
`exec` and `wait`, yielded process handles, stdin and external interrupts use the pinned upstream
protocol. Live JavaScript state is process memory and cannot be restored from PostgreSQL. Lost
live cells or uncertain effects block automatic continuation; requests/tools are never replayed
merely because a transport failed. Process handles inherit upstream's detached retention limits.

Database-clock leases and generations fence stale sampler writes. They cannot atomically fence an
external shell effect: a dispatched operation with no observed result is uncertain. The persisted
intent/result journal distinguishes this from an operation known not to have been dispatched.
Executor failure leaves continuation and retained conversation outside the failed Sandbox.

Explicit provider rejections, terminal provider failures and model-tool validation errors before
JavaScript dispatch record a rejected intent. When no unresolved effects or live execution remain,
the command and turn fail without blocking the Session; subsequent input may continue without replaying the failed
operation. Accepted tool calls that were never dispatched receive truthful failure outputs.
Transport loss, streams without a terminal response and uncertain execution remain blocking failures. Nested
executor failures remain conservative because the surrounding JavaScript may already have caused
effects.

Idle owners independently read process output and reconcile exit in bounded pages. Observed exit
releases driver/host capacity without a model poll. At most 1 MiB of unread output per handle remains
in PostgreSQL and can be consumed by an empty `write_stdin`, including after the old executor
session expires. Unknown outcomes or unread-output overflow block continuation without advancing
the retained read cursor. Truly running processes and live JS cells retain a runtime slot.

The JS broker uses `ExecutorGateway.Connect` to send exec-server frames through the Runner's
existing outbound stream. Its configured target template selects the per-Sandbox control Service
and thus the Gateway replica holding that stream. Gateway authorizes the sampler account and checks
Sandbox UID and incarnation; Runner only reaches its configured loopback executor. No caller-chosen
executor URL crosses that API. The relay bounds each message to 1 MiB and each Runner to eight channels;
oversized tool messages fail the channel rather than being silently truncated. Dispatcher does
not relay tools. Standing instructions are supported; remote
AGENTS.md discovery, subagents, skills/MCP, approvals/hooks and a Claude sampler are deferred.

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
continuation. Recovery selects only queued work/live handles and caps drivers and hosts at the
configured Session limit; it releases completed driver tasks.

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
