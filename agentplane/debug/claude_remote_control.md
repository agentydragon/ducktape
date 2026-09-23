# Claude Code Remote Control vs. the stdio seam

Whether Claude Code's Remote Control (`--remote-control`, `claude remote-control`, `--rc`) speaks
the protocol the runner speaks, and whether it can be pointed at an endpoint we host.

Read out of the shipped bundle, Claude Code **2.1.245** (`.claude-wrapped`, Bun-embedded JS,
`GIT_SHA 28b7e8c41235a9e2fcb24248e60c4cc2d29c853a`). The roster pins 2.1.252; re-check the
constants below against the pinned build before relying on them.

## It is the same protocol

Remote Control carries the same `stream-json` frames the runner exchanges over stdio, on a cloud
WebSocket, with claude.ai and the mobile app in the seat the runner occupies locally.

The transport object exposes two write paths, both carrying SDK frames verbatim:

```js
writeSdkMessages(msgs) {
  let fresh = msgs.filter(m => !m.uuid || !seen.has(m.uuid));   // uuid dedup
  transport.writeBatch(fresh.map(m => ({...m, session_id: sid})));
},
sendControlRequest(req) { ... }                                 // can_use_tool, request_user_dialog
```

Ingress (`[bridge:repl] Ingress message type=…`) accepts `control_response`, `control_request`, and
message frames of which only `type === "user"` passes (`Ignoring non-user inbound message`), with
`isReplay: true` echo suppression and uuid dedup — the `--replay-user-messages` semantics.

The inbound `control_request` handler answers with
`{type:"control_response", response:{subtype:"success"|"error", request_id, response}}` over:
`initialize`, `set_model`, `set_max_thinking_tokens`, `set_permission_mode`, `rename_session`,
`set_color`, `file_suggestions`, `read_file`, `get_context_usage`, `get_usage`, `mcp_status`,
`mcp_authenticate`, `mcp_oauth_callback_url`, `mcp_reconnect`, `interrupt`, `apply_flag_settings`,
`stop_task`. `initialize` replies with `{commands, agents, output_style, available_output_styles,
models, account, pid}` plus `pending_permission_requests` / `pending_user_dialog_requests`.

**This is a readable source for control surfaces [the roster](../native/docs/protocol_roster.md)
marks "(inferred)"** from `strings` — the handshake response shape, the runtime-knob subtypes,
`request_user_dialog`, `stop_task`. Claude Code ships no source, but this path is unminified enough
to read, so those rows can be promoted from inferred to observed without a live probe.

## Where it differs from stdio

- **Transport**: WebSocket, OAuth account credentials, device enrollment, org policy gate
  `allow_remote_control` (managed setting `disableRemoteControl`).
- **Envelope**: `session_id` stamped per frame, batched writes, sequence numbers for reconnect
  backfill, attestation (`bridge_event_attestation`, `bridge_control_request_attestation`).
- **Ingress is narrower**: `user` plus control frames only; no arbitrary frame injection.
- **Egress is wider**: the forward filter passes `user`, `assistant`, `system` (`local_command`,
  `compact_boundary`), `attachment` (`hook_system_message`, `tool_host_result_lines`,
  `queued_command`), `conversation_reset`, `rate_limit_event`, and `system` subtypes `status`,
  `task_started`/`task_progress`/`task_updated`/`task_notification`, `background_tasks_changed`,
  `thinking_tokens`, `code_change_published`, `vcs_state_changed`. Several are absent from the roster.
- **`outboundOnly` mode**: mirror-only; every control_request except `initialize` is rejected.
- **Two flavors**: `[bridge:repl]` attaches a live REPL; `runBridgeHeadless` is a spawn-on-demand
  daemon (`capacity`, `spawnMode`, `createSessionOnStart`) behind `claude remote-control`.

## The endpoint cannot be redirected

The env registry declares an ungated string `CLAUDE_BRIDGE_BASE_URL: () => dD` where `dD = t.str()`,
alongside `CLAUDE_BRIDGE_{OAUTH_TOKEN,SESSION_INGRESS_URL}` and `CLAUDE_REMOTE_TOOLS_BRIDGE_URL`,
and a validator that explicitly permits local development:

> `Error: Remote Control base URL uses HTTP. Only HTTPS or localhost HTTP is allowed.`

with `localhost` and `127.0.0.1` as sibling constants. It is not a self-hosting hook. Setting
`CLAUDE_BRIDGE_BASE_URL=http://127.0.0.1:<port>` (proxies cleared) and running `claude
remote-control` sent **nothing** to a listener on that port; the CLI connected to the real bridge.
The WebSocket endpoints are hardcoded module constants —

```js
var Nt = "wss://bridge.claudeusercontent.com",
  Gt = "wss://bridge-staging.claudeusercontent.com";
```

— with a host allowlist pinning those two names and the permitted path prefixes
(`/v2/session_ingress/shttp/mcp/`, `/v2/session_ingress/mcp/ws/`, `/v2/ccr-sessions/`, `/v1/code/`).
Prod vs staging is chosen by OAuth environment.

Unresolved: which consumer _does_ read `CLAUDE_BRIDGE_BASE_URL` — plausibly only the HTTP session
calls (`bridge_session_create`/`_get`/`_patch`), or it is gated on `USE_LOCAL_OAUTH` /
`USE_STAGING_OAUTH`, which sit beside it in the same env cluster. Not traced.

**Gotcha:** `claude remote-control` connects to the signed-in account immediately and with no
confirmation — it registers an `env_…` environment and creates a `session_…` on claude.ai, both
visible in the account afterwards. Treat any live run as account-visible.

## Consequence for the runner

Remote Control is not a transport we can borrow: the peer is fixed to Anthropic's bridge. It is
evidence that the seam's protocol carries a remote attachment unchanged — a cloud-attachable
agentplane session means serving these same frames over our own socket, which
[the runner](../runner/SPEC.md) already does over stdio.

Untested: whether `--remote-control` does anything under `-p --input-format stream-json`. It is
accepted without error there, and no bridge connection was observed, but none was checked for.
