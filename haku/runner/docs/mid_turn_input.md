# Mid-turn user-message insertion

> Research reference only. This documents upstream protocol behavior for the pinned
> harnesses; it makes no runner or transcript behavior change.

## Scope and pins

Haku currently runs:

- Codex CLI/app-server `@openai/codex@0.144.1`, upstream tag `rust-v0.144.1`.
- Claude Code CLI `@anthropic-ai/claude-code@2.1.198` with
  `claude-agent-sdk==0.2.141`.

The Codex pin is explicit in `haku/runner/codex/protocol.py` and the runner image
(`haku/x/dispatch/worker/Dockerfile`). The SDK pin is in `requirements_bazel.txt`; the
CLI pin is in the same image. The current runner implementations queue a prompt until
the harness is idle and expose only interrupt as an in-flight command:
`haku/runner/codex/harness.py`, `haku/runner/claude/harness.py`, and
`haku/runner/protocol.py`.

This feature is distinct from both a prompt admitted at the next turn boundary and
interrupt followed by a new prompt.

## Codex app-server

### Wire mechanism

Use the JSON-RPC request method `turn/steer`. Its v2 params are:

```json
{
  "threadId": "thread-id",
  "expectedTurnId": "active-turn-id",
  "input": [
    { "type": "text", "text": "new instruction", "textElements": [] }
  ],
  "clientUserMessageId": "optional-client-id",
  "additionalContext": {},
  "responsesapiClientMetadata": {}
}
```

`threadId`, `expectedTurnId`, and non-empty `input` are the important fields. The
optional `clientUserMessageId` is carried onto the resulting user-message item;
`additionalContext` and `responsesapiClientMetadata` are separate optional metadata
paths. A successful response is `{ "turnId": "active-turn-id" }`; it does not create a
new turn. The protocol registration and structs are in
[`app-server-protocol/src/protocol/common.rs`](https://github.com/openai/codex/blob/rust-v0.144.1/codex-rs/app-server-protocol/src/protocol/common.rs),
[`app-server-protocol/src/protocol/v2/turn.rs`](https://github.com/openai/codex/blob/rust-v0.144.1/codex-rs/app-server-protocol/src/protocol/v2/turn.rs), and the generated schema is
[`app-server-protocol/schema/json/v2/TurnSteerParams.json`](https://github.com/openai/codex/blob/rust-v0.144.1/codex-rs/app-server-protocol/schema/json/v2/TurnSteerParams.json).

The server calls `thread.steer_input(...)`, which appends a `TurnInput::UserInput` to
the active turn's pending-input queue and wakes the turn's input queue. This is the
implementation in
[`core/src/session/mod.rs`](https://github.com/openai/codex/blob/rust-v0.144.1/codex-rs/core/src/session/mod.rs).

### Where it lands

The insertion is turn-local. It is not a second `turn/start`, and it does not interrupt
the current model/tool activity. The turn loop drains pending input when it reaches an
input boundary; pending input makes the turn take a follow-up model pass after the
current work rather than waiting for a new top-level turn. The relevant drain and
`needs_follow_up` logic is in
[`core/src/session/turn.rs`](https://github.com/openai/codex/blob/rust-v0.144.1/codex-rs/core/src/session/turn.rs).

The app-server emits an `item/started` notification whose item is a `userMessage`.
When `clientUserMessageId` is supplied, it is present as the item's `clientId`. The
upstream acceptance test waits for that `item/started` notification while the original
turn is still active:
[`app-server/tests/suite/v2/turn_steer.rs`](https://github.com/openai/codex/blob/rust-v0.144.1/codex-rs/app-server/tests/suite/v2/turn_steer.rs).

The JSON-RPC response is an acceptance acknowledgement. The user-message item is the
stream acknowledgement that the input entered the open turn. The eventual
`turn/completed` still refers to the same turn ID.

### Ordering and races

`expectedTurnId` is an active-turn precondition. The request is rejected when:

- there is no active turn (`NoActiveTurn`);
- the supplied ID differs from the active turn (`ExpectedTurnMismatch`);
- the active turn is a review or compact turn (`ActiveTurnNotSteerable`); or
- `input` is empty (`EmptyInput`).

These errors are mapped in
[`app-server/src/request_processors/turn_processor.rs`](https://github.com/openai/codex/blob/rust-v0.144.1/codex-rs/app-server/src/request_processors/turn_processor.rs)
and the underlying error type/checks are in
[`core/src/session/mod.rs`](https://github.com/openai/codex/blob/rust-v0.144.1/codex-rs/core/src/session/mod.rs).

The active-turn check and queue append are performed under the session's active-turn
state, so an insertion that loses the race with turn completion is rejected as
`NoActiveTurn` or, if another turn has become active, `ExpectedTurnMismatch`. It is not
silently converted into a next-turn prompt by `turn/steer`. There is no separate
“accepted for this turn” versus “carried to next turn” result: a successful response and
the `userMessage` item mean current-turn acceptance; an error means no insertion.

Multiple successful `turn/steer` requests append multiple pending inputs. The queue
preserves append order. Each request should use the currently observed active turn ID;
the response returns that same ID.

### Interrupt interplay

`turn/interrupt` takes only `threadId` and `turnId` and aborts the active task. It does
not add input. Its response is empty, and the normal end-of-turn notification reports
the interrupted outcome. The params are in
[`app-server-protocol/src/protocol/v2/turn.rs`](https://github.com/openai/codex/blob/rust-v0.144.1/codex-rs/app-server-protocol/src/protocol/v2/turn.rs);
Haku's current translation is in `haku/runner/codex/harness.py`.

Steering therefore means “append input and let the active turn continue”; interrupt
means “abort the active turn.” Combining them is a caller-level sequence, not the
meaning of `turn/steer`.

### Version constraint

The pinned `rust-v0.144.1` source contains the `turn/steer` method, v2 params, response,
server processor, core queue append, and active-turn tests cited above. This establishes
that Haku's Codex pin has the capability. The first-version boundary was not established
from the available pinned source history; do not infer a minimum release from the
presence of the feature at `0.144.1`. A future implementation should establish that
boundary from Codex release history before lowering or changing the image pin.

## Claude Code via `claude-agent-sdk`

### Wire mechanism

The bidirectional API is `ClaudeSDKClient.query(...)`, not the one-shot `query(...)`
function. `ClaudeSDKClient.query` accepts either a string or an async iterable of
message dictionaries and writes each dictionary to the running transport. At
`claude-agent-sdk==0.2.141`, a string is encoded as one newline-delimited JSON object:

```json
{
  "type": "user",
  "message": {"role": "user", "content": "new instruction"},
  "parent_tool_use_id": null,
  "session_id": "default"
}
```

For an async iterable, each yielded dictionary is written as one JSON line; the SDK
adds `session_id` when it is absent. The public method is in
[`src/claude_agent_sdk/client.py`](https://github.com/anthropics/claude-agent-sdk-python/blob/v0.2.141/src/claude_agent_sdk/client.py);
the raw write loop is in
[`src/claude_agent_sdk/_internal/transport/subprocess_cli.py`](https://github.com/anthropics/claude-agent-sdk-python/blob/v0.2.141/src/claude_agent_sdk/_internal/transport/subprocess_cli.py).

The SDK launches Claude Code with `--input-format stream-json` and
`--output-format stream-json` in the subprocess transport. Stdin must remain open;
`ClaudeSDKClient` is the stateful API intended for sending while receiving. The
one-shot `query(...)` helper is documented as unable to interrupt or send follow-ups.

The minimum user frame accepted by the SDK parser has `type: "user"` and
`message.content`; `parent_tool_use_id` may be null. A caller that needs stable
correlation can add a `uuid`; the SDK's own examples and types treat user frames as
first-class `UserMessage` stream entries. Haku currently supplies `uuid` and
`parent_tool_use_id: null` in `haku/runner/claude/harness.py`.

### Where it lands

Claude Code's stream-json input is a live input stream, so a user JSON line written
while Claude is processing is consumed by the same CLI session rather than requiring a
new process or an interrupt. The SDK exposes the resulting `UserMessage` on the output
stream and `receive_messages()` keeps yielding until the process/session ends;
`receive_response()` stops after a `ResultMessage` and is therefore not the right reader
for an insertion that may arrive before the current result.

The SDK source documents and tests the bidirectional client and concurrent send/receive
pattern, but does not define a model-call scheduling boundary equivalent to Codex's
input queue. The exact placement is therefore a Claude Code CLI behavior, not an SDK
guarantee: the line is accepted by the live stream, and Claude Code incorporates it at
the next point at which the CLI can absorb another user message. The SDK source does
not promise that an in-flight model request is cancelled or that the new text is visible
to that already-started request.

The output `type: "user"` frame is the protocol-level acknowledgement that the CLI has
accepted/emitted the message. The SDK parser maps it to `UserMessage`; see
[`src/claude_agent_sdk/_internal/message_parser.py`](https://github.com/anthropics/claude-agent-sdk-python/blob/v0.2.141/src/claude_agent_sdk/_internal/message_parser.py).

### Ordering and races

The SDK serializes writes with the transport write lock, so calls to `query()` do not
interleave bytes. The SDK does not provide a request ID or “inserted into current turn”
response. A caller must correlate by its own `uuid` or by the emitted `UserMessage`.

The SDK source does not specify what the CLI does if stdin receives a line after the
current result has been produced but before stdin closes. In particular, it does not
promise that such a line becomes a new turn, is rejected, or is dropped. The current
SDK's stdin-lifecycle code explicitly has edge cases around result frames, queued input,
and in-flight tasks; see
[`src/claude_agent_sdk/_internal/query.py`](https://github.com/anthropics/claude-agent-sdk-python/blob/v0.2.141/src/claude_agent_sdk/_internal/query.py).
This is an implementation/race question for a harness integration test against the
pinned CLI, not something to assume from the SDK method signature.

Multiple `ClaudeSDKClient.query()` calls can be made while the client is connected, and
the transport writes them in call order. Whether multiple messages are absorbed into
one active Claude turn or split across consecutive turns is CLI scheduling behavior;
the SDK does not expose a per-message turn result.

### Interrupt interplay

`ClaudeSDKClient.interrupt()` sends a stream-json control request with
`{"subtype": "interrupt"}` through the CLI control channel. It is a cancellation
operation, not a user message. The SDK's public method and internal control request are
in [`src/claude_agent_sdk/client.py`](https://github.com/anthropics/claude-agent-sdk-python/blob/v0.2.141/src/claude_agent_sdk/client.py)
and [`src/claude_agent_sdk/_internal/query.py`](https://github.com/anthropics/claude-agent-sdk-python/blob/v0.2.141/src/claude_agent_sdk/_internal/query.py).

Haku's current Claude interrupt sets `cancel_queued: true` in its control request so an
operator stop does not immediately start an already queued prompt. Mid-turn insertion
must instead write a `type: "user"` line and leave the active task running.

### Version constraint

The capability is present in the pinned SDK: `ClaudeSDKClient` is present in the SDK's
first released streaming-client line (`v0.1.0`), with `query()` writing user JSON lines,
`interrupt()` sending the control request, and the subprocess using
`--input-format stream-json`. The v0.2.141 source retains that API and transport.

This establishes `v0.2.141` as capable. It does not establish a CLI version minimum for
mid-turn absorption: the SDK delegates scheduling to Claude Code, and the CLI source is
not part of the Python SDK repository. Haku's pinned CLI is `2.1.198`; validate any
claimed lower CLI minimum with a pinned-CLI wire test rather than with SDK version
history alone.

## Runner and transcript shape

These are implementation facts and options, not decisions.

- The runner protocol could add a console-to-runner command alongside `Interrupt`, carrying
  an admitted prompt ID, text, and (if needed) a target active-turn identity. Codex would
  translate it to `turn/steer`; Claude would translate it to one stream-json `type: "user"`
  line.
- Codex can acknowledge acceptance with the JSON-RPC response and then the native
  `item/started` user-message notification. Claude has no equivalent request response;
  the emitted `UserMessage` frame is the useful native acknowledgement.
- The transcript/projection model needs an authored prompt item inside an already-open
  turn bracket, with ordering relative to native frames and a stable prompt ID. It must
  not accidentally open a second neutral turn or be rewritten as an interrupt.
- The projection must represent the case where an insertion is admitted but no native
  acknowledgement arrives because the harness exits or the turn races to completion.
  Whether that is pending, failed, or an unconfirmed authored item is an open projection
  question.
- For Codex, `expectedTurnId` gives a precise stale-turn failure. For Claude, the runner
  may need its own open-turn generation/sequence guard because the SDK has no equivalent
  acceptance response.
- It remains open whether insertion is a delivery mode for a prompt already admitted by
  the durable inbox, or a separate lane with its own admission record. The choice must
  preserve reconnect replay and distinguish next-turn delivery from current-turn
  insertion.
