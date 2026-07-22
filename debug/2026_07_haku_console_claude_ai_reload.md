# haku-console claude.ai connector "Couldn't reload tools from the server" — RCA (2026-07-22)

## Symptom

claude.ai's custom connector for `haku.allegedly.works` (haku-console `/mcp`)
showed "Couldn't reload tools from the server" on every tool refresh and
retried roughly every 30 s. The server returned HTTP 200 for `initialize`,
`tools/list`, and `tools/call`, and token refresh (`POST /mcp/token`)
succeeded — so this was neither an auth/connectivity failure nor a dead
connector.

## Root cause

haku-console emitted a non-conformant `outputSchema` on its proxied tools.
`_output_schema` (in `mcp_server.py`, removed by the fix) modeled each proxied
tool's result-or-promise return as a top-level JSON Schema:

```json
{"oneOf": [{"<upstream result shape>"}, {"<ToolCallPromise>"}], "$defs": {...}}
```

claude.ai requires every tool's `outputSchema.type` to be exactly `"object"`
and rejects a top-level `oneOf`/`anyOf`/`allOf`; one bad tool invalidates the
whole `tools/list`. This is `anthropics/claude-ai-mcp#400` (a ZoomInfo twin:
HTTP 200 on `tools/list`, then "Couldn't reload tools from the server" on
refresh, with a Zod `invalid_value` at `outputSchema.type`). The 200 is the
server answering; claude.ai then runs its own client-side validation pass and
drops the batch.

`_build_proxy_tool` applied `_output_schema` to every proxied tool whose
upstream declared an output schema — 129 of 188 tools: `home_assistant` 75,
`grocy_sf` 30, `gmail` 18, `google_calendar` 4, `haku_routine` 1, `hostexec` 1.
It was **not** gated on the auto-approval boundary: passthrough and
approval-envelope tools alike. The bad shape was haku-console's own synthesis
(the `oneOf` added the `ToolCallPromise` branch haku-console injects), not the
upstream servers'.

## Fix

Stop emitting `outputSchema` for proxied tools (`output_schema=None` in
`_build_proxy_tool`). `outputSchema` is optional in MCP; claude.ai loads the
tools without it, and the promise behavior is already described in each tool's
description. Native console tools (`list_mcp_servers`, `list_node_daemons`,
`get_mcp_server_status`, …) keep their own conformant `{"type":"object"}`
schemas — they don't go through the proxy path.

## What it was NOT

- **Not auth.** Token refreshes succeeded (last one ~1.2 h before diagnosis);
  only 4 transient `POST /mcp/token` 401s in 14 d (CNPG connection-reset blips
  around node churn), each recovered within ~30 min. That is the known
  `debug/2026_06_claude_ai_connector_deauth.md` pattern, but not this incident.
- **Not a tool-count cap.** Anthropic documents no per-connector tool limit;
  the only count-related ceiling is a 256-tool silent-truncation defect
  (`anthropics/claude-ai-mcp#587`, Cowork/Desktop) — silent, and > 188. The
  deferred-tool-loading / `tool_search` mechanism Anthropic built exists
  precisely so large tool sets don't front-load.

## Open

- Confirm post-deploy: after the new image rolls out, a claude.ai reload (or
  `/mcp` in Claude Code, which surfaces the raw Zod error) should show no
  validation failure.
- haku-console still doesn't export `mcp_auth_upstream_refresh_failures_total`
  or have a ServiceMonitor — the 2026-07-02 RCA's alerting fix covered the
  other facades, not this one. Separate follow-up.
