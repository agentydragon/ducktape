# Claude Code 2.1.252 runtime contracts

Status: **static implementation evidence and Agentplane design constraints**.

These notes come from structurally debundling the pinned Claude Code 2.1.252 application. They
describe behavior implemented by that build, but do not replace the live capture requirement in
[provider_protocols.md](provider_protocols.md): a runner feature must still be exercised before it
becomes an Agentplane guarantee. No proprietary source is reproduced here.

## Priority for Agentplane

| Priority | Boundary                        | Current gap                                                                                                                                                                                    |
| -------- | ------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| P0       | Input admission and persistence | Claude `queued` means command-queue admission, but the runner currently maps it to the provider-neutral `InputAccepted`. Transcript append acceptance is also weaker than durable persistence. |
| P0       | Driver-hosted MCP               | The wire is documented in [driver_tools.md](driver_tools.md), but the runner neither declares SDK MCP servers during initialization nor routes `mcp_message`.                                  |
| P1       | Permission and dialog recovery  | The runner deliberately auto-allows tool permission requests and rejects other controls. A future interactive host must use `tool_use_id`, not a transient request id, as its recovery key.    |
| P1       | Background task state           | Claude exposes a replace-set snapshot plus detail edges; the current adapter retains these only as native frames.                                                                              |
| P1       | Remote delivery ambiguity       | Claude's managed remote transport distinguishes never-uploaded calls from calls that may have landed. Agentplane's app-to-runner stream has no equivalent classification.                      |
| P2       | Limits and refusal fallback     | Rate-limit and fallback events remain native-only, and a fallback that needs a user dialog cannot complete through the current runner.                                                         |

## Known Agentplane bugs and recommended work

These IDs are local shorthand for implementation work, not upstream issue numbers.

### C1: `InputAccepted` overstates Claude's acknowledgement

**Bug.** [`runner/claude.py`](../runner/claude.py) emits `InputAccepted` for
`command_lifecycle: queued`. The name previously implied transcript admission and still conflates
different provider-native boundaries. Clients can therefore treat an input as recoverably accepted
before Claude has started it or persisted it.

**Recommendation.** Make queue admission explicit instead of silently changing the meaning for one
provider. Add an `InputQueued` state/event, move Claude's `InputAccepted` transition to `started`
or replayed-user evidence, and retain the native lifecycle frame for exact correlation. Migration
tests must cover early cancellation without `started`, coalesced contributors, and process loss in
each state. Until that protocol change lands, consumers must read Claude `InputAccepted` as native
queue admission only.

### C2: the documented SDK MCP host role is not implemented

**Bug.** [`native/claude/wire.py`](../native/claude/wire.py) cannot declare SDK MCP servers/configs
or parse typed `mcp_message` requests, and [`runner/claude.py`](../runner/claude.py) rejects them
through `UnknownControlRequest`. The runner therefore cannot provide the tools described in
[driver_tools.md](driver_tools.md).

**Recommendation.** Extend initialization with typed server declarations, add bidirectional
`mcp_message` correlation, and preserve per-server timeouts plus normalized error replies. Test the
full initialize/notification/list/call sequence, including the required reply to notification-shaped
messages. Keep MCP task support disabled until an active `tools/call` path is observed using it.

### C3: app-to-runner delivery is ambiguous after connection loss

**Bug.** Runner events are durable and cursor-replayable, but app commands are direct gRPC writes.
If that stream fails, the app cannot distinguish a command that never reached the runner from one
that arrived before the acknowledgement was lost. `input_id` resolves a retry only after the runner
has logged it.

**Recommendation.** Persist an outbound command ledger in the app with at least queued, written,
and runner-acknowledged states. Reconcile it against the runner log on reattach and expose
never-delivered versus possibly-delivered outcomes. Exercise loss before write, after write but
before acknowledgement, and after the runner's durable input event.

### C4: background state has no typed reset path

**Bug.** Claude background snapshots and edges currently survive only as `Native` events. A client
cannot consume the replace-set contract without provider-specific parsing, and replaying edges after
a worker restart can retain tasks that no longer exist.

**Recommendation.** Add a provider-aware adapter projection whose snapshot event replaces the
complete set and whose detail events reference the native source sequence. Test restart with an
empty snapshot, queue pressure, duplicate terminal notification, and correlation to the originating
tool call. Do not manufacture equivalent Codex push semantics; its background-terminal surface is
polling-only.

### C5: interactive controls cannot park and resume

**Unsupported behavior.** The runner intentionally auto-allows `can_use_tool` and answers hooks,
dialogs, and MCP requests with errors. That keeps current turns from wedging, but it cannot support
async approval, user-dialog-driven refusal fallback, or hook execution.

**Recommendation.** When interactive controls enter scope, persist `tool_use_id` as the durable
identity and keep `request_id` only for the live exchange. Rebuild pending actions from initialize,
deduplicate redelivery, propagate cancellation, and cover orphaned permission recovery after the
original request id has disappeared. Do not persist resolver objects or present a refusal as a user
decision.

### C6: limit and fallback decisions are opaque

**Gap.** `rate_limit_event` and model-fallback frames remain native-only. The caller cannot tell
whether Claude is intentionally waiting in low-priority mode, left it because of a budget/reset
condition, or is asking for a refusal-fallback decision.

**Recommendation.** First preserve and display the native reason and reset metadata without
inventing a shared retry state. Add typed events only once captures pin their shapes. A hosted
fallback must depend on C5; otherwise keep the dialog path explicitly unsupported.

### Corrected documentation bug

The previous queue note claimed coalesced non-representative UUIDs received no terminal lifecycle
receipt. The pinned runtime retains every contributor UUID and emits receipts for all of them; this
PR corrects [claude_input_queue.md](claude_input_queue.md). Cancellation is still batch-granular
after dequeue.

## Input and turn lifecycle

`command_lifecycle {state: "queued"}` acknowledges admission to Claude's command queue. It does
not prove that the input has started, entered the transcript, reached the model, or become durable.
An early cancellation can therefore produce `cancelled` without `started`.

Claude can coalesce compatible queued commands into one turn. The last UUID is the batch's
representative for execution and cancellation, but the runtime retains every contributing UUID and
emits `started` plus a terminal lifecycle receipt for each. Selective withdrawal remains impossible
after dequeue: `cancel_async_message` only removes an item still in the queue, and interrupting the
active batch applies to every contributor.

Agentplane must consequently keep these facts separate:

- runner receipt and durable logging of the caller's input;
- Claude command-queue admission (`queued`);
- Claude transcript/turn admission (`started` or replayed user evidence); and
- terminal completion or cancellation for every contributing UUID.

The existing queue-specific implications and capture work are in
[claude_input_queue.md](claude_input_queue.md).

## Transcript durability and compaction

Claude serializes writes per transcript file and deduplicates appends by message UUID, but an append
API can return after enqueueing and before the filesystem write completes. Explicit flush, terminal
result, and orderly shutdown are the persistence fences. Shutdown seals the writer against later
appends.

Recovery treats an incomplete JSONL tail as local corruption: it quarantines/seals the tail and
retains the valid prefix rather than discarding the transcript. Compaction writes a temporary file,
syncs it, verifies that the source did not change, preserves complete concurrently appended suffix
lines, and only then publishes. It aborts rather than committing output with broken preserved UUID
or parent chains.

Implications:

- Agentplane's durable event log does not make Claude's native transcript durable.
- A process loss after `queued` or even after local append acceptance still needs uncertainty
  handling unless a persistence fence completed.
- Resume can merge known UUIDs that do not yet have a local file and lazily hydrate driver-backed
  agent transcripts without overwriting locally written ones.

The broad transcript parser/reducer and the UI/autocompact policy were not recovered in this pass.

## Driver-hosted MCP

Initialization supplies `sdkMcpServers` and per-server configuration. The CLI then sends each MCP
JSON-RPC message as a correlated `control_request {subtype: "mcp_message", ...}`. The host must
answer with `mcp_response` even for notification-shaped messages; the CLI awaits that response and
injects it into its MCP client. Messages in the reverse direction are sent through the session
controller and correlated there.

Agentplane therefore needs typed `mcp_message` dispatch, per-request correlation, and per-server
deadline/error normalization before it can claim driver-hosted MCP support. MCP tool identity stays
separate: calls carry the originating Claude `toolUseId` in `_meta`.

Claude 2.1.252 contains MCP task/input-request helper scaffolding, but the active v2 `tools/call`
path does not wire it. Do not advertise driver-hosted MCP task semantics from the scaffolding alone.

## Permission and user-dialog parking

`request_id` correlates one live control exchange. `tool_use_id` is the durable semantic identity
used to recover a permission decision after a worker restart. Pending entries retain the full
control envelope, resolver/rejecter, response schema, and forwarding state.

A future interactive host must preserve these rules:

- aborting a live request sends `control_cancel_request`, removes it, and rejects it;
- reinitialization returns complete pending permission/dialog envelopes;
- duplicate answers and tool-name mismatches are rejected by `tool_use_id`;
- unanswered human requests deliberately remain parked across shutdown, while other pending
  controls reject when the stream closes;
- a timed-out dialog ignores a late answer, and an error response is not a user choice; and
- if the original `request_id` is gone, Claude can find the unresolved transcript tool use by
  `tool_use_id` and enqueue an orphaned-permission command to resume the turn.

Returning no decision from a host callback intentionally leaves the request parked. That is useful
for async approval, but only if Agentplane persists and later redelivers the durable identity.

## Background tasks

Treat `background_tasks_changed` as a complete replacement of the live set, including after a
process restart. `task_started`, `task_updated`, progress, and terminal notification frames are
detail edges, not a source from which to reconstruct the set.

The runtime maintains a per-session event queue capped at 1000 entries. Under pressure it
preferentially retains task lifecycle bookends and terminal status. Drain stamps fresh outer
`uuid`/`session_id` values. A terminal notification is guarded for once-only delivery and may carry
the originating `tool_use_id`, output file, summary, usage, transcript, and ambient metadata.

See [background_work.md](background_work.md) for the provider comparison. The app-state snapshot
projector remains outside the recovered module; only its replace/reset consumption rule is relied
on here.

## Managed remote transport

This section is design evidence from Claude's managed remote transport, not a claim about its stdio
protocol or Agentplane's current transport.

Claude rereads authentication with bounded waiting before initialization/connection and restores
persisted worker state before resume hydration. Durable control requests stay tracked after local
enqueue: successful upload advances delivery state, while a dropped batch distinguishes a request
known never to have uploaded from one that may have reached the peer. Permanent close ends input;
explicit close also removes callbacks, keepalive, attestation, and feature-refresh subscriptions.

Agentplane durably logs runner-to-client events and can replay them by cursor. Its client-to-runner
gRPC writes have no persisted upload ledger, so after a connection loss the bridge cannot tell
"never sent" from "possibly delivered." `input_id` makes a retry idempotent only after the runner
has logged that id. Do not infer a delivery guarantee from a successful local write.

## Rate limits and refusal fallback

Claude's low-priority state machine opts eligible calls in with `anthropic-usage-limit: slow`, reads
the unified slow-limit headers, and tracks active/idle state plus five-hour/seven-day resets. Waiting
is jittered and bounded; weekly, budget, ineligible, disabled, wall-clock, and maximum-wait exits are
distinct. Reaching maximum wait ends low-priority mode and returns to the normal API error path.

Refusal fallback is separate from availability retry. It selects a category/catch-all route,
checks entitlement and model family, prevents retry loops, and may preserve safe partial output.
When policy needs a choice it parks on `retry_fallback`, `edit_prompt`, or `cancelled`; a newly queued
prompt cancels that parked dialog. A fallback can be latched for the session or limited to one
response.

Until the runner handles these surfaces, preserve `rate_limit_event`, model-fallback frames, and
dialog requests as native evidence. Do not translate them into a generic retry or claim that a
host-mediated fallback succeeded.
