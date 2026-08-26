# Codex app-server protocol evidence

This package is pinned to the Codex version already installed in the `agent-workspace` image:
`@openai/codex@0.144.1` (`cluster/k8s/agents/agent-sandbox/workspace-image/Dockerfile`). The npm
package declares the six platform packages at that same version and launches their native binary.
The corresponding authoritative source is OpenAI's tag `rust-v0.144.1`, peeled commit
`44918ea10c0f99151c6710411b4322c2f5c96bea`.

The claims implemented here were checked against that tag, in these vendored/generated artifacts:

- `codex-rs/app-server/README.md`: newline-delimited stdio transport, omitted `jsonrpc` header,
  initialization handshake, and thread/turn/item lifecycle.
- `codex-rs/app-server-protocol/schema/typescript/ClientRequest.ts` and
  `ClientNotification.ts`: `initialize`, `thread/start`, `turn/start`, and `initialized` envelopes.
- `codex-rs/app-server-protocol/src/rpc.rs`: request IDs may be strings, which lets each replacement
  Console namespace its requests away from late responses addressed to its predecessor.
- `schema/typescript/v2/ThreadLoadedListParams.ts`, `ThreadLoadedListResponse.ts`,
  `ThreadReadParams.ts`, and `ThreadReadResponse.ts`: reconnect discovery and recovery of an active
  turn ID for interruption after Console adoption.
- `schema/typescript/v2/ThreadItem.ts`: the complete item union and terminal payload fields.
- `AgentMessageDeltaNotification.ts`, `ReasoningSummaryTextDeltaNotification.ts`,
  `CommandExecutionOutputDeltaNotification.ts`, `ItemStartedNotification.ts`, and
  `ItemCompletedNotification.ts`: stable item IDs on lifecycle and delta notifications.
- `CommandExecutionStatus.ts`, `McpToolCallStatus.ts`, and `TurnStatus.ts`: the status enums mapped by
  the adapter.
- `McpToolCallResult.ts`: MCP `content`, `structuredContent`, and `_meta` result payloads.
- `codex-rs/cli/src/mcp_cmd.rs` and its tests: streamable-HTTP MCP configuration uses `url` and
  `bearer_token_env_var`, so Codex can read the claim-owned exact-session bearer without placing its
  value in argv or Console launch material.
- `schema/typescript/ServerNotification.ts`, `schema/typescript/v2/ErrorNotification.ts`,
  `TurnError.ts`, and `CodexErrorInfo.ts`: the `error` notification and the failure payload it
  shares with `Turn.error`, described under "Turn failures" below.
- `codex-rs/tui/src/thread_transcript.rs`: completed reasoning summary parts render joined with two
  newlines, matching `item/reasoning/summaryPartAdded` in the live TUI.

`codex app-server generate-ts` and `generate-json-schema` produce version-specific schemas from the
installed binary. Use those commands when the image pin moves; do not assume a newer online schema
still describes 0.144.1.

## Projection boundary

The adapter consumes server notifications and produces the existing types from
`haku.console.x.conversation_events`. It does not define a new event vocabulary. The implementation
is linked for projection and implements the common runtime/client/runner seams, but has no
production execution resources or conversation writer and is therefore not launchable.

Supported now:

- agent-message start, text deltas, and completion (completion text contributes only an
  undelivered suffix);
- reasoning-summary start, deltas, and completion as `disclosure=summary`;
- command execution start/output/completion, including exit status and duration in `structured`;
- MCP tool call start/completion, rendered result content, and native structured payload;
- completed/interrupted/failed turn outcomes.

Deliberately ignored because the conversation schema assigns them elsewhere or rejects the detail:
thread status, token usage, `turn/started`, MCP progress narration, and server-request resolution.
User-message items are also ignored: prompts are console-authored before the backend claims them.

Currently counted as `unprojected`, preserving fail-soft observability: file changes, web search,
plans, raw reasoning text, dynamic/collaboration tool items, and any future method or item type.
They remain in the native frame log for a later explicitly reviewed mapping.

The committed `testdata/real_text_command.sanitized.jsonl` is the reviewed real capture supplied
with issue #4431's staging notes (`.openclaw/codex-trace-4431/README.md`). It observed two bounded
turns on 2026-08-19 UTC: text deltas/completion, then reasoning item completion, command execution
start/completion, and a second text answer. `testdata/schema_derived_turn.synthetic.jsonl` remains
synthetic and exists only to cover schema-supported MCP, output-delta, and future-item cases that
the safe capture did not exercise.

## Turn failures

`Turn.error` is documented upstream as "only populated when the Turn's status is failed", and it
holds the same `TurnError` that the standalone `error` notification carries:

```typescript
type ErrorNotification = { error: TurnError; willRetry: boolean; threadId: string; turnId: string };
type TurnError = { message: string; codexErrorInfo: CodexErrorInfo | null; additionalDetails: string | null };
```

Three properties of that pair decide how a reader has to treat it:

- **`willRetry` is Codex's own retryability verdict**, per turn, and the retries are Codex's, not
  the client's: it emits one `error` notification per attempt while it is still trying, then repeats
  the same failure with `willRetry: false` when it gives up.
- **`message` and `additionalDetails` swap roles between those two.** While retrying, `message` is a
  bare progress counter (`Reconnecting... 3/5`) and `additionalDetails` holds the provider's reason;
  on the terminal notification and on `turn.error`, `message` holds the reason and
  `additionalDetails` is null. Reading `message` alone renders the counter as the failure.
- **`CodexErrorInfo` is an externally tagged enum**, so a variant is either a bare string
  (`serverOverloaded`, `usageLimitExceeded`, `contextWindowExceeded`, `internalServerError`,
  `unauthorized`, `sandboxError`, `other`, …) or a single-key object carrying an upstream HTTP status
  (`{"responseStreamDisconnected": {"httpStatusCode": null}}`). It is the stable category; the
  message is prose. 0.144.1 declares sixteen variants and the set grows, so a reader decodes an
  unrecognized one to a named unknown variant rather than raising or guessing a neighbor.

`testdata/real_provider_failure.sanitized.jsonl` is the captured production failure these claims are
read off. It also shows the thread going to `thread/status/changed → {"type": "systemError"}` one
frame before the terminal error, which is the protocol's own statement that the failure was not
turn-local. Whether `turn/start` on such a thread is accepted is not settled by the capture: no
follow-up turn was submitted.

The adapter currently extracts only `turn.error.message`, into the transient
`TurnCompletion.failure`; `TurnCompleted` carries an outcome and no reason, so nothing durable
survives, and all six `error` notifications land in `unprojected`. Issue #4752 tracks closing that.
