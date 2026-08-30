# Mid-turn user-message insertion

> Research reference only. This documents upstream protocol behavior for the pinned
> harnesses; it makes no runner or transcript behavior change.

## Scope and pins

Haku currently runs:

- Codex CLI/app-server `@openai/codex@0.144.1`, upstream tag `rust-v0.144.1`.
- Claude Code CLI `@anthropic-ai/claude-code@2.1.198`, upstream tag `v2.1.198`, with
  `claude-agent-sdk==0.2.141`.

The Codex pin is explicit in `haku/runner/codex/protocol.py` and the runner image
(`haku/x/dispatch/worker/Dockerfile`). The SDK and CLI pins are in the same image and
`requirements_bazel.txt`. The current runner implementations queue a prompt until the
harness is idle and expose only interrupt as an in-flight command:
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
the current model/tool activity. The turn loop drains pending input at an input
boundary; pending input causes a follow-up model pass after the current work. The
relevant drain and `needs_follow_up` logic is in
[`core/src/session/turn.rs`](https://github.com/openai/codex/blob/rust-v0.144.1/codex-rs/core/src/session/turn.rs).

The app-server emits an `item/started` notification whose item is a `userMessage`.
When `clientUserMessageId` is supplied, it is present as the item's `clientId`. The
upstream acceptance test waits for that notification while the original turn is still
active:
[`app-server/tests/suite/v2/turn_steer.rs`](https://github.com/openai/codex/blob/rust-v0.144.1/codex-rs/app-server/tests/suite/v2/turn_steer.rs).
The eventual `turn/completed` still refers to the same turn ID.

### Ordering and races

`expectedTurnId` is an active-turn precondition. The request is rejected when there is
no active turn (`NoActiveTurn`), the ID differs (`ExpectedTurnMismatch`), the active
turn is a review or compact turn (`ActiveTurnNotSteerable`), or `input` is empty
(`EmptyInput`). These errors are mapped in
[`app-server/src/request_processors/turn_processor.rs`](https://github.com/openai/codex/blob/rust-v0.144.1/codex-rs/app-server/src/request_processors/turn_processor.rs)
and the underlying checks are in
[`core/src/session/mod.rs`](https://github.com/openai/codex/blob/rust-v0.144.1/codex-rs/core/src/session/mod.rs).

The active-turn check and queue append are performed under the session's active-turn
state. A request that loses the race with completion is rejected as `NoActiveTurn` or,
if another turn has become active, `ExpectedTurnMismatch`; it is not silently converted
to a next-turn prompt. Multiple successful requests append in order.

### Interrupt interplay

`turn/interrupt` takes `threadId` and `turnId` and aborts the active task. It does not
add input. Its response is empty; the normal end-of-turn notification reports the
interrupted outcome. Its params are in
[`app-server-protocol/src/protocol/v2/turn.rs`](https://github.com/openai/codex/blob/rust-v0.144.1/codex-rs/app-server-protocol/src/protocol/v2/turn.rs); Haku's translation is in
`haku/runner/codex/harness.py`.

### Version constraint

The pinned `rust-v0.144.1` source contains the method, params, response, processor, core
queue append, and active-turn tests cited above. This establishes that Haku's Codex pin
has the capability. The first-version boundary was not established from the available
pinned source history; do not infer a minimum release from presence at `0.144.1`.

## Claude Code stream-json protocol

### Wire mechanism

The insertion is a `type: "user"` NDJSON line on Claude Code's already-open stdin.
The pinned SDK writes this line through `ClaudeSDKClient.query(...)`, but the wire
contract belongs to Claude Code's `--input-format stream-json` mode:

```json
{
  "type": "user",
  "message": {"role": "user", "content": "new instruction"},
  "parent_tool_use_id": null,
  "session_id": "session-id",
  "uuid": "client-generated-message-id"
}
```

The SDK's subprocess command uses `--output-format stream-json --verbose` and
`--input-format stream-json`; its write path serializes each input object as one JSON
line. The SDK-side implementation is in
[`src/claude_agent_sdk/client.py`](https://github.com/anthropics/claude-agent-sdk-python/blob/v0.2.141/src/claude_agent_sdk/client.py)
and
[`src/claude_agent_sdk/_internal/transport/subprocess_cli.py`](https://github.com/anthropics/claude-agent-sdk-python/blob/v0.2.141/src/claude_agent_sdk/_internal/transport/subprocess_cli.py).

This is not a control request. Claude Code's control channel uses a different frame:

```json
{
  "type": "control_request",
  "request_id": "request-id",
  "request": {"subtype": "interrupt"}
}
```

The CLI answers controls with `type: "control_response"` keyed by `request_id`. The
SDK exposes this channel for `interrupt()` and other controls, but no SDK method is
needed for insertion: insertion is another stream-json user frame. The framing and
request/response implementation is in
[`src/claude_agent_sdk/_internal/query.py`](https://github.com/anthropics/claude-agent-sdk-python/blob/v0.2.141/src/claude_agent_sdk/_internal/query.py).

### Where it lands

Claude Code reads stdin concurrently with active generation. A user frame received
while a turn is running enters the CLI's queued-command path as a prompt; it is not an
interrupt and does not start a second process. The pinned CLI changelog explicitly
refers to user prompts queued mid-turn, fixes queued prompts disappearing, and records
an earlier fix for real-time steering:
[`CHANGELOG.md`](https://github.com/anthropics/claude-code/blob/v2.1.198/CHANGELOG.md).

The public Claude Code repository does not ship the compiled CLI's internal queue or
model loop, so it does not establish the exact model-call boundary. The safe protocol
fact is narrower: the line enters the live CLI input stream and is scheduled as a prompt
according to CLI behavior; it does not cancel the in-flight model request. The exact
boundary needs an integration test against the `2.1.198` binary.

With `--replay-user-messages`, Claude Code re-emits each stdin user message as a
`type: "user"` stream message for acknowledgement. The option was added in Claude Code
`1.0.86` and is present at `2.1.198`. Without that flag, the SDK does not promise a
user-frame echo. SDK `UserMessage` parsing is therefore parsing for frames the CLI may
emit, not proof that every inserted line is acknowledged. See
[`src/claude_agent_sdk/_internal/message_parser.py`](https://github.com/anthropics/claude-agent-sdk-python/blob/v0.2.141/src/claude_agent_sdk/_internal/message_parser.py)
and the pinned CLI
[`CHANGELOG.md`](https://github.com/anthropics/claude-code/blob/v2.1.198/CHANGELOG.md).

### Ordering and races

The SDK serializes writes, so multiple callers cannot interleave JSON bytes. The CLI
receives them in stdin order. Claude Code may queue multiple prompt frames and coalesce
queued prompts into a later prompt turn; the SDK exposes no turn ID, insertion response,
or current-turn-versus-next-turn result. The changelog establishes the behavior class,
not a per-message delivery receipt or scheduling guarantee.

If the active turn finishes before a queued line is processed, the public protocol does
not define whether it is folded into the next prompt, rejected, or dropped. A
`--replay-user-messages` echo establishes that the CLI processed the line, but does not
identify the model turn that used it. Whether several lines merge or split must be
 tested against the pinned binary.

### Interrupt interplay

`interrupt` is a control request with an explicit response; a user frame is prompt input
with no request/response envelope. `ClaudeSDKClient.interrupt()` sends the former and
does not send a replacement prompt. Haku's current interrupt path also uses the control
request, with `cancel_queued: true` in the SDK request. Insertion instead writes one
`type: "user"` line and leaves the active task running.

### Version constraint

Claude Code `2.1.198` is downstream of the relevant protocol milestones: stream-json
output was added in `0.2.66`, `--replay-user-messages` in `1.0.86`, and the changelog
contains subsequent fixes for real-time steering and queued mid-turn prompts. Those
entries are in the pinned upstream
[`CHANGELOG.md`](https://github.com/anthropics/claude-code/blob/v2.1.198/CHANGELOG.md).
This establishes that the pinned CLI has the protocol and queued-prompt behavior. The
first version that accepted a user frame during an active turn, and the exact minimum
version for reliable insertion, are not recoverable from the public changelog; test the
`2.1.198` binary directly.

## Runner and transcript shape

These are implementation facts and options, not decisions.

- The runner protocol could add a console-to-runner command alongside `Interrupt`, carrying
  an admitted prompt ID, text, and (if needed) a target active-turn identity. Codex would
  translate it to `turn/steer`; Claude would translate it to one stream-json `type: "user"`
  line.
- Codex can acknowledge acceptance with the JSON-RPC response and native `item/started`
  user-message notification. Claude can provide a processed-input acknowledgement only if
  the runner enables `--replay-user-messages`; otherwise it needs its own correlation and
  pending state.
- The transcript/projection model needs an authored prompt item inside an already-open
  turn bracket, ordered relative to native frames and carrying a stable prompt ID. It
  must not accidentally open a second neutral turn or be rewritten as an interrupt.
- The projection must represent an admitted insertion with no native acknowledgement
  because the harness exits or the turn races to completion. Whether that is pending,
  failed, or unconfirmed is an open projection question.
- Codex has `expectedTurnId` for stale-turn failure. Claude has no equivalent acceptance
  response, so the runner may need its own open-turn generation/sequence guard.
- It remains open whether insertion is a delivery mode for a prompt already admitted by
  the durable inbox, or a separate lane with its own admission record. The choice must
  preserve reconnect replay and distinguish next-turn delivery from current-turn
  insertion.
