# Tools the driver provides

Both harnesses let the process driving the JSON protocol expose its own tools to the model. This
page records how a call round-trips on each side, whether the tool set can change while a session
runs, and what each harness does with an MCP server's `notifications/tools/list_changed`.

Evidence: the pinned Claude Code 2.1.252 and Codex app-server 0.152.0 binaries, driven by a
throwaway rig against a loopback model endpoint — every claim marked **confirmed** was produced by
running the real binary that way. Claims marked **read** come from Codex's Rust
(`codex-rs/`, `rust-v0.152.0`), from the `@anthropic-ai/claude-agent-sdk` type declarations, which
document the control protocol Claude Code speaks, or from the debundled `cli.js` chunks
([gaffer-private `claude/re`](https://github.com/agentydragon/gaffer-private/tree/devel/claude/re)).
Background work is a separate page: [background_work.md](background_work.md).

## Claude Code: the driver hosts an MCP server

The driver names servers it hosts in the `initialize` control request; the CLI then acts as an MCP
client against them over the same stdio stream.

```json
{ "subtype": "initialize", "sdkMcpServers": ["driver"], "sdkMcpServerConfigs": { "driver": { "timeout": 3000 } } }
```

`sdkMcpServerConfigs[name].timeout` is a per-server tool-call wall clock in milliseconds; values
under 1000 fall through to `MCP_TOOL_TIMEOUT` (read), which is effectively unbounded by default.

**Each MCP message is one control request.** The CLI sends
`control_request {subtype: "mcp_message", server_name, message}` where `message` is a JSON-RPC 2.0
object, and the driver answers
`control_response {subtype: "success", response: {mcp_response: <JSON-RPC reply>}}`. **Gotcha:** a
JSON-RPC _notification_ also needs an `mcp_response` — `{"jsonrpc": "2.0", "result": {}, "id": 0}`.
Acknowledging `notifications/initialized` with a bare success instead leaves the handshake
unanswered; the CLI times the `initialize` out, retries, and settles the server at
`status: "failed"` with no tools. Confirmed.

Observed startup, once per connection: `initialize` (`protocolVersion` `2025-11-25`, `clientInfo`
`claude-code`/`2.1.252`) → `notifications/initialized` → `tools/list`. Confirmed.

**A call.** The CLI sends `tools/call` with the bare tool name, the arguments, and
`_meta: {"claudecode/toolUseId": "<tool_use id>", "progressToken": <n>}`. The model sees the tool
as `mcp__<server>__<tool>` with the declared `inputSchema` passed through verbatim as
`input_schema`; the reply's `content` blocks become the `tool_result` content. Confirmed.

**Failures all reach the model, and all let the turn continue.** Confirmed for each:

| Driver behavior                       | What the model gets                                                    |
| ------------------------------------- | ---------------------------------------------------------------------- |
| `control_response` `subtype: "error"` | `is_error: true`, the error string                                     |
| MCP result with `isError: true`       | `is_error: true`, the result text                                      |
| no answer                             | `is_error: true`, `MCP server "driver" tool "ping" timed out after 3s` |

The timeout fired 3.1 s after the call for `timeout: 3000`.

### Changing the tool set mid-session

`mcp_message` flows in both directions — the driver may send one to deliver its server's own
messages, and the CLI acknowledges with an empty success. That is not enough to change the tool
list:

- **`notifications/tools/list_changed` pushed by a driver-hosted server is inert.** The CLI
  acknowledges it and re-lists nothing; the next model request carries the old names and the old
  schemas. Confirmed. The cause is visible in the bundle: the loops that attach elicitation and
  list-changed listeners iterate the CLI's own MCP client list, which the driver-hosted clients are
  not part of, and the elicitation loop skips `config.type === "sdk"` outright (read).
- **`mcp_reconnect` refuses**, `subtype: "error"`, `"SDK servers should be handled in print.ts"`.
  Confirmed.
- **`mcp_set_servers` re-declaring the same name is a no-op**: success with
  `{"added": [], "removed": [], "errors": {}}`, no rebuild. Confirmed.
- **`mcp_set_servers` with `{}`, then again with the server, works.** The two calls report
  `removed: ["driver"]` and `added: ["driver"]`; the CLI re-runs `initialize` /
  `notifications/initialized` / `tools/list`, and the next model request carries the added tool and
  the widened `input_schema`. Confirmed. This is the driver's only working update path.

`mcp_status` reports a driver-hosted server with `scope: "dynamic"` and an empty `tools` array.

### `notifications/tools/list_changed` from a real MCP server

Honoured, end to end. A stdio server configured with `--mcp-config`, advertising
`capabilities.tools.listChanged`, that swapped one tool for another and widened a second tool's
schema: the CLI re-issued `tools/list` and the model's next request carried the new set and the new
schema. Confirmed with a scripted server that flips its list on a tool call.

**The boundary is the next model request, not the next turn.** With the notification sent while the
originating `tools/call` was still outstanding, the CLI issued `tools/list` immediately and the
follow-up request _inside the same turn_ already carried the new set. Confirmed. Nothing rewrites
the transcript: earlier `tool_use` blocks for a tool that no longer exists stay as they are, and the
tool is simply no longer offered.

Refresh failure keeps the previous tools and warns rather than emptying the list, and a listen-stream
reopen synthesizes a refetch in case a notification was missed while the stream was down (read).

**Gotcha:** `--safe-mode` disables MCP servers configured on disk entirely — they never connect, so
none of this is reachable — while leaving driver-hosted servers working. The scripted tests use
`--safe-mode`, so an MCP scenario has to drop it and rely on `--setting-sources=` alone. Confirmed.

## Codex: `dynamicTools` on `thread/start`

`thread/start.dynamicTools` is gated behind `initialize.capabilities.experimentalApi: true`.
Without it `thread/start` fails, `-32600`, `thread/start.dynamicTools requires experimentalApi capability`.
Confirmed.

A spec is either `{"type": "function", name, description, inputSchema, deferLoading?}` or
`{"type": "namespace", name, description, tools: [<function>...]}`. A legacy flat shape carrying
`namespace` and `exposeToContext` is normalized into that; mixing the two forms in one array is
rejected (read). Validation runs at `thread/start` and rejects the whole request: empty or
whitespace-padded names, over-length names, duplicates, a deferred tool outside a namespace, an
unparseable schema, and anything named `mcp` or prefixed `mcp__`. Confirmed for the reserved name —
`-32600 dynamic tool name is reserved: mcp__evil`.

Declared tools reach the model as plain `function` entries in the Responses request alongside the
built-ins, `strict: false`, `parameters` the given schema, **under the declared name** — Codex adds
no prefix. Confirmed.

**A call** arrives as a JSON-RPC request from server to client:

```json
{
  "method": "item/tool/call",
  "id": 0,
  "params": {
    "threadId": "…",
    "turnId": "…",
    "callId": "call-1",
    "namespace": null,
    "tool": "lookup",
    "arguments": { "note": "hi" }
  }
}
```

alongside an `item/started` notification carrying a `dynamicToolCall` item. The driver replies with
a JSON-RPC result `{"contentItems": [{"type": "inputText", "text": "…"}], "success": true}`;
`inputImage` and `inputAudio` items are also accepted, and must be `data:` URLs (read). The text
becomes the `function_call_output` for that `callId` in the next request. Confirmed.

`success` only sets the item's status to `failed` on `item/completed`, which the driver sees. The
model's `function_call_output` carries no error marker either way. Read, and consistent with the
confirmed error case below.

**Failures.** A JSON-RPC error reply is swallowed: the app-server substitutes
`{"contentItems": [{"type": "inputText", "text": "dynamic tool request failed"}], "success": false}`
and the turn continues, so the model reads `dynamic tool request failed` and nothing else.
Confirmed. The same fallback covers an undeserializable response and a rejected media URL, each with
its own message (read).

**There is no timeout.** The handler awaits the driver's reply on a bare oneshot with no deadline
(read); with the driver silent for 25 s the turn made no progress at all. Confirmed. An unanswered
`item/tool/call` stalls the turn until the driver answers or the turn transitions.

`turn/start.toolOutput` is a different path: it supplies a tool result as the _input_ that starts a
turn, rather than answering an `item/tool/call`.

### The tool set cannot change while a thread runs

`dynamicTools` exists only on `thread/start`. `turn/start` has no such field and a `dynamicTools`
key passed there is silently ignored — the request after that turn still carried the original
description and schema and never gained the added tool. Confirmed. `thread/resume` has no
`dynamicTools` field either, and a resumed thread reuses the specs recorded in its history (read).
Changing a driver-provided tool or its schema means starting a new thread.

### `notifications/tools/list_changed` from a real MCP server

Ignored. Codex's MCP client handler logs `MCP server tool list changed` at info level and does
nothing else (`codex-rs/rmcp-client/src/logging_client_handler.rs`); tools are fetched once at
server startup and cached for the life of the connection (`ManagedClient::tools`). What does
refresh the catalog is driver- or config-initiated: the `config/mcpServer/reload` method, plugin,
marketplace and account changes that invalidate the MCP runtimes, and a hard refresh of the Codex
Apps server. **Read only** — not exercised against the binary.

So a Codex session stays connected to a server whose tools have changed underneath it and keeps
offering the old ones.

## What this means for the runner protocol

The call round trip is the same shape on both sides and could be one abstraction: declare a name
and a JSON schema, receive a call carrying an id and arguments, return content plus a success flag,
and the harness puts the result in the transcript as that tool's output. Three things do not line
up, and the first forces a choice rather than an adapter detail.

**Mutability is the one that picks a side.** Claude's driver-hosted tool set is per-connection and
rebuildable while a session runs, taking effect at the next model request. Codex's is fixed for the
life of the thread. A common "replace the tool set" verb is not implementable on Codex without
ending the thread, and a session maps to a Thread above the seam, so this surfaces as a product
decision: either the runner offers the verb and declares it unsupported on Codex, or the runner's
contract makes the driver-provided tool set immutable per session and a driver that needs a change
opens a new one.

**Failure semantics do not survive a common promise.** Claude enforces a per-server wall clock and
reports every failure to the model as `is_error: true` with a readable message. Codex has no
deadline — a driver that never answers wedges the turn — and collapses every failure into one fixed
string with no error marker. The runner can impose its own deadline and require the driver to
answer, but it cannot promise the model sees an error flag on both.

**Naming differs.** Claude renames a driver tool to `mcp__<server>__<tool>`; Codex uses the declared
name and reserves that prefix. Either the runner owns the name the model sees, or the name is
provider-visible.

On `notifications/tools/list_changed` the two diverge: Claude re-lists and uses the new set, Codex
ignores it. So a session that keeps offering tools its MCP server has withdrawn is a live failure
mode on Codex and not on Claude. For driver-hosted tools neither harness follows the notification,
so a driver that wants its own tool list to change has to say so through the control protocol:
`mcp_set_servers` remove-then-add on Claude, and a new thread on Codex.
