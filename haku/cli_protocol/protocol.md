# The Claude Code CLI stream protocol

Reference for the newline-delimited JSON protocol Haku's console speaks to `claude`, written
against **claude-code 2.1.220** as bundled in `claude_agent_sdk` 0.2.128. It covers what the
console needs, not the CLI's whole surface: fields the CLI defines for host UIs (VS Code, Claude
Desktop, Remote Control) are named where they explain a frame and otherwise left out.

Two sources back it. Structure and field semantics come from the zod schemas in the bundled
binary, which carry their own `.describe()` text. Behaviour comes from live runs in <probes/>,
each of which prints every frame in both directions. Where the two disagree the probe wins, and
the reader should assume anything not stated as measured is a schema reading.

The CLI describes many of these fields as `@internal`, so treat the whole surface as
version-pinned: re-run the probes on a repin.

## Transport

```bash
claude --print --input-format stream-json --output-format stream-json --verbose
```

One JSON object per line in each direction, over the process's stdin/stdout. The console reaches
that process through the runner's websocket rather than a pipe, but nothing in the protocol
depends on which.

Two channels are multiplexed on the one stream and are distinguished by the top-level `type`:

- **Conversation** — append-only, the record of what happened. `user` frames in; `assistant`,
  `user`, `system`, `result`, `stream_event` and friends back.
- **Control** — request/response, correlated by `request_id`, in **both** directions.

The two are independent: a control response never appears in the conversation, and a conversation
frame is never a reply to anything. A client that stops draining one stalls the other, since they
share a stream.

## Conversation frames

| `type`              | Carries                                                                   |
| ------------------- | ------------------------------------------------------------------------- |
| `user`              | A prompt (outbound) or a `tool_result` block (inbound)                    |
| `assistant`         | One assistant message: `text`, `thinking`, `tool_use` blocks              |
| `result`            | End of turn: outcome, cost, usage, timings, `structured_output`           |
| `stream_event`      | Raw Anthropic streaming deltas, only with `--include-partial-messages`    |
| `system`            | Out-of-band session events, discriminated further by `subtype`            |
| `rate_limit_event`  | Current rate-limit window and overage state                               |
| `command_lifecycle` | State of one submitted prompt (see [Prompt lifecycle](#prompt-lifecycle)) |
| `active_goal`       | The session's current goal, `null` when none                              |

A nested frame — one produced by a subagent rather than the main thread — carries
`parent_tool_use_id` naming the `Task` tool use it belongs to.

`system` subtypes the console sees:

| `subtype`           | Carries                                                                                        |
| ------------------- | ---------------------------------------------------------------------------------------------- |
| `init`              | Session identity, tools, MCP servers, `capabilities`                                           |
| `commands_changed`  | The slash-command/skill catalog                                                                |
| `task_started`      | `task_id`, `tool_use_id`, `description`, `subagent_type`, `task_type`, `prompt`                |
| `task_progress`     | `description` ("Running …"), `last_tool_name`, `usage` (tokens, tool uses, elapsed)            |
| `task_updated`      | `patch` — `status`, `end_time`                                                                 |
| `task_notification` | Terminal `status`, `output_file`, and a prose `summary`                                        |
| `post_turn_summary` | `status_category` (`review_ready` / `blocked` / `need_input`), `status_detail`, `needs_action` |
| `thinking_tokens`   | Thinking-budget accounting                                                                     |
| `compact_boundary`  | Where the context was compacted                                                                |

`task_type` distinguishes `local_agent` (a subagent) from `local_bash` (a backgrounded shell).
The `task_*` and `post_turn_summary` frames are the CLI's own answer to "what is this session
doing right now", which is why they matter to Haku's room status line rather than only to a
terminal UI.

**`system/init` arrives with the first turn, not with `initialize`.** A session that completes the
handshake and sends no prompt never sees one; the handshake's own answer is the `initialize`
control response.

## Control channel

```json
{ "type": "control_request", "request_id": "req_1", "request": { "subtype": "initialize" } }
{ "type": "control_response", "response": { "subtype": "success", "request_id": "req_1", "response": {} } }
{ "type": "control_response", "response": { "subtype": "error", "request_id": "req_1", "error": "…" } }
```

**The correlation key is nested inside `response`**, not beside it at the top level. A responder
must echo the id from the request it is answering.

Either side may open a request, and `control_cancel_request` withdraws one. A request the peer
never answers is waited on **indefinitely**: measured with `probes/hooks.py`-style silence against
a `can_use_tool`, the turn produced no `result` in 60s. So a client must answer every inbound
request, including ones it does not implement — an error response is an answer, silence is not.

### Sent by the client

`initialize`, `interrupt`, `set_permission_mode`, `set_model`, `set_max_thinking_tokens`,
`set_cwd`, `mcp_set_servers`, `mcp_reconnect`, `mcp_status`, `mcp_toggle`, `reload_plugins`,
`reload_skills`, `rewind_files`, `stop_task`, `rename_session`, and a family of `get_*` reads
(`get_context_usage`, `get_plan`, `get_session_cost`, `get_settings`, `get_usage`,
`get_workspace_diff`, `get_binary_version`).

`interrupt` takes two optional fields:

- `reason` — forwarded to the turn's `AbortSignal.reason` so tools can distinguish a user cancel
  from other aborts. Known values `interrupt`, `user-cancel`, `remote-cancel`, `consumer-error`,
  `workflow-abort`, `stalled`, `recovery-timeout`; an open set.
- `cancel_queued` — see [Prompt lifecycle](#prompt-lifecycle).

### Sent by the CLI

| `subtype`                 | Asks the client to                                       |
| ------------------------- | -------------------------------------------------------- |
| `can_use_tool`            | Decide a permission prompt                               |
| `hook_callback`           | Run a registered hook and return its output              |
| `mcp_message`             | Serve one JSON-RPC message to a client-hosted MCP server |
| `request_user_dialog`     | Render a blocking dialog and return the choice           |
| `elicitation`             | Collect structured input an MCP server asked for         |
| `oauth_token_refresh`     | Supply a fresh access token                              |
| `host_auth_token_refresh` | Supply a fresh host auth token                           |

## `initialize`

The handshake. Sent once, before the first prompt; its response is the session's command and
agent catalog, the account, and the model list.

Everything is optional, so the minimal handshake is `{"subtype": "initialize"}`. Fields:

| Field                                | Effect                                                                                                 |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------ |
| `hooks`                              | Event name → matchers carrying `hookCallbackIds`; see [Hooks](#hooks)                                  |
| `sdkMcpServers`                      | Names of MCP servers the **client** hosts; see [Client-hosted MCP servers](#client-hosted-mcp-servers) |
| `jsonSchema`                         | A JSON Schema the final answer must match; see [Structured output](#structured-output)                 |
| `systemPrompt`                       | Array of strings replacing the system prompt                                                           |
| `appendSystemPrompt`                 | String appended to it                                                                                  |
| `agents`                             | Name → subagent definition (`description`, `prompt`, `tools`, `model`)                                 |
| `skills`                             | Allowlist of skills loaded into the main session's system prompt, by canonical name or `:name` suffix  |
| `title`                              | Session title, suppressing automatic title generation                                                  |
| `toolAliases`                        | Alias → real tool name, resolved before name resolution. Single-hop, no chains                         |
| `planModeInstructions`               | Replaces plan mode's workflow body, keeping its read-only preamble and `ExitPlanMode` footer           |
| `excludeDynamicSections`             | Moves cwd and memory-path context out of the cached system prompt into a first user message            |
| `supportedDialogKinds`               | `request_user_dialog` kinds the client can render; see below                                           |
| `agentProgressSummaries`             | Requests subagent progress summaries                                                                   |
| `forwardSubagentText`                | Forwards subagent **prose**, not just its tool calls                                                   |
| `promptSuggestions`                  | Requests prompt suggestions                                                                            |
| `appendSubagentSystemPrompt`         | Appended to every subagent; gated on `CLAUDE_CODE_ENABLE_APPEND_SUBAGENT_PROMPT`                       |
| `webSearchIsolationExemptMcpServers` | Extra servers exempt from the web-search isolation latch                                               |

The response carries `commands`, `agents`, `output_style`, `available_output_styles`, `models`,
`account`, and `pid`, plus fast-mode and remote-control state. Several documented fields
(`unavailable_models`, `current_model`, `current_permission_mode`) are populated only for
allowlisted first-party hosts and are absent here.

**There is no client-capability field.** The `capabilities` array on `system/init`
(`interrupt_receipt_v1`, `interrupt_cancel_queued_v1`, `msg_lifecycle_v1`) is the CLI stating what
it supports; nothing is negotiated, and each feature is reached by using it.

`supportedDialogKinds` fails closed: a kind not listed makes the CLI degrade the dialog-gated flow
to its no-dialog behaviour rather than park a dialog the client might mishandle. For
`refusal_fallback_prompt`, the only kind currently defined, that degraded behaviour is the classic
refusal error. The first client to attach fixes the set for the session.

### Validation is partial

`initialize` neither validates the whole request nor rejects unknown fields. `skills` and `hooks`
get typed errors (`initialize: skills must be an array of strings`); `agents: 42` and an entirely
invented field are both answered `success` and ignored. A misspelled field name is therefore
silent, and the only way to know a field took effect is to observe its effect.

## Prompt lifecycle

A prompt is a conversation frame, not a control request:

```json
{
  "type": "user",
  "message": { "role": "user", "content": "…" },
  "parent_tool_use_id": null,
  "uuid": "0f0e…"
}
```

**The `uuid` is what turns lifecycle reporting on.** With one, the CLI emits `command_lifecycle`
frames — `queued` → `started` → `completed` | `cancelled` — naming that prompt in `command_uuid`.
Without one the prompt still runs and is still queued; it is merely unobservable, and it cannot be
cancelled by `cancel_queued`, which only reaches uuid-stamped commands.

A prompt written while a turn is running is not rejected. It is **absorbed at the next tool
boundary** and the model acts on it within that turn:

```text
first  queued -> started
second                     queued -> started -> completed
first                                                       completed     # one result frame
```

So one `result` can answer two prompts, and the lifecycle is what tells that apart from a prompt
that merely waited: a folded prompt reports `completed` before the turn's `result`, a fresh turn
reports it after. A turn generating continuous prose has no boundary to absorb at, and there the
prompt waits for the turn to end.

**`interrupt` alone does not empty the queue.** Measured: with a prompt running and a second one
queued, a bare `interrupt` cancels the running one (`result` with subtype
`error_during_execution`) and the CLI immediately **starts the queued one**. Adding
`cancel_queued: true` cancels both — the queued prompt goes straight from `queued` to `cancelled`
and never starts. An abort that means "stop, and drop what I asked for next" must set it.

## Structured output

`jsonSchema` takes a **bare JSON Schema object**:

```json
{ "subtype": "initialize", "jsonSchema": { "type": "object", "properties": { … }, "required": [ … ] } }
```

The CLI turns it into a `StructuredOutput` tool the model must call. The `result` frame then
carries the validated object in `structured_output`, and `result` holds the same thing as JSON
text.

**Gotcha:** the `{"type": "json_schema", "schema": {…}}` wrapper — the shape
`ClaudeAgentOptions`' output-format setting takes — is accepted, silently ignored, and yields a
prose answer with `structured_output: null`. There is no error, and the tool is never registered.
A schema the CLI cannot convert is also non-fatal: it logs and disables structured output.

## Hooks

`hooks` maps a hook event name to matchers, each naming callback ids the client will be asked to
run:

```json
{ "PreToolUse": [{ "matcher": "Bash", "hookCallbackIds": ["cb_pretool"] }] }
```

The CLI then sends `hook_callback` with that `callback_id`, the hook's `input`, and the
`tool_use_id` where one applies. The input is the ordinary hook payload — `session_id`,
`transcript_path`, `cwd`, `prompt_id`, `permission_mode`, `hook_event_name`, plus the
event-specific fields (`prompt` for `UserPromptSubmit`; `tool_name` and `tool_input` for
`PreToolUse`). The response is the hook's output, and it is honoured:

```json
{
  "decision": "block",
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "…"
  }
}
```

produces a `tool_result` with `is_error: true`, the reason as its content, and
`tool_result_meta[].non_execution_kind: "permission-rule"`. A `PreToolUse` hook runs **before**
the permission check, so denying there means no `can_use_tool` is ever asked.

`SessionStart` does not fire for a session the client initializes this way.

## Permission prompts

Permission decisions reach the client only when the CLI is launched with
`--permission-prompt-tool stdio`. Without it a tool needing approval is refused outright — the
model gets "Claude requested permissions to use X, but you haven't granted it yet" — with no
control request sent anywhere.

With it, the CLI sends `can_use_tool` carrying `tool_name`, `display_name`, `input`,
`tool_use_id`, and `permission_suggestions` (ready-made `addRules` entries a host can offer as
"always allow"). The answer is `{"behavior": "allow", "updatedInput": {…}}` or a `deny`, and
`updatedInput` is what actually runs, so a host may rewrite the call.

`--dangerously-skip-permissions` is the blunt alternative, but the CLI refuses it under
root/sudo — which in a container reads as a handshake that never completes rather than as a flag
error.

## Client-hosted MCP servers

`sdkMcpServers` is a list of **names**. Declaring one tells the CLI to speak MCP to the client
over the control channel instead of connecting anywhere: every JSON-RPC message arrives as
`mcp_message` with `server_name` and `message`, and the reply is
`{"mcp_response": <json-rpc message>}`.

The client is the server. It answers `initialize`, `notifications/initialized`, `tools/list` and
`tools/call` itself. Notifications carry no `id` and still arrive as control requests that need a
response — an empty JSON-RPC result is the convention. A `tools/call` params object carries
`_meta` with `claudecode/toolUseId` and a `progressToken`.

Tools are exposed to the model as `mcp__<server_name>__<tool_name>` and are otherwise ordinary:
they go through the permission path above, and in a session with enough tools they land in the
deferred pool behind `ToolSearch` like any other.

This is the route by which a host adds tools with no second process, no port, and no credential
on the wire — the tool implementation stays in the host, which already holds whatever the tool
needs.

## Subagents

`agents` defines subagents by name; the model reaches one through the `Task` tool with
`subagent_type` set to that name, and the definition's `model` is honoured (a `haiku` subagent
reports `claude-haiku-4-5` on its nested frames).

A subagent's `tool_use` and `tool_result` frames are forwarded to the client by default, tagged
with `parent_tool_use_id`. **Its prose is not.** `forwardSubagentText: true` is what adds the
nested `assistant` frames carrying `text` blocks; without it the subagent's own report reaches the
main thread but never the client, and the only prose the client sees is the parent's summary of
it.

`agentProgressSummaries` produced no observable difference on this transport in the probed shape —
the `task_progress` frames arrive either way — so it is presumably for host UIs that render their
own summary.

## What the Agent SDK's typed layer does with all this

The SDK's `Message` union has variants for `user`, `assistant`, `system`, `result`,
`stream_event` and `rate_limit_event`. `system` keeps its raw payload in a generic
`SystemMessage.data`, so subtypes survive; **top-level types with no variant do not**.
`command_lifecycle` and `active_goal` hit the parser's forward-compatible default case and are
dropped with a debug log.

That is the practical reason the console keeps raw frames as its record and parses separately:
the typed layer is lossy in exactly the places the newer protocol features live.
