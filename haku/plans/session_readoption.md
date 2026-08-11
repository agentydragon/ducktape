# Session re-adoption across a console roll

**Status: not built.** Recorded while the protocol details were in hand.

Today a console replica that dies takes its Claude Code session with it. The lease
(<../console/x/claude_chat.py>, `expire_stale_leases`) makes that **observable** — another
replica notices the holder stopped renewing and fails the session instead of leaving the room
waiting forever. This note is about the next step: making the session **survive** the roll
rather than merely being cleaned up after it.

## Why the sandbox is not the problem

The sandbox already outlives the console. It is a separate SandboxClaim with its own
`shutdownTime` driven by `session_ttl_seconds`, on its own pod, and the runner dials **out** to
the console — so a reconnect lands on whichever replica the Service picks, with no addressing
problem to solve.

What dies is the CLI process. `bridge_websocket_to_claude`
(<../runtime/x/agent_sdk_transport/runner.py>) terminates it in its `finally` when the socket
closes. That single line is the difference between the two designs below.

## Two designs

**A — resume in the surviving sandbox.** Let the CLI die with the console; a new replica starts
a fresh CLI in the same pod with `--resume`. `CLAUDE_CONFIG_DIR=/claude-config` is already set
in the runner image, so the session state is on disk there. Cheap: no runner protocol change.
Loses the in-flight turn.

Worth noting even if B is what gets built: resume is strictly better than the re-awakening in
<matrix_chat_runtime.md> (R3.3a), which reconstructs context from the last N room messages.
Resume restores the actual context rather than an approximation of it, and needs no summary.

**B — keep the process alive across the gap.** The runner stops killing Claude on disconnect,
buffers, and redials; an adopting console picks the conversation up mid-flight. Preserves the
in-flight turn. This is the target.

## The protocol B has to survive

Newline-delimited JSON over the CLI's stdin/stdout, which the bridge already carries verbatim
as `ClaudeMessage.payload`. **Two channels are multiplexed on it**, distinguished by the
top-level `type`.

**Conversation.** SDK → CLI is a single shape:

```json
{ "type": "user", "session_id": "", "message": { "role": "user", "content": "…" }, "parent_tool_use_id": null }
```

CLI → SDK is `assistant`, `user`, `system`, `result`, `stream_event`, plus `task_started` /
`task_progress` / `task_notification`, `rate_limit_event`, `server_tool_use`,
`advisor_tool_result`, and `transcript_mirror`. Append-only, which is what makes replay
tractable.

**Control.** Request/response correlated by `request_id`:

```json
{ "type": "control_request", "request_id": "…", "request": { "subtype": "interrupt" } }
{ "type": "control_response", "request_id": "…", "response": { "subtype": "success" } }
{ "type": "control_cancel_request", "request_id": "…" }
```

It is **bidirectional** — both ends originate requests. SDK → CLI covers `initialize`,
`interrupt`, `set_model`, `set_permission_mode`, `get_context_usage`, `mcp_status`,
`mcp_toggle`, `mcp_reconnect`, `rewind_files`, `stop_task`. CLI → SDK covers `can_use_tool`,
hook callbacks, and calls into SDK-hosted MCP servers.

## Gotcha: inbound control traffic is what normally makes this hard, and we have none

`Query.pending_control_responses` is an in-memory `request_id` → Event map. If the CLI has an
outstanding request to the SDK when the console dies, nobody answers it, the CLI blocks
forever, and an adopting console cannot know the request exists — it would have to replay
unanswered inbound requests or synthesise denials.

**That cannot happen in this deployment.** The session is built with
`permission_mode="bypassPermissions"` and `setting_sources=[]`, registers no hooks and no
`can_use_tool`, and reaches MCP as an external HTTP server the CLI contacts itself rather than
through an SDK-hosted server. So the control channel here is effectively outbound-only:
`initialize` once, `interrupt` on abort.

This removes the hardest part of B before it starts — and it is a property that can be lost by
accident. **Adding a hook, a `can_use_tool` callback, or an SDK-hosted MCP server makes
re-adoption qualitatively harder**, and whoever adds one should come back to this note.

## What B needs

- **The runner owns the `initialize` handshake.** It is per-connection state in the SDK, but
  the CLI now outlives the connection, so an adopting console must not re-handshake a process
  that is already initialized. The runner owns the process lifetime, so it is the honest owner
  of the fact that the handshake happened. Consoles come and go around it.
- **A resume point.** The runner keeps a bounded ring buffer of frames it has not seen
  acknowledged; on adopt, the console says which sequence it already has. That means a
  monotonic sequence number on runner → console frames — additive to the envelope, and by the
  envelope's own `extra="forbid"` rule it costs a `PROTOCOL_VERSION` bump.
- **An adopt path in `authenticate_bridge`.** It currently requires `status == PROVISIONING`
  and `bridge_connected_at is None`, so every reconnect is refused. Adoption must be gated on
  taking the lease — that is the arbitration that stops two replicas adopting one CLI.
- **Reading the stream directly for an adopted turn.** `client.query()` + `receive_response()`
  is request-scoped and assumes this process issued the turn. An adopted mid-flight turn has to
  be read off the transport and routed by session. This is the one place the SDK offers
  nothing.
- **An idle timeout in the runner.** A CLI held open for a console that never returns trades a
  wedged room for a wedged sandbox.

## The lease decision this revisits

`expire_stale_leases` currently treats an expired lease as **dead**: fail the session, sweep the
claim, provision a replacement. Re-adoption wants it to mean **unowned** — adoptable, with
failure only once adoption has not happened (or has been tried and failed).

That is a semantic change to one method rather than a rewrite, but it is the part of the lease
work that was decided before re-adoption existed, so it is where to start.

## Open questions

- How large a ring buffer is enough, and what should the runner do when it overflows —
  drop the session, or drop frames and let the console reconcile from its own persisted
  messages?
- Should an adopting console re-announce anything to the room, or is a silent recovery the
  better behaviour? A room that says nothing when nothing was lost is arguably correct, but it
  makes the mechanism invisible when it is new and still being trusted.
- Does `--resume` (design A) belong as the fallback when adoption fails, giving two tiers of
  recovery before a session is declared lost?
