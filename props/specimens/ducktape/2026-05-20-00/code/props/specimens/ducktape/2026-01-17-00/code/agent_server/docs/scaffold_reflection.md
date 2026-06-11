# scaffold reflection and runtime self‑modification

This document enumerates safe, practical surfaces that let the agent reflect on and (carefully) adjust its own scaffold at runtime. All mutating operations are gated by the policy middleware; read‑only surfaces are compact and structured for the model.

See also: <vision.md>, <mcp-runtime/overview.md>, <mcp-runtime/policy-gateway.md>, <mcp-runtime/resources.md>.

## Principles

- Safety first: tiny write surface; every mutating tool call is approval‑gated.
- Everything observable: expose small, typed resources for mounts, capabilities, runtime info.
- Hooks, not DSL: hooks are Python code (bounded) matching the handler model; easy migration path.
- No bypass: even hook‑emitted tool invocations go through the Compositor and policy middleware.

## Baseline dependencies

- Compositor with policy middleware (enforcement gate).
- Resources server (`compositor/*` introspection, subscriptions).
- Runtime container image `adgn-runtime` (includes Python + rg; code installed in image).
- Optional Compositor HTTP on loopback with a bearer token for the container (<mcp-runtime/overview.md>).
- Split policy servers: policy_reader (resources + decide), policy_approver, policy_proposer.

## Read‑only introspection (P0)

- runtime container info — implemented
  - Server `runtime`, URI `resource://container.info` (JSON): `{id, image, platform, os, arch, adgn_version, python_paths, tools:{rg:true}}`
  - Purpose: adapt commands/reads to container reality.
- `compositor_meta` per‑server state — implemented
  - Server `compositor_meta`, resource `compositor://state/{server}` (typed union state).
  - URIs appear under the compositor prefix (e.g., `compositor://compositor_meta/state/{server}`); use the resources server helper to translate back to the server name.
  - No dedicated mounts index resource; enumerate state resources via `resources/list` and watch `resources/list_changed` for attach/detach.
  - Instructions/capabilities are available via InitializeResult in the running state (no separate resources).
- `resources://subscriptions` — implemented
  - Current durable subs; agent can reason about what’s blocked open.

## Runtime‑updatable (P0–P1) — approval‑gated

- Compositor admin tools (P0) — implemented
  - Server: `compositor_admin`.
  - Tools: `attach_server({name, spec}) -> {ok}`, `detach_server({name}) -> {ok}`.
  - Listing state: use `compositor_meta` per‑server state resources (no `list_mounts` tool).
  - Guardrails: typed specs, secrets masked in UI/logs, idempotent detach, middleware gating.
  - Parallelism: detach/attach across different servers may run in parallel; container batches and then publishes snapshot/broadcast.
- Loop hooks — Python only (P1) — planned
  - Minimal API:
    - Tools: `loop.enable_hook({name, on:"resource_updated", matcher, source, inputs?}) -> {id}`; `loop.disable_hook({id}) -> {ok}`.
    - Resource: `loop://hooks` → `{hooks:[{id, name, enabled, on, matcher, created_at, last_error?}]}`.
  - Hook code:
    - `def handle(event, ctx) -> Effects`.
    - `event`: only type `resource_updated` with `{server, uri, ts, coalesced_count}`.
    - `Effects`: `{wake: bool, messages: [str], invoke: [{server, tool, arguments}]}`.
      - `invoke` is scheduled by the orchestrator via Compositor (policy still gates).
  - Safety:
    - Runs in adgn-runtime container; RO FS; no network; no tool calls from inside the hook.
    - Tight limits: ~200–300 ms, small memory, per‑hook rate‑limit, TTL.
  - Inputs hydration (optional): declare DB reads at enable time (see DB server) → orchestrator fetches and passes to `ctx.inputs[...]`.
  - Bridge: invoke hooks on coalesced notifications before legacy handlers; migrate handlers into hook code over time.

## DB MCP server (P1) — read‑only, namespaced views only — planned

- Server: `db` (or `agent_db`).
- Access shape:
  - Whitelisted views only: `db://view/<name>?params...` and/or `query({view, params})`.
  - No raw SQL to preserve cross‑agent safety boundaries.
- Guardrails: per‑view param schemas, size/time caps, JSON rows only; strictly read‑only.
- Hook inputs: `loop.enable_hook(..., inputs:[{server:"db", uri:"db://view/approvals_recent?limit=50"}])` → orchestrator passes rows into `ctx.inputs`.

- Advisory/testing (P0)
- Policy evaluation tool (on policy_reader) — implemented
  - Tool: `decide({name, arguments}) -> {decision, rationale}`.
  - Visibility: default hidden (middleware‑only); optionally exposed for testing.
  - Always advisory: no approval item creation; middleware still evaluates at execution.

## Priorities

- P0 — immediate value, low risk
  - Compositor admin tools (attach/detach) with middleware gating
  - Compositor meta: per‑server state (enumerate state resources; no mounts index). Instructions/capabilities via InitializeResult.
  - runtime://container.info (structured, Docker‑ish)
  - policy_reader.decide (advisory; optional exposure)
- P1 — hooks + DB
  - Loop hooks (Python only): enable/disable + loop://hooks; event=resource_updated; Effects allow wake/messages/invoke
  - DB MCP server (read‑only): named views/params only (no raw SQL); inputs hydration for hooks
  - Orchestrator bridge (invoke hooks on coalesced notifications)
- P2 — nice‑to‑haves (later)
  - Durable subscription presets tools (add/remove; pinned semantics)
  - Small loop/notify config toggles (bounded ranges) with rollback
  - Batch decide for planning (advisory only)

## Dependency DAG (logical)

- Core
  - PolicyMiddleware → required for all mutating tools (admin + hooks enable/disable)
  - ResourcesServer → exposes compositor/runtime resources + subscriptions
  - RuntimeImage → executes hooks; contains rg + adgn package
  - HTTPAccess (optional) → container→host Compositor connection (bearer)
- Surfaces
  - CompositorAdmin (attach/detach) ← PolicyMiddleware
  - CompositorResources (per‑server state via compositor_meta) ← ResourcesServer
  - PolicyReader.decide (advisory) ← PolicyReader server
  - DBServer (read‑only) ← agent DB
  - LoopHooks (enable/disable + loop://hooks) ← PolicyMiddleware, RuntimeImage, ResourcesServer
  - HookInputsHydration (optional) ← DBServer
  - OrchestratorBridge (deliver coalesced notifications to hooks) ← ResourcesServer

ASCII

```
PolicyMiddleware ──→ CompositorAdmin
PolicyMiddleware ──→ LoopHooks
ResourcesServer  ──→ CompositorResources
ResourcesServer  ──→ OrchestratorBridge ──→ LoopHooks
RuntimeImage     ──→ LoopHooks
DBServer         ──→ HookInputsHydration ──→ LoopHooks
PolicyReader.decide (advisory)
HTTPAccess (container→host Compositor) [optional]
```

## Migration notes

- Start with P0: admin tools + introspection + advisory decide; read server instructions at attach/init and inject a compact summary to the model.
- Add P1 hooks+DB: keep handlers; run hooks first; migrate handler bodies into hook code incrementally.
- Keep mutating tools gated by policy; log concise rationale strings; retain idempotence and rollback paths.
