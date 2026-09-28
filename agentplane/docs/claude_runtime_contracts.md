# Claude Code 2.1.252 runtime contracts

Status: **static implementation evidence and Agentplane design constraints**.

These notes come from structurally debundling the pinned Claude Code 2.1.252 application. They
describe behavior implemented by that build, but do not replace the live capture requirement in
[harness_protocols.md](harness_protocols.md): a runner feature must still be exercised before it
becomes an Agentplane guarantee. No proprietary source is reproduced here.

## Priority for Agentplane

| Priority | Boundary                       | Current gap                                                                                                                                                                                                       |
| -------- | ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| P0       | Command delivery and recovery  | Stable retry identity, a defined saved-response boundary, and native execution recovery need proof. Queue placement is decided in [the layering design](thread_layering.md#queue-placement-decision).             |
| P2       | Driver-hosted MCP              | The wire is documented in [driver_tools.md](driver_tools.md) and scripted-test-pinned at the native driver layer, but the runner neither declares SDK MCP servers during initialization nor routes `mcp_message`. |
| P1       | Permission and dialog recovery | The runner deliberately auto-allows tool permission requests and rejects other controls. A future interactive host must use `tool_use_id`, not a transient request id, as its recovery key.                       |
| P1       | Background task state          | Claude exposes a replace-set snapshot plus detail edges; the current adapter retains these only as native frames.                                                                                                 |
| P1       | Remote delivery ambiguity      | Claude's managed remote transport distinguishes never-uploaded calls from calls that may have landed. Agentplane's app-to-runner stream has no equivalent classification.                                         |
| P2       | Limits and refusal fallback    | Rate-limit and fallback events remain native-only, and a fallback that needs a user dialog cannot complete through the current runner.                                                                            |

## Known Agentplane bugs and recommended work

These IDs are local shorthand for implementation work, not upstream issue numbers.

### C1: command effect is now causal

The runner no longer treats `command_lifecycle: queued` as a user-message result. It records
`CommandAdmitted` at its durable journal boundary. A normal prompt is correlated by Claude's
following `stream_event.message_start.user_message_uuid`; a coalesced queued message retains its
newline-joined text and every origin command id at that representative UUID (with a replayed native
echo when Claude emits one). For active-turn inputs Claude emits neither correlation nor replayed
user text: after the tool-result frame, its exact `started` cohort proves the inputs entered that
continuation, so the runner records both sources on `HarnessUserMessageConfirmed`. A cancelled
queue entry becomes `CommandNoop`. The exact native lifecycle remains visible as `Native` evidence.

### C2: the documented SDK MCP host role is not implemented in the runner

**Bug.** [`runner/claude.py`](../runner/claude.py) still cannot provide the tools described in
[driver_tools.md](driver_tools.md): its `initialize` never declares `sdkMcpServers`/configs, and it
rejects an incoming `mcp_message` through its own typed `McpMessage` case of the same "no answer
path here" branch hook callbacks and dialogs share. [`native/claude/wire.py`](../native/claude/wire.py)
and [`native/claude/mcp.py`](../native/claude/mcp.py) now declare SDK MCP servers/configs and parse
typed `mcp_message` requests, and the full initialize/notification/list/call sequence — including
the required reply to notification-shaped messages — is scripted-test-pinned in
`harness_tests/claude/test_driver_tools.py`. That is native-driver evidence, not a runner
capability; the gap below is now purely a runner-adapter integration gap, not a wire-modeling one.

**Recommendation.** Wire the runner adapter to the now-pinned native shape: declare the runner's own
servers at `initialize`, add bidirectional `mcp_message` correlation reusing the Action Service
contracts rather than a second tool-request lifecycle (per
[driver_tools_and_background.md](../plans/driver_tools_and_background.md)), and preserve per-server
timeouts plus normalized error replies. Keep MCP task support disabled until an active `tools/call`
path is observed using it.

### C3: app-to-runner delivery is ambiguous after connection loss

**Bug.** Runner events are durable and cursor-replayable, but app commands are direct gRPC writes.
If that stream fails, the app cannot distinguish a command that never reached the runner from one
that arrived before the acknowledgement was lost. A stable command id resolves a retry only after
the runner has logged it.

**Required evidence.** Exercise loss before write, after write but before acknowledgement,
and after runner admission. Retain the same command id across retries and reload.
[The layering design](thread_layering.md#queue-placement-decision) owns the choice of
runner-first admission versus an app outbox; both need native crash-recovery proof.
Lack of an observed admission does not prove that a command was never delivered.

### C4: background state has no typed reset path

**Bug.** Claude background snapshots and edges currently survive only as `Native` events. A client
cannot consume the replace-set contract without harness-specific parsing, and replaying edges after
a worker restart can retain tasks that no longer exist.

**Recommendation.** Add a harness-aware adapter projection whose snapshot event replaces the
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

- runner `CommandAdmitted` and durable logging of the caller's input;
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
- A process loss after `queued` or even after local append acceptance still says nothing about
  Claude's transcript persistence; that is separate from the runner's durable command outcome.
- Resume can merge known UUIDs that do not yet have a local file and lazily hydrate driver-backed
  agent transcripts without overwriting locally written ones.

The broad transcript parser/reducer was not recovered in this pass.

### Compaction on the wire (measured 2.1.233; re-verify on 2.1.252)

Measured live on 2.1.233 with a forced auto-compaction (`--autocompact 100000`,
`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=60`); the frame sequence reproduced across runs at different
thresholds. No agentplane test pins it yet ([TODO](../native/TODO.md)).

**When it fires.** The `autocompact_state` frame, pushed at boot and whenever the resolution
changes, is authoritative:
`{enabled, effective_window, threshold, enforced, source}`. `--autocompact <tokens>` sets the window
(floor 100k, minus 20k headroom, so `100000` resolves to `effective_window: 80000`) and
`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` the fraction that trips; `CLAUDE_CODE_MAX_CONTEXT_TOKENS` does not
move it. **Gotcha:** `get_context_usage.autoCompactThreshold` is wrong whenever the percentage
override is set — it reported 67000 while `autocompact_state` said 48000, and compaction fired at
`pre_tokens: 49773`.

**What arrives, in order:**

```text
control_request  hook_callback PreCompact   { trigger: "auto", custom_instructions: null }
system/status    { status: "compacting" }
control_request  hook_callback SessionStart { source: "compact" }
system/status    { status: null, compact_result: "success" }
system/compact_boundary
user             isSynthetic: true — one text block, the summary
```

- `compact_boundary` carries accounting only: `compact_metadata` (`trigger`, `pre_tokens`,
  `post_tokens`, `cumulative_dropped_tokens` = pre − post, `duration_ms`,
  `pre_compact_discovered_tools`) and `logical_parent_uuid`. The latter names a turn in the CLI's
  on-disk transcript that never appears on the wire, so it cannot relink the stream.
- **The next frame rewrites the conversation** and looks like a prompt: a `user` frame whose single
  text block opens "This session is being continued from a previous conversation that ran out of
  context." `isSynthetic: true` is its only distinguishing mark. A consumer that renders `user`
  frames as user turns attributes the summary to the operator.
- `SessionStart` fires here with `source: "compact"`, and never at startup for hooks registered at
  `initialize` ([hooks](hooks.md)). `PreCompact` arrives before the compaction runs.
- The schema's `preserved_segment` (for a compaction that keeps a suffix) was absent: this shape
  summarised everything; the partial shape is unobserved.

**Against the runner's `Native` log** ([SPEC](../runner/SPEC.md) § Harness-originated messages):
compaction appends and retracts nothing — every earlier frame stays, unedited, with no uuid reused —
so a replay cursor over the log keeps its meaning. The cut is positional: everything before the
boundary is out of the model's context. A consumer folding every `user` frame into one
conversation gets the pre-compaction turns and their summary, the second copy as a user message.

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
Measured on 2.1.220: a `can_use_tool` left unanswered produced no `result` within 60 s, so a host
must answer every inbound control request, with an error if it implements none
([TODO](../native/TODO.md): how long the CLI actually waits).

## Background tasks

Treat `background_tasks_changed` as a complete replacement of the live set, including after a
process restart. `task_started`, `task_updated`, progress, and terminal notification frames are
detail edges, not a source from which to reconstruct the set.

The runtime maintains a per-session event queue capped at 1000 entries. Under pressure it
preferentially retains task lifecycle bookends and terminal status. Drain stamps fresh outer
`uuid`/`session_id` values. A terminal notification is guarded for once-only delivery and may carry
the originating `tool_use_id`, output file, summary, usage, transcript, and ambient metadata.

See [background_work.md](background_work.md) for the harness comparison. The app-state snapshot
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
"never sent" from "possibly delivered." A stable command id makes a retry idempotent only after
the runner has logged that id. Do not infer a delivery guarantee from a successful local write.

## Rate limits and refusal fallback

Claude's low-priority state machine opts eligible calls in with `anthropic-usage-limit: slow`, reads
the unified slow-limit headers, and tracks active/idle state plus five-hour/seven-day resets. Waiting
is jittered and bounded; weekly, budget, ineligible, disabled, wall-clock, and maximum-wait exits are
distinct. Reaching maximum wait ends low-priority mode and returns to the normal API error path.

Refusal fallback is separate from availability retry. It selects a category/catch-all route,
checks entitlement and model family, prevents retry loops, and may preserve safe partial output.
When policy needs a choice it parks on `retry_fallback`, `edit_prompt`, or `cancelled`; a newly queued
prompt cancels that parked dialog. A fallback can be latched for the session or limited to one
response. The dialog parks only if the host listed its kind (`refusal_fallback_prompt`) in
`initialize.supportedDialogKinds`; otherwise it fails closed to the classic refusal error, and the
first client to attach fixes the set for the session (schema reading, 2.1.220).

Until the runner handles these surfaces, preserve `rate_limit_event`, model-fallback frames, and
dialog requests as native evidence. Do not translate them into a generic retry or claim that a
host-mediated fallback succeeded.
