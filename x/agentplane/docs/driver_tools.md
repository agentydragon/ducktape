# Tools the driver provides

Both harnesses let the process driving the JSON protocol expose its own tools to the model. This
page records how a call round-trips on each side, how much of the registry each turn actually shows
the model, whether the registry itself can change while a session runs, and what each harness does
with an MCP server's `notifications/tools/list_changed`.

Evidence: the pinned Claude Code 2.1.252 and Codex app-server 0.152.0 binaries, driven by a
throwaway rig against a loopback model endpoint — every claim marked **confirmed** was produced by
running the real binary that way. Claims marked **read** name their source: Codex's Rust at the
tag `rust-v0.152.0` (the pinned release, not `main`, which has moved on), the
`@anthropic-ai/claude-agent-sdk` type declarations, which document the control protocol Claude Code
speaks, or the debundled `cli.js` chunks
([gaffer-private `claude/re`](https://github.com/agentydragon/gaffer-private/tree/devel/claude/re)).
Background work is a separate page: [background_work.md](background_work.md).

Two axes run through this page and are easy to conflate. **What the model sees on a turn** is
dynamic on both harnesses, driven by the model itself through a tool-search tool. **What the
registry contains** — which tools exist at all, and with what schema — is a separate question with a
different answer on each side.

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

The current Agentplane runner does not yet implement this host role: its initialize payload cannot
declare `sdkMcpServers`/configs and its adapter rejects `mcp_message` as an unknown control request.
The active v2 `tools/call` path in 2.1.252 also does not wire the task/input-request helper
scaffolding present elsewhere in the MCP runtime, so driver-hosted MCP task semantics must remain
unsupported. See [claude_runtime_contracts.md](claude_runtime_contracts.md).

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

### Deferral and `ToolSearch`

The CLI does not always put the driver's tools in the request. With tool search on, it strips them
out and offers the model a `ToolSearch` tool that fetches a deferred tool's schema on demand. **This
is client-side**: the CLI builds a different request body, so the difference is visible in the bytes
it sends, with nothing server-side deciding it. Confirmed by an A/B on the same twelve driver-hosted
tools:

| `ENABLE_TOOL_SEARCH` | `tools` in the request                                                                     |
| -------------------- | ------------------------------------------------------------------------------------------ |
| unset                | all twelve `mcp__driver__*` tools, in full, no `defer_loading` anywhere                    |
| `tst`                | none of them; a `ToolSearch` tool and a `DeferredToolPlaceholder` carrying `defer_loading` |

`ToolSearch` describes itself as fetching "full schema definitions for deferred tools so they can be
called", takes `{query, max_results}`, accepts `select:<name>` for an exact fetch, and returns the
matched definitions inline. Deferred tools reach the model as _names only_, in `<system-reminder>`
messages, per that description.

The mode is chosen by the CLI from `ENABLE_TOOL_SEARCH` (`auto:N` selects the auto mode with an N%
context threshold; `auto:0` forces it on, `auto:100` off) and gated on the model supporting
`tool_reference` blocks — Sonnet 4+, Opus 4+, Haiku 4.5+ per the CLI's own diagnostic (read, from
the bundle).

Per-server and per-tool opt-out is `alwaysLoad`, which the CLI applies as
`_meta['anthropic/alwaysLoad']` on the MCP tool and which the API reads as `defer_loading: false`;
the documented default is that tools are deferred whenever tool search is enabled (read, from the
SDK type declarations).

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

"Dynamic" here means **client-supplied**: definitions the driver hands the app-server at
`thread/start`, as against the built-in tools compiled into Codex. It does not mean the set mutates.
Each tool separately chooses whether the model sees it up front or has to find it, which is the part
that is dynamic in the model's view — the two sections below answer those two different questions.

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

### Deferral and `tool_search`

Per-tool `deferLoading: true` keeps a tool registered and callable while excluding it from the
model-facing list sent on ordinary turns; the model finds it with `tool_search`. A deferred tool
must sit in a namespace, which `validate_dynamic_tool` enforces
(`codex-rs/app-server/src/request_processors/thread_processor.rs`, read at `rust-v0.152.0`). The
whole path is confirmed:

1. **Enablement.** `tool_search` is offered only when the model's catalog entry sets
   `supports_search_tool`, the provider advertises the `namespace_tools` capability (default true),
   and at least one registered tool is deferred (`core/src/tools/spec_plan.rs`, read). Confirmed by
   an A/B on otherwise identical setups: with `supports_search_tool: false` the request carried only
   the built-ins; with `true` it also carried a
   `{"type": "tool_search", "execution": "client"}` entry taking `{query, limit}`, whose description
   names "Dynamic tools: Tools provided by the current Codex thread".
2. **Search.** The model invokes it with a `tool_search_call` item — a special model tool, not a
   `function_call`. The app-server answers with a `tool_search_output` item carrying the matching
   **full definitions**, namespace and all, each still marked `defer_loading: true`. Confirmed.
3. **Exposure runs through the conversation, not the tool list.** The request's `tools` array is
   unchanged after the search; the definition reaches the model as an input item. Confirmed.
4. **Call.** A `function_call` with `namespace` and `name` set routes to the driver as an ordinary
   `item/tool/call` with `namespace` populated, and the driver's reply becomes the
   `function_call_output`. Confirmed.

A `deferLoading: true` namespace being absent from the ordinary tool list is therefore the design,
not a gap.

### Where the registry is frozen

The Responses API rebuilds the `tools` array on every request, and Codex uses that — deferral and
code mode both vary what a given turn carries. What a client cannot do is change **which tools
exist** or **a tool's input schema** once the thread exists.

Traced at `rust-v0.152.0`. Each turn builds a fresh `ToolRegistry`, and
`append_dynamic_tool_runtimes(&turn_context.dynamic_tools, …)` (`core/src/tools/spec_plan.rs`) feeds
the driver's tools into it. `TurnContext.dynamic_tools` is a verbatim clone of
`SessionConfiguration.dynamic_tools` (`core/src/session/turn_context.rs:812`), which is populated
once in `Session::new` from `StartThreadOptions.dynamic_tools` and afterwards only ever cloned
forward — into a derived turn context (`turn_context.rs:541`), into a review turn
(`session/review.rs:173`), and into the thread record for persistence (`session/session.rs:853`).
**`SessionConfiguration.dynamic_tools` is the freeze point.** No later value reaches it: the only
dynamic-tool `Op` in the protocol is `Op::DynamicToolResponse`, which carries a call _result_, and
no v2 method — `turn/start`, `turn/settings/update`, `thread/settings/update`, `thread/resume`,
`thread/fork` — has a `dynamicTools` field.

A schema change is therefore not expressible at all after `thread/start`. Confirmed on the wire: a
`dynamicTools` key on `turn/start` is dropped, and the next request still carried the original
description and schema and never gained the added tool.

The only recourse is a new `thread/start`, which costs a new thread id and the transcript — the
history does not come with it. `thread/fork` does not help: it copies history but reads its dynamic
tools from the forked thread's own `SessionMeta` (`codex-rs/history/src/lib.rs`), so a fork inherits
exactly the specs the client is trying to change.

### A resume keeps the driver's tools

The specs survive a full process restart. Confirmed end to end: a durable thread started with a
`lookup` tool, a turn that called it, the app-server killed, a fresh process started on the same
`CODEX_HOME`, `thread/resume` — and the next request carried `lookup` with its schema, the
transcript intact, and a fresh call to it arriving at the new driver process as an `item/tool/call`
whose result reached the model.

This is worth stating because the signatures suggest the opposite: `resume_thread_from_rollout` and
`resume_thread_with_history` take no `dynamic_tools` parameter and `StartThreadOptions` defaults the
field to an empty vector, which reads like the tools are silently dropped. The empty vector is
exactly the trigger — `Session::new` falls back to `conversation_history.get_dynamic_tools()`, which
reads the specs back out of the rollout's `SessionMeta` line, under the comment "Dynamic tools are
defined at thread start and persisted in rollout session metadata" (`core/src/session/mod.rs`, read
at `rust-v0.152.0`). Two edges follow from that lookup: an **ephemeral** thread writes no rollout
and cannot be resumed at all, and a **cleared** history returns `None`, so the specs are gone.

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

The call round trip is the same shape on both sides and could be one abstraction: declare a name and
a JSON schema, receive a call carrying an id and arguments, return content plus a success flag, and
the harness puts the result in the transcript as that tool's output. Judge the rest on the two axes
separately, because they answer differently.

**What the model sees on a turn: already dynamic on both, and not the runner's to own.** Each
harness withholds a driver-provided tool's schema and lets the model pull it in when it becomes
relevant — Claude with `ToolSearch` and `<system-reminder>` name announcements, Codex with
`tool_search` and a `tool_search_output` item. Both are client-side, both are driven by the model
rather than the driver, and in both the driver's only lever is a per-tool flag: Claude's `alwaysLoad`
(default: deferred when tool search is on) and Codex's `deferLoading` (default: eager). The runner
should carry that one flag on the declaration and let the harness do the rest. It should not model
"the tool list changed", because on both sides nothing changed — the registry was constant and the
model asked for more of it.

**What the registry contains: mutable on Claude, frozen per thread on Codex.** Claude rebuilds it
mid-session (`mcp_set_servers` remove-then-re-add) and re-lists a configured MCP server on
`tools/list_changed`, both taking effect at the next model request. Codex freezes it at
`thread/start`; no method or `Op` carries a new spec afterwards, and a fork inherits the old one.

That second axis is a narrower constraint than it looks, because deferral absorbs most of what a
driver would want mutation for. A driver that declares its superset at `thread/start` and defers the
rarely-used part gets tools appearing to the model on demand without the registry changing at all.
Mutation is only genuinely needed when a tool the driver could not have declared at session start
appears, or when an existing tool's **schema** changes — and that second case is the one with no
Codex answer at all, since a schema is fixed for the life of the thread. So the runner has a real
choice to make, but it is about a narrow case rather than a protocol fork: either the contract makes
the declared set immutable per session and a driver that must re-describe a tool opens a new session,
or the runner offers a replace verb and names it unsupported on Codex. Either way it is worth
recording that a Codex client's fallback costs the transcript, since `thread/fork` does not carry a
new declaration.

Two smaller things a common contract cannot promise identically:

- **Failure semantics.** Claude enforces a per-server wall clock and reports every failure to the
  model as `is_error: true` with a readable message. Codex has no deadline — a driver that never
  answers wedges the turn — and collapses every failure into one fixed string with no error marker.
  The runner can impose its own deadline and require the driver to answer; it cannot promise the
  model sees an error flag on both.
- **Naming.** Claude renames a driver tool to `mcp__<server>__<tool>` and the model calls it under
  that name. Codex keeps the declared name and carries the namespace as a separate field on the
  call, reserving the `mcp` prefix. Either the runner owns the name the model sees, or the name is
  provider-visible.

On `notifications/tools/list_changed` the two diverge: Claude re-lists and uses the new set, Codex
ignores it. So a session that keeps offering tools its MCP server has withdrawn is a live failure
mode on Codex and not on Claude. For driver-hosted tools neither harness follows the notification, so
a driver that wants its own registry to change has to say so through the control protocol:
`mcp_set_servers` remove-then-add on Claude, and a new thread on Codex.

Resume is not a hazard on either side: Codex rehydrates the declared specs from the thread's rollout,
so a runner that restarts a process and sends `thread/resume` keeps the driver's tools.
