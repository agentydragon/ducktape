# UI State Reducer — Current State & Open Items

Motivation

- Eliminate split of display logic between server and client and the confusion around multiple “transcripts”
- Make UI rendering deterministic, reloadable, and versioned
- Centralize transformation from agent/tool events → display items in one place (server-side)

Goals (acceptance criteria)

- Server maintains a single authoritative UiState per agent/session
- UiState is a typed, display‑oriented model; client renders only UiState (no own grouping)
- Ordering: ToolCall → FunctionCallOutput → grouped UI items emitted deterministically
- Snapshot reload: full UiState restored on reload with identical visual content
- End‑turn controlled via bus and reflected in UiState (no parsing of names)
- Protocol versioning for UiState messages (future‑proof)

Non‑Goals (for this refactor)

- Streaming deltas (we can start with whole‑state updates)
- Multi‑session persistence beyond a single server process lifetime

Design overview (implemented)

- Server‑side reducer
  - A pure reducer `reduce_ui_state(prev: UiState, evt: UiEvent) -> UiState` consumes typed events and returns a new state
  - One source of truth lives on `AgentSession`: `session.ui_state` (not "transcript")
- Display items (normalized)
  - `UserMessage {id, ts, text}`
  - `AssistantMarkdown {id, ts, md}`
  - `ToolGroup` - `{id, ts, tool, call_id, cmd?, approvals, stdout, stderr, exit_code}`
- Event inputs for reducer
  - `UserText` (on `on_user_text_event`)
  - `ToolCall` (on `on_tool_call_event`)
  - `FunctionCallOutput` (on `on_tool_result_event`)
  - `ApprovalDecision` (on `approval_decision`)
  - `UiMessage` (from `ui.send_message`)
  - `EndTurn` (from `ui.end_turn`)
- MCP UI server & bus
  - `ui.send_message(UiMessage)` pushes `UiMessage` to per‑agent `UiBus`
  - `ui.end_turn()` pushes `UiEndTurn`; `UiAutoHandler` consumes via bus and `Abort()`s turn
  - Server drains `UiBus` items after tool outputs and before snapshot to generate `UiMessage` events into `UiState`
- Protocol
  - `UiStateSnapshot { type: "ui_state_snapshot", v: "ui_state_v1", seq, state }`
  - `UiStateUpdated { type: "ui_state_updated", v: "ui_state_v1", seq, state }` (initially whole state; optional future deltas)
  - Deprecate `snapshot.transcript` over time (keep for migration)

Ordering & snapshot

- After each `FunctionCallOutput`, reduce with any `UiMessage` items drained from `UiBus` → emit `UiStateUpdated`
- On hello/resume: drain `UiBus`, then send `UiStateSnapshot` of current `UiState`

Handler & loop control

- `UiAutoHandler(bus)`:
  - `on_before_sample`: if `bus.consume_end_turn()` → `Abort()`; else `Continue(RequireAnyTool())` to force tool usage
  - No per‑tool interception; approvals are enforced by Policy Gateway middleware
  - Reducer is applied by the UI server `ConnectionManager`/`AgentSession` on typed events and bus drains

Open items only (migration largely complete)

1. Cleanup: remove any remaining legacy transcript references in comments and protocol helpers; keep WS‑only path.
2. Persistence: consider durable UiState snapshots (optional) to speed very large histories.

Testing (remaining)

- Add targeted reducer tests for rare UI paths (multi‑tool groups with interleaved UI messages; large batched updates).

Decisions (resolved)

- `DisplayItem` schema: `UserMessage`, `AssistantMarkdown`, `ToolGroup` (no `UiNotice` for now)
- Approvals in `ToolGroup`: store full `ApprovalDecision` kind (`approve | deny_continue | deny_abort`)
- `UiStateUpdated` payload: send full state (v1)
- Persistence: in-memory only for now; durable persistence later (open)
- Assistant messages: only via `ui.send_message`; `assistant_text` path in UI mode MUST raise/crash with explanatory comment and pointers to the new path
- Protocol naming: `ui_state_snapshot`/`ui_state_updated` with version `ui_state_v1`
- Client: do not keep legacy transcript rendering; remove it

Risks & mitigations

- Drift between event stream and `UiState`: mitigated by single reducer and strictly ordered application
- Client/server mismatch during migration: version messages and feature flag on client

Execution checklist (open only)

- [ ] Cleanup: remove any lingering transcript helpers; update docs accordingly
- [ ] Optional: durable `UiState` persistence (see Decisions)

Appendix: example `UiState` (v1)

```json
{
  "seq": 5,
  "items": [
    { "kind": "UserMessage", "ts": "...", "text": "run ls -la" },
    {
      "kind": "ToolGroup",
      "ts": "...",
      "tool": "seatbelt_sandbox_exec",
      "call_id": "abc",
      "cmd": "ls -la",
      "approvals": ["approve"],
      "stdout": "...",
      "stderr": "",
      "exit_code": 0
    },
    { "kind": "AssistantMarkdown", "ts": "...", "md": "Here are the results..." }
  ]
}
```
