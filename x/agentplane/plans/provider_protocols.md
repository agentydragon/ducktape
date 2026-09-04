# Claude Code and Codex protocol notes

Status: **provider evidence for the native drivers**.

The first implementation speaks each native protocol directly. This document records only the
facts needed to write the drivers and tests. It does not define a neutral API, compatibility-profile
system, or product persistence model.

## Shared rules

- Use real stdin/stdout pipes, not PTYs, tmux, pane scraping, or prompt heuristics.
- Keep Claude and Codex driver state separate until captures demonstrate a useful common seam.
- Preserve complete native frames and provider-native ids in the ordered transcript during live
  probe runs; the logs stay outside Git.
- Record upstream LLM request bodies and streamed response chunks separately from native frames;
  the scripted tests assert request markers and build responses rather than replaying recordings.
- Do not serialize HTTP headers, cookies, environment variables, credentials, or OAuth state.
- Use explicit binary paths or simple environment variables; do not build package-resolution or
  executable-integrity infrastructure.
- A version string is useful human-readable metadata, not a compatibility gate.

The existing `haku/runner`, `haku/cli_protocol`, and `haku/console` code is behavior evidence only.
The new implementation under `x/agentplane/` must not import it.

When a harness version changes or a new protocol area needs coverage, run the live probe with the
new pinned binary, compare what the harness sends with the scripted expectations, and update the
driver/tests only for the behavior we choose to pin down, together with the binary pin.

## Model endpoint boundary

The existing Haku deployment routes Claude's Anthropic Messages traffic and Codex's OpenAI Responses
traffic through LiteLLM and CLIProxyAPI. That is experiment/deployment plumbing. Agentplane does not
own consumer login, OAuth refresh, credential delivery, or model routing.

The recording proxy records only request/response bodies and response chunks. The scripted tests
stand in for the endpoint with a loopback server the test drives request by request
([`../harness_tests/scripted_upstream.py`](../harness_tests/scripted_upstream.py)), so the native
driver is exercised without paid inference.

## Claude Code

### Launch and transport

The observed launch uses stream JSON in both directions, with the relevant shape:

```text
claude
  --output-format stream-json
  --verbose
  --include-partial-messages
  --input-format stream-json
```

The native driver uses the actual launch arguments supported by the resolved binary and
record the resulting transcript. The wire is newline-delimited JSON over stdin/stdout.

### Initialization

The driver sends an initialization control request and waits for the correlated response before
starting scenario traffic:

```json
{
  "type": "control_request",
  "request_id": "req_...",
  "request": { "subtype": "initialize" }
}
```

Required tests should prove the actual response shape rather than relying on stdin ordering.

### Prompt and output

A normal prompt is a user frame, commonly shaped like:

```json
{
  "type": "user",
  "message": { "role": "user", "content": "..." },
  "parent_tool_use_id": null,
  "uuid": "client-generated-uuid"
}
```

Capture the UUID and all related lifecycle frames in the native transcript. Observed output includes
assistant messages, thinking/tool-use blocks, tool results, partial `stream_event` records, command
lifecycle records, system records, and a terminal `result` record. The driver should preserve all of
them and assert only the events needed by the scenario.

### Tools, steering, and interrupt

For the first tool scenario, exercise one deterministic shell or structured tool and assert the
native tool call, result, upstream model exchange, and workspace effect. Do not build a broad common
tool taxonomy.

Claude exposes a correlated interrupt control request. The test must distinguish the request and
its response from the later native terminal result.

A second user frame during an active turn may be queued until a tool boundary, treated as a later
prompt, or ignored until completion. Test a controlled active case and record what actually happens.
Do not call it steering in a neutral API until the wire proves that distinction.

### Resume

Use the provider's native session-resume mechanism after killing only an idle child. Assert recovery
of a nonce held in model context. Do not call replaying a bridge journal or resending a prompt Claude
resume.

## Codex app-server

### Launch and handshake

The observed launch is:

```text
codex app-server --listen stdio://
```

The wire is newline-delimited JSON-RPC-shaped messages over stdio. The driver must support the
handshake needed by the tested binary:

1. send `initialize`;
2. await its response;
3. send `initialized`; and
4. start or resume a thread.

Handle server requests required by the selected scenario. Do not assume every stdout message is a
notification.

### Thread and turn

For a resumable test, start a durable rather than ephemeral thread and retain its native thread id.
A typical shape is:

```json
{
  "method": "thread/start",
  "id": 10,
  "params": { "cwd": "/workspace/project", "ephemeral": false }
}
```

A turn commonly uses:

```json
{
  "method": "turn/start",
  "id": "input-1",
  "params": {
    "threadId": "thr_...",
    "clientUserMessageId": "input_...",
    "input": [{ "type": "text", "text": "Run the tests" }]
  }
}
```

Assert the actual response and notification order. Relevant output includes user messages, agent
messages and deltas, reasoning, plans, command execution, file changes, tool calls, item completion,
and `turn/completed`.

### Tools, steering, and interrupt

Use one deterministic command or structured tool for the initial tool scenario. Preserve the native
item and result messages; do not create a common operation projection yet.

Codex exposes `turn/steer` for an active thread/turn (it requires `expectedTurnId` and joins the
running turn) and `turn/interrupt` for interruption. Test the native request and the resulting
item/turn evidence. A request acknowledgement is not itself a
terminal outcome.

A second `turn/start` while a turn is active is a separate experiment from `turn/steer`. Both join
the active turn: Core appends the input to that turn's in-memory pending-input list
(`core/src/session/input_queue.rs`), drained only after the current model response and every tool
call it requested have finished (`core/src/session/turn.rs`), never mid-stream. Joining is
recorded into history (and echoed as `item/started`/`item/completed` for a `userMessage` item
carrying back `clientUserMessageId`) at the moment it drains, immediately before the next model
request goes out — there is no separate "now sending to the model" signal. `turn/interrupt` is the
only cancellation primitive for joined input, and it is not selective: aborting the turn also
clears whatever was pending for it (`InputQueue::clear_pending`), so there is no way to pull back
one joined message without killing the turn it joined.

Codex additionally ships a **separate, durable queue** unrelated to steering/joining
(`codex-rs/ext/queue`, present in the `rust-v0.152.0` pin): `thread/queue/{add,list,update,delete,
reorder,start}` plus a `thread/queue/changed {threadId}` notification (all `#[experimental(...)]`
on the wire — opt-in requirements against the pinned binary are unconfirmed). Unlike joining, a
queued item does not affect the active turn at all; it is SQLite-backed
(`codex-thread-store`'s `QueueStore`) and only turns into a new turn once the thread goes fully
idle (`QueuedItemService::dispatch_if_idle`, wired off the `on_thread_idle` lifecycle hook, which
explicitly skips dispatch when the idle cause was `Interrupted`). Every mutating call
(`add`/`update`/`delete`/`reorder`) and the idle-triggered dispatch itself take the same per-thread
lock, so `thread/queue/delete` cannot race the moment a queued item gets promoted to a turn:
`ThreadQueueDeleteResponse.deleted` tells the caller definitively whether the item was actually
still in the queue to remove. This is the shape that supports a clean
enqueued-but-not-yet-turn/dequeued-before-turn state machine; joining does not.

Promotion itself is observable, not just inferrable from absence: `thread/queue/add`'s
`clientUserMessageId` is threaded straight into the stored item's `TurnInput::UserInput.client_id`
(`thread_queue_processor.rs::add` → `submission_into_turn_input`), and dispatch starts the queued
item as ordinary turn-start input, so it goes through the exact same
`record_user_prompt_and_emit_turn_item` path as joining — the same `item/started`/`item/completed`
`userMessage` pair, carrying that same `clientUserMessageId` back as `client_id`, fires the moment
Core records it into history, immediately before it goes out in the turn's first model request.
Concretely, promotion looks like: a `thread/queue/changed {threadId}` (the delete-on-dispatch that
removed it from the queue, indistinguishable on its own from any other queue mutation), a
`turn/started` for a brand-new turn (dispatch always starts fresh, never joins), then
`item/started`/`item/completed` for the `userMessage` item — that last pair, matched by
`clientUserMessageId`, is the actual "entered the transcript, about to be sent" signal. Not yet
exercised by the driver or captured live — the earlier "not observed" note in
<../native/docs/protocol_roster.md> reflected that the probe only ever tried the implicit
second-`turn/start` path, never the explicit `thread/queue/*` methods.

### Resume

Persist the native Codex state required by the tested binary, retain `thread.id`, kill the idle
app-server, and use `thread/resume`. Assert model-context continuity separately from workspace
survival. Do not infer continuity from a product transcript.

## P0 driver contract

Each provider driver should support only the operations required by the initial scenarios:

```text
launch_and_initialize()
start_or_resume_native_thread()
send_prompt(...)
read_until_terminal()
run_tool_scenario(...)
steer_if_supported(...)
interrupt(...)
resume_after_idle_restart(...)
```

These are test-driver operations, not the future Agentplane public API. Return explicit unsupported
results when the tested binary lacks a surface; do not add compatibility abstractions to conceal the
fact.
