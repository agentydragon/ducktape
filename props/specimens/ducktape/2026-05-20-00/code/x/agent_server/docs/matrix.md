# matrix mcp server

This document captures Matrix‑specific behavior and how it integrates with the runtime orchestrator (handlers) and the agent loop. The policy middleware enforces approvals for tool calls but does not own chat delivery. The current UI server is a simple chat room; Matrix is one concrete way to deliver chat messages via MCP notifications. See also <overview.md> and <../vision.md>.

## Goals

- Deliver all non‑self messages to the agent without skipping.
- Remain efficient and safe with multiple clients using the same user.
- Avoid requiring the human UI to subscribe or render notifications.

## Notifications (stateless watermarking)

- On new non‑self events, the Matrix MCP server emits:
  - `notifications/resources/updated` with params:
    - `uri: matrix://room/<room_id>/last`
    - `messages: [{event_id, sender, ts, body}, …]` — new non‑self messages since the prior notification (the server may compute this batch internally, but does not advance any shared read marker on behalf of clients).
- Self‑authored events (from `matrix_user_id`) are excluded from both notifications and reads.

## Orchestrator Behavior

- Primary consumer: the orchestrator subscribes to the Matrix resource and consumes `messages[]` to inject every message in order (no skipping). If the batch is large, the orchestrator splits across turns while preserving order.
- Persistence: the orchestrator stores its own `last_id` after scheduling delivery for crash/restart recovery.
- Catch‑up: On startup/reconnect, the orchestrator calls a catch‑up method to read strictly after its persisted `last_id`:
  - `matrix.read_since({room_id, after_event_id, limit?}) -> {messages, upto_event_id}`
  - or via a parameterized resource: `matrix://room/<room_id>/since/<event_id>`

## UI Behavior

- Optional: the default UI does not subscribe or render notifications. It focuses on approvals and management.
- If an activity feed is enabled later, it can render raw notification lines or display `messages[]` directly without reads.

## Resource & Tools

- Resource: `matrix://room/<room_id>/last` — compact snapshot of latest non‑self messages; MIME `application/json` or `text/markdown`.
- Tools:
  - `matrix.read_since({room_id, after_event_id, limit?}) -> {messages, upto_event_id}` — for deterministic catch‑up.
  - `matrix.set_read_marker({room_id, event_id}) -> {ok}` — optional override (not needed in steady state since the server advances on notify).

## Example

Example — Matrix notifications (agent‑facing injection will render each message):

```
Latest Matrix messages (room !abc:hs):

[2025-10-09 12:34:50Z] alice: CI is green on main
[2025-10-09 12:34:56Z] bob: Deploy now?
```

Example — Raw notification line (UI, if enabled):

```
notifications/resources/updated uri=matrix://room/!abc:hs/last
```

## Notes

- Security: treat `access_token` as sensitive; bind the server over loopback and require bearer auth in `McpServerSpec`.
- Watermarks in the orchestrator: stored in an agent‑side table (e.g., `resource_watermarks`) as needed to build consolidated notifications. The Matrix server does not advance shared read markers by default.
