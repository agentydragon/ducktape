# Experimental contract

This experiment supports sequential Responses sampling, `exec` code-mode calls, and a model-facing
`wait` custom tool. `main_bin advance` performs one transition. `main_bin run` retains one upstream
host while sampling and observing yielded cells, until assistant completion, interruption, failure,
or the 90-second CLI deadline. Every CLI owner has a 120-second database-clock lease; there is no
heartbeat, forced takeover, or automatic transport retry.

## Tools and observations

- `exec_command({cmd, workdir?, tty?, yield_time_ms?})` runs `/bin/sh -c`. The default wait is 60000 ms;
  waits accept integers from 0 to 60000. It returns combined UTF-8 replacement-decoded stdout/stderr
  as `{output, exit_code}` on observed exit, or `{output, exit_code: null, session_id}` while running.
  Each observation is capped at 1 MiB. A yielded tool result is not a process-exit observation.
- `write_stdin({session_id, chars?, eof?, yield_time_ms?})` sends input once, then observes output;
  empty chars polls without a write. Its default wait is 1000 ms. A sole `"\u0003"` uses upstream
  `process/signal` interrupt. `eof: true` requires a PTY and empty chars: it sends the terminal's Ctrl-D
  byte. Terminal mode determines its behavior. The pinned protocol has no pipe-close RPC, so pipe
  EOF is explicitly rejected. Other bytes are ordinary stdin bytes. Rejected or lost writes are not
  retried, even though upstream accepts a deduplicating write ID.
- `apply_patch(patch)` invokes the pinned Codex hidden apply-patch entrypoint by argv. Its parser and
  filesystem behavior are upstream's; result/timeout handling follows process execution above.
- A first line `// @exec: {"yield_time_ms": 10}` bounds the foreground JS observation. The real cell
  keeps running in the same host. The result identifies an opaque `cell_id`, distinct from a process
  `session_id`. `wait` takes raw JSON `{cell_id, yield_time_ms?, terminate?}` and uses upstream
  `session/wait` or `session/terminate`. Missing, foreign-host and terminal cell handles fail before
  dispatch. Terminating a cell alone does not certify its remote tools stopped; the process registry
  remains visible, and external interruption cleans up known supervised processes too.

The experiment omits upstream shell startup policy, sandbox policy, approvals and formatted result
metadata. It has no local tool fallback when the executor fails.

## Durable authority and live state

Postgres contains ordered provider items, completed observations, pending intents, process handles
and output cursors, live-cell identities, the executor session ID, ownership generations, and interrupt
requests/results. Provider dictionaries, including opaque encrypted reasoning and unknown fields,
are retained intact. Requests use Responses Lite `additional_tools`, encrypted reasoning and no
provider-side conversation storage. Only a complete `response.completed` can commit provider output
or dispatch a model tool; partial streams never dispatch. There is no automatic provider retry or
`previous_response_id` continuation.

Every provider, cell observation and nested tool invocation commits an intent before dispatch.
Completed provider/cell outputs atomically append to the conversation and clear the outer pending
intent. Nested results alone cannot recover an interrupted JS cell. Output cursors advance only after
real retained output is read; terminal process results drain output after exit. Current live handles
are regenerated from the database for model requests rather than inferred from earlier tool text.
The embedded central service also reconciles idle processes independently of model reads. Its
bounded unread-output buffer preserves up to 1 MiB per handle in PostgreSQL, and an observed exited
process with unread bytes can release the live runtime while remaining locally readable.

Process handles can survive completed cells and a sampler restart **while the same upstream executor
session remains resumable**. Initialize sends its recorded `resumeSessionId`; the pinned server retains
detached sessions for 30 seconds. A lost/expired session with live handles fails without opening a
replacement or replaying commands/stdin. A fresh session is allowed after all recorded processes and
cells are terminal. No server credentials are stored in these tables.

Cell handles include the host session UUID and native cell ID, since upstream counters restart in
fresh hosts. Live V8 state is not serializable. `run` can wait on cells in its retained host; losing that
host or using a fresh owner with a live-cell checkpoint blocks advancement. A one-step `advance` that
yields a cell therefore cannot continue it in the next process; use `run` for that lifecycle.

Fresh disposable databases use `main_bin init` / SQLAlchemy metadata. Existing experiment databases
apply `migrations/0001_compaction.sql`, `migrations/0002_process_controls.sql`,
`migrations/0003_process_output.sql` and `migrations/0004_interrupt_targets.sql` once, in order,
offline before using this extension.
There is no production migration runner or shipped service using these isolated tables.

## External interruption and explicit continuation

`main_bin status CONVERSATION` exposes the observed owner generation and interrupt request outcomes.
`main_bin interrupt CONVERSATION --generation N` durably requests interruption of that active owner;
a stale or idle generation is refused. `advance` and `run` check requests before scheduling a transition.
The intent transaction also refuses dispatch when a request is already committed; arrival after intent
commit can still race with dispatch. Requests left at normal release are explicitly marked not applied.

The owner cancels the sampling HTTP operation, terminates known live host cells, requests upstream
process termination and observes process exits. Cleanup has a 15-second overall budget, 5-second
per-handle limits and a 5-second delegate drain. These controls address known supervised processes;
they are not a cgroup fence or a proof that detached/escaped descendants cannot exist. Fixture tests
check that the actual known shell and child are no longer live after successful interruption.

Observed cleanup records `interrupted`, provider cancellation/completion uncertainty, cell outcomes,
process exit observations and possible partial effects. It closes all outstanding committed model
calls with truthful interrupted/not-started outputs, clears the resolved pending intent and stops
sampling. `advance` then returns `interrupted`. An explicit `main_bin input CONVERSATION PROMPT`
appends user input and reopens the conversation; old sibling calls are not executed. Provider
cancellation means the local response was closed, not proof that the provider stopped billing/work.

Unobserved cleanup, transport loss or unresolved effects record `effects_unknown` and retain/create
a blocking pending intent. New input and further advances are refused. There is no reconciliation
interface. Acknowledging that cancellation was sent never suffices to clear uncertainty.

A row lock, database-clock lease and monotonic generation fence all owner reads/writes. The database
cannot atomically fence remote effects: takeover can race after intent commit, and an old dispatched
request may still run. Lost replies or sampler/executor death never trigger automatic replay. This is
single-writer conversation fencing, **not exactly-once execution** or rolled-back interruption.

## Compaction

`init --instructions` persists optional standing developer instructions. Every request regenerates
those instructions, the current tool definitions and Postgres's active process handles. These are
outside the replaceable context and cannot be removed by a model summary. Any live JavaScript cell,
unresolved outer/nested intent, unmatched tool call, orphan output or duplicate call ID blocks
compaction before dispatch. The same database fence and pending-intent check apply at installation.

`compact` requests pinned Codex 0.162.1's remote Responses v2 protocol: the configured ordinary
Responses endpoint, `x-codex-beta-features: remote_compaction_v2`, and a final `compaction_trigger`
input. This experiment does not implement upstream's local natural-language summarization fallback
or its legacy compact endpoint. It accepts exactly one `compaction` item with nonempty opaque
`encrypted_content`, after `response.completed`; calls returned during compaction are rejected.
Unknown fields and the encrypted string are retained unchanged. Completed streamed items are
collected even when the terminal event has no `output` array, as in pinned upstream fixtures.

The full ordered conversation stays in `items`; every request and successful raw response/events
stay in its intent. A successful compaction atomically replaces only `context_items` and clears its
pending intent. New conversation/tool items subsequently append to both histories. The replacement
must be strictly smaller in canonical serialized UTF-8 bytes than the replaced context. This is a
payload-size guarantee, not a tokenizer guarantee. The regenerated prefix is excluded from this comparison.

`advance --auto-compact-tokens N` or `run --auto-compact-tokens N` requests compaction before the next sample when the most recent
completed sampling response reported `usage.total_tokens >= N`. A tool call still executes before
that check, live JavaScript defers automatic compaction until it can be waited to completion, and an
assistant final remains `done`. Compaction clears the usage checkpoint to prevent
repeated compaction without another sample. This opt-in threshold uses observed provider usage;
unmeasured tool-output growth can cross a model window before another observation. It is not a hard
context-window bound or a default derived from an unverified model catalog.

Rejected or malformed completed compaction, including nonshrinking output, preserves the original
context and settles its intent as rejected. Explicit new input can resume safely; there is no
automatic retry. Unterminated or canceled compaction leaves its intent unresolved. An acknowledged
external interrupt may clear the canceled compaction intent after observed cleanup; it preserves
the preceding context and requires explicit user input. Ownership loss fences installation.
Unresolved cases block future automatic advance and have no reconciliation interface yet.
Compaction never dispatches or replays a tool.

## Remaining omissions

This extension does not add instruction discovery,
persistent JavaScript `store`/`load`, notify, subagents, skills, MCP, approvals, hooks, memory retrieval,
History export or frontend traces. Unsupported provider item types and non-`exec`/`wait` tools fail
explicitly. No live inference/provider-account compatibility, production replacement or deployment is
established. The full native harness remains responsible for capabilities outside this contract.

## Service embedding

The central sampler reuses these runtime transitions. Its Store subclass appends evidence within
the same transactions and supplies public Session attribution. The standalone experiment keeps
its own conversation identity and sends no central attribution header.
