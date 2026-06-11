# ui chat server

This document describes the chat MCP servers that handle human↔agent messaging. The legacy `ui` server (with `send_message` / `end_turn`) is being retired; forward-looking design routes all chat through the dedicated chat servers plus `loop.yield_turn` for end-of-turn signalling. See also <overview.md> and <../vision.md> for how chat fits into the broader runtime.

## Architecture snapshot (sidecar model)

- Human chat traffic lives in its own MCP server ("chat human"), mounted **outside** the Compositor. The frontend (or an HTTP bridge) calls this server directly to post user messages.
- The runtime keeps a dedicated FastMCP client to that sidecar. It pins a subscription to the inbox resource and, on each `resources/updated`, reads the payload and raises a `UserText` event. That event flows through `reduce_ui_state`, so `UiState` (and the WebSocket timeline) is the single source of truth for chat history.
- Assistant responses use the chat sidecar as well (e.g., `chat.assistant.post`). There is no plain assistant text channel—every message the model emits must go through the chat tool. The runtime raises the corresponding timeline marker (`AssistantMarkdownItem`) so `UiState` remains the authoritative view.
- With this split, the chat server provides shared storage and notifications; the Compositor remains the aggregated tool surface, and the frontend keeps rendering whatever arrives via `UiState`.

## MCP‑native chat delivery (target)

Expose chat via a resource + notifications so both the orchestrator and the Human UI subscribe/read like other servers. This removes the in‑proc bus coupling while keeping a simple UI.

- Sidecar client: the runtime (or any observer) must create a dedicated FastMCP client to the human chat server and subscribe directly; these notifications do **not** traverse the Compositor.

- Resource
  - URI: `ui://chat/inbox`
  - Body (JSON, `application/json`):

    ```json
    {
      "last_id": "1700000012345",
      "messages": [
        {
          "id": "1700000012344",
          "ts": "2025-10-09T12:34:50Z",
          "author": "user",
          "mime": "text/markdown",
          "content": "CI is green on main"
        },
        {
          "id": "1700000012345",
          "ts": "2025-10-09T12:34:56Z",
          "author": "user",
          "mime": "text/markdown",
          "content": "Deploy now?"
        }
      ]
    }
    ```

  - Semantics: append‑only; `id` monotonic (snowflake/ULID). Agent‑authored messages appear in the resource but DO NOT produce notifications (no self‑echo).

- Notifications
  - Emit `notifications/resources/updated` with `params.uri = "ui://chat/inbox"` for each batch of new user messages. No orchestrator‑added fields are required; server MAY include a small `messages[]` batch for automation efficiency.
  - Dual subscribers:
    - Orchestrator: pinned subscription (for agent context injection; no skipping; uses watermark/read_since).
    - Human UI: subscribes and reads the resource to render messages. The Human UI can ignore params extras and call `resources/read` for full fidelity; it does not have to rely on notification payloads.

- Tools (optional)
  - `chat_read_since({after_id, limit?}) -> {messages: [...], last_id}` — deterministic, stateless catch‑up after restart (orchestrator tracks `after_id`).
  - `chat.human.post({mime, content})` — human message submission.
  - `chat.assistant.post({mime, content})` — assistant output (used by the runtime instead of `ui.send_message`).

## Sample sequences (dual subscriptions)

Startup/hydration

1. Orchestrator enables chat‑via‑MCP mode and pins a subscription to `ui://chat/inbox`. Human UI also subscribes (or hydrates on view mount).
2. Orchestrator has a persisted `last_id` → call `chat.read_since({after_id})`; otherwise `resources/read`. Human UI calls `resources/read` to render the current inbox (does not rely on extras).
3. Orchestrator injects each message (no skipping) in order; persists the new `last_id`. Human UI renders them as they arrive or after read.

New user message

1. Human chat server appends to inbox; emits `notifications/resources/updated uri=ui://chat/inbox`.
2. Orchestrator (via its sidecar client) receives notify → call `resources/read`/`chat.read_since`, raises a `UserText` event, and reduces it into `UiState`. Human UI receives the same notify and calls `resources/read` to render the inbox (or renders from params.messages[] if provided).
3. Orchestrator persists new `last_id`.

Agent sends message

1. Runtime calls `chat.assistant.post(...)`; the assistant chat server persists the message but suppresses notifications (no self‑echo). Human clients see it via their own local echo or periodic reads; the human chat server will emit notifications only for messages authored by the human side.
2. Runtime emits an assistant timeline item (`AssistantMarkdownItem`) so the frontend updates immediately. Optionally mirror the message into the human inbox if you want all parties to rely on the same resource.

Crash/reconnect

1. Orchestrator restarts with persisted `last_id`.
2. Orchestrator (through the sidecar client) calls `ui.chat_read_since({after_id})` (or windowed `resources/read`) to fetch missed user messages.
3. Orchestrator injects messages by emitting `UserText` events; persists new `last_id`.

## Notes

- Preferred path going forward: Dual MCP subscriptions (orchestrator + Human UI) against `ui://chat/inbox`.
- Minimal UI remains supported (no subscriptions) but is considered transitional.
- Behavior mirrors <matrix.md> (batched updates, stateless watermarking, no self‑notifications) for future convergence.

---

## Example flows

These flows illustrate end‑to‑end behavior for both V1 (bus‑only) and the recommended MCP‑native mode. The UI contract remains: the assistant MUST use `ui.send_message` (no plain text assistant output).

### Human → Agent (V1 bus‑only)

1. Human writes: "Deploy now?" in the UI.
2. UI backend forwards the message to the orchestrator (out of band) and displays it.
3. The orchestrator injects the message into the next sampling turn for the model:

   ```jsonc
   {
     "messages": [{ "role": "user", "content": "Deploy now?" }],
   }
   ```

4. Model responds with tool calls (no plain text). Example:
   - Call `ui_send_message({ mime: "text/markdown", content: "Acknowledged. Running deployment…" })`
   - Then call `loop_yield_turn({})` (or, for legacy flows, `ui_end_turn({})`)
5. The Compositor (with policy middleware) gates/forwards the tool calls; UI renders the assistant message and ends the turn.

### Human → Agent (MCP‑native resource mode)

1. Human writes in UI → UI server appends to `ui://chat/inbox` and emits `notifications/resources/updated uri=ui://chat/inbox`.
2. The orchestrator (subscribed) either consumes `params.messages[]` or calls `resources/read`/`ui_chat_read_since`.
3. The orchestrator injects each new user message (no skipping) into the next sampling turn (same JSON as above).
4. Model calls `ui_send_message` followed by `loop_yield_turn`; the Compositor forwards; UI renders.

### Agent → Human (both modes)

1. Model submits `chat.assistant.post({mime, content})` (no plain text outside tools).
2. Runtime emits `UiMessageEvt` and optionally mirrors the message to human clients that rely on the chat resource.
3. Model calls `loop.yield_turn({})` to finish the turn.

---

## Message identity and high‑water marks

- Message identity
  - Each message has an `id` that is a precise, monotonically increasing identifier. Acceptable shapes:
    - ISO‑8601 timestamp with microseconds (e.g., `2025-10-09T12:34:56.123456Z`) with a per‑process sequence tie‑breaker when needed, or
    - A ULID/Snowflake that is time‑ordered.
  - In examples, `id` may equal the precise timestamp. Clients treat `id` as an opaque, ordered token.

- High‑water mark (HWM) options
  - Client‑managed: use `ui.chat_read_since({after_id})` where the client stores `after_id`. Simple and stateless on the server; duplicates are avoided if the client persists the HWM.
  - Server‑managed per‑session (auth‑derived): not used in this design. The server remains stateless for watermarking; the orchestrator maintains last delivered id.
  - Recommendation: keep server stateless; orchestrator uses `read_since` for exactly‑once delivery; Human UI may simply subscribe and call `resources/read` or use `read_since` for incremental pagination.

- Notifications vs reads
  - The UI server SHOULD NOT depend on skipped notifications for correctness. Both subscribers can always recover via `read_since`/`read_and_advance` using their last HWM.

### Subscriber identity

- Deriving identity
  - Use the MCP session’s authentication context (e.g., a participant/bearer token) as the default subscriber identity. This avoids passing `subscriber` everywhere and keeps HWMs tied to a real client principal.
  - Tools accept an optional `subscriber` parameter to override the default (for administrative reads or backfills).

- Recommended assignments
  - Orchestrator: use a dedicated, stable token if needed for server access control; watermarking does not rely on server‑managed identity.
  - Human UI: use a separate token (e.g., `subscriber = human`). Multiple human clients can either share a token (shared view) or use distinct tokens (independent views per device/tab).

- Rotation and reconnects
  - Keep tokens stable across reconnects so server‑managed HWMs persist naturally. If tokens rotate, ensure the new principal is mapped to the previous subscriber id (or pass `subscriber` explicitly during migration).

---

## Yielding turns (deprecate ui.end_turn)

- Problem: `ui.end_turn` couples yielding to a specific UI.
- Recommendation: use the neutral Loop Control server (`loop.yield_turn({})`) for the agent to signal a yield regardless of UI implementation. The `ui.end_turn` tool is deprecated and will be removed alongside the `ui` server.

### Tool call with approval (illustrative)

1. After seeing the user’s request, the model calls a privileged tool (e.g., `runtime.exec`).
2. The policy decision is `ask` → the policy middleware blocks and surfaces an approval item to the human.
3. Human approves; the policy middleware executes the tool, captures the result, and injects a concise summary to the model in the next turn (alongside any pending chat messages).
4. Model follows up by calling `ui.send_message` to present the outcome to the user, then `ui.end_turn`.
