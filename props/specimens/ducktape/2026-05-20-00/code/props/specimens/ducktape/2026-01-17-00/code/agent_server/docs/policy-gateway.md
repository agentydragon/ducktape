# policy middleware in the compositor — implementation spec (V1 sync)

All agent and container tool/resource calls go through the Compositor surface.
This document specifies the V1 (synchronous) policy middleware implemented as pre‑dispatch filtering inside the Compositor (aggregator). The Compositor remains the aggregation layer and also becomes the authoritative ingress for model‑initiated tool calls (and, when enabled, programmatic calls from inside the container). Resource management (list/read/subscribe) is handled by a dedicated resources MCP server mounted under the Compositor; the policy middleware does not add resource semantics. See <overview.md> for runtime architecture, and <../vision.md> for the philosophy behind executable policy and graduated autonomy.

Scope

- V1 (sync) only: in‑proc first, HTTP optional; approvals block the call at the proxy; no unified async returns
- Chat/message delivery and notification batching are out of scope for the policy middleware; these live in Loop Control and/or UI/chat servers. See overview and ui-chat docs for future directions.

---

## 1. Responsibilities (V1 Sync)

- Gate all `tools/call` via the Approval Policy (source of truth) before dispatching to the mounted server
- Dispatch approved calls to the target mount and return upstream results
- Return JSON‑RPC errors on denials (`policy_denied`, `policy_denied_continue`)
- Block on `ask` (V1); create an approval request; execute on approve; error on reject
- Provide agent‑only Loop Control tools via a `loop` mount under the Compositor (middleware does not add semantics; orchestration lives with Loop Control/handlers)

Not responsibilities

- Aggregating resources: handled by the Compositor (and optionally a dedicated `resources` server). The policy middleware may observe `resources/*` for logging but adds no semantics.
- Aggregating tools: Compositor remains the aggregation layer; the policy middleware is a pre‑dispatch policy filter only.
- Chat/inbox delivery, high‑watermarks, batching/coalescing of notifications: handled by UI/chat servers and/or Loop Control and handlers.

---

## 2. Architecture & Data Flow

- In‑proc (V1 baseline)
  - Agent (Agent) → Compositor (FastMCP proxy mounts + policy middleware) → Upstream servers
  - Container client → Compositor over loopback (host.docker.internal) → Upstream servers
- Human UI integrates separately via the UI server and/or human‑only MCP servers (e.g., policy authoring). The policy middleware does not own chat/inbox flows.

HTTP (optional)

- The Compositor may expose a loopback HTTP MCP endpoint for automation clients (the container). Do not expose upstream mounts directly.
  - Transport: Streamable HTTP with bearer auth (FastMCP). Start with a constant token; later rotate per session.
  - Bind: 0.0.0.0 on a free port; container reaches it via `host.docker.internal:<port>` (Linux: host-gateway/host networking).
  - Build the shared runtime image via `docker/runtime/Dockerfile`; policy evaluation reuses the same base and is launched with stricter runtime flags.

---

## 3. Interfaces

3.1 Middleware surface (inside the Compositor)

- Intercept `tools/call` pre‑dispatch; compare the namespaced tool (`{server}_{tool}`) against policy rules
  - Tool names constructed via `build_mcp_function(server, tool)` from `mcp_infra.naming`
- Allow: dispatch to the target mount and return result
- Deny: map to JSON‑RPC error
- Ask: create approval item; await resolution; then approve→dispatch or deny→error
- Resources: no enforcement changes; normal Compositor behavior for `resources/*`

  3.2 Loop Control tools (agent‑only)

- Mounted under Compositor as `loop` server: `loop_yield_turn({}) -> {ok}`
- Visible/callable to agent only; hidden/denied to Human UI

---

## 4. Approvals — Proxy Enforcement (sync)

Decisions

- allow → execute upstream; return upstream result
- deny_continue → JSON‑RPC error `policy_denied_continue` (-32951); do not execute
- deny_abort → JSON‑RPC error `policy_denied` (-32950); agent sets abort latch
- ask (V1) → block; create an approval item exposed via the human‑facing policy server/UI; on approve execute+return; on reject return `policy_denied_continue`. The agent/model does not see "pending approvals" in sync mode.

Policy evaluation (via MCP tool on policy_reader)

- The policy middleware calls `decide({name, arguments}) -> {decision, rationale}` on the `policy_reader` server.
- Visibility: default hidden; may be exposed to agent/human tokens for testing/advisory checks. Enforcement still occurs only in the middleware when real tool calls are gated.
- Backend selection (container, in‑proc, etc.) is an internal detail of the `policy_reader` server. The container backend launches a short‑lived Python process using the runtime image resolved via `ADGN_RUNTIME_IMAGE` (default `adgn-runtime:latest`).
  - Transport note (important): avoid streaming request JSON over a hijacked HTTP connection into container stdin. On VM‑backed Docker engines (e.g., Colima, some Desktop setups), `attach_socket` + half‑closing stdin can stall or time out at the client. Instead, inject the request JSON and policy source via environment variables (e.g., `POLICY_INPUT`, `POLICY_SRC`) and run a tiny shim that feeds `POLICY_INPUT` into `sys.stdin` before executing the policy.
  - Environment size is ample for policy programs and request JSON: macOS exec argv+env limit is ~256 KB, typical Linux arg_max is ~2 MB. Our payloads are O(kB), well under limits.
- Timeout/error → map to `policy_evaluator_error` (`code = -32953`); the policy middleware returns a structured JSON‑RPC error with `{name, reason}`.

### Reserved policy errors and remapping

- Reserved errors (middleware only):
  - `policy_denied` (`code = -32950`)
  - `policy_denied_continue` (`code = -32951`)
  - `policy_evaluator_error` (`code = -32953`)
- Backends must not emit the reserved denials; only the middleware does. If a backend raises or returns these (by code or message), the middleware remaps them to:
  - `policy_backend_reserved_misuse` (`code = -32952`)
  - `data` includes `{ name, backend_code? }` to aid diagnostics

Testing mode (advisory)

- The `policy_reader.decide` tool may be exposed to agent/human tokens for testing and planning.
- Advisory only: calling `decide` does not create approval items or alter enforcement; it is a dry‑run prediction of the policy outcome. The policy middleware still evaluates and enforces at tools/call time.

Policy servers (split: reader, approver, proposer)

- Split responsibilities across three MCP servers to enforce least privilege:
  - policy_reader (read‑only): `resource://approval-policy/policy.py`; `approvals://pending/<id>`
  - policy_approver (human‑only): `approve({id})`, `deny({id})`, optional `set_policy({proposal_id})`, `set_policy_text({source})`
  - policy_proposer (agent/model): `propose_policy({source}) -> {id}`
  - Principals: agent → proposer+reader; human → approver+reader

Approvals UI integration (Human‑only)

- The policy middleware integrates with the Human UI to present approval‑pending items. Implementation options (choose one):
  - In‑proc UI bus: call a UI‑side callback to enqueue an approval request (call_id, tool, args) for display; the UI invokes a resolve method, which unblocks the call.
  - MCP servers: the UI connects to policy_reader + policy_approver. The reader provides the queue/resources; the approver exposes tools. The policy middleware awaits resolution for tool calls; policy changes are applied via `set_policy`.
- The agent handler is not involved in ask resolution. Pending approvals are not injected into model context in sync mode.

---

## 5. Orchestration & Yield (Sync)

The policy middleware does not own orchestration semantics. Turn scheduling, wake sources, and end‑turn behavior are defined by the Loop Control server and handlers. See <control.md> and <overview.md> for details. In V1, approvals resolution does not inject intermediate “pending” state into the model context.

---

## 6. Transports & Routing

In‑proc (preferred for V1)

- Use FastMCP client in‑proc to call the Compositor; no auth required

HTTP (optional)

- Container: loopback HTTP (container → host.docker.internal) to the Compositor with policy middleware. Do not expose upstream mounts.
- Human UI (if needed): UDS/loopback + JWT

---

## 7. Resources Management (out of scope)

Policy middleware does not implement resources semantics. See `docs/mcp-runtime/resources.md` for the dedicated Resources server design and how active subscriptions are exposed to clients.

---

## 8. Persistence

The policy middleware does not persist chat or resource subscription state. Persisted runtime mounts live with the Compositor; resource subscriptions live with the resources server; watermarks/HWMs are maintained by the agent orchestrator/handlers if used (see the overview). The middleware may maintain only ephemeral in‑memory state for approval‑pending calls while waiting on a human decision (or back it with the same SQLite overlay if running multi‑process).

---

## 9. Errors (JSON‑RPC)

- Deny (abort latch):

```json
{
  "jsonrpc": "2.0",
  "error": {
    "code": -32950,
    "message": "policy_denied",
    "data": { "type": "policy_denied", "decision": "deny_abort", "reason": "…" }
  },
  "id": 42
}
```

- Deny (continue):

```json
{
  "jsonrpc": "2.0",
  "error": {
    "code": -32951,
    "message": "policy_denied_continue",
    "data": { "decision": "deny_continue", "server": "runtime", "tool": "exec", "reason": "…" }
  },
  "id": 17
}
```

- Evaluator error (timeout/exception while deciding):

```json
{
  "jsonrpc": "2.0",
  "error": {
    "code": -32953,
    "message": "policy_evaluator_error",
    "data": { "name": "server_tool", "reason": "TimeoutError: …" }
  },
  "id": 17
}
```

Note: resource subscribe/unsubscribe error mapping belongs to the resources/compositor docs. The policy middleware primarily maps approval denials on `tools/call`.

---

## 10. Configuration

- Policy evaluator: `ADGN_RUNTIME_IMAGE` selects the container image; optional limits set via `ADGN_POLICY_EVAL_TIMEOUT_SECS`, `ADGN_POLICY_EVAL_MEM`, `ADGN_POLICY_EVAL_NANO_CPUS`
- Tokens (only when crossing processes): automation bearer (agent), human JWT

---

## 11. Logging & Telemetry (minimal)

- Log policy decisions (allow/deny/ask) with tool key and rationale (redact secrets)
- Log error mappings (codes), evaluator timeouts, and blocked durations for ask
- Optional: per‑server call latencies and pool saturation events

---

## 12. Testing Plan

- Unit: decision mapping (`allow`/`deny_*`/`ask`); error shapes; evaluator timeouts
- Integration: V1 sync flow — approvals at proxy; Loop Control yield; sleep_until_user; container calls routed through the Compositor (policy middleware enforces)
- Resource server: list/read/subscribe basic smoke; synthetic state URIs (if used)
- Security: container cannot reach Compositor; agent‑only Control not visible on human token

---

## 13. Migration Plan (from legacy manager to Compositor)

1. Install the policy middleware as pre‑dispatch inside the Compositor for `tools/call`.
2. Remove any handler‑based enforcement; handlers may retain visibility/logging only.
3. Use the dedicated Resources server for `resources/*` operations; the Compositor mounts it.
4. Mount Loop Control via Compositor; update prompts/instructions to use `loop_yield_turn`.
5. Update container clients to call the Compositor loopback (host.docker.internal); do not expose upstream mounts directly.
6. Optional: chat/inbox flows are documented in <ui-chat.md> and the overview; the policy middleware does not implement them.

---

## 14. Decisions (V1 Sync)

- Approvals handler: removed. Enforcement is solely at the policy middleware; any prior handler concerns can be split into other focused handlers or dropped.
- Wake behavior: approvals/resolutions do not wake. When chat is enabled, chat and finished tool results wake; initial V1 excludes chat/inbox flows.
