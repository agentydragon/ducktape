# Self-Reflective Agent Runtime — Proposed Architecture

This note sketches a path toward a “self-healing” / self-reflective MCP runtime. The goal is to let the agent evolve its own behaviour while keeping hard guard rails for policy, auditing, and operator control.

## High-Level Goals

- Give the agent an execution environment (container) it can shape: install hooks, maintain state, tweak handlers, even hot-swap its own loop.
- Keep human oversight and policy enforcement outside the agent’s control (approvals, audit logs, global configuration).
- Provide a clear interface where the agent can request new capabilities (network, credentials, MCP servers) and where humans can approve/deny them.
- Support present-day workflows (chat + tools + approvals) while opening a path toward more autonomous “brain surgery” by the agent itself.

## Components

### Control Plane (trusted / outside the agent container)

- **Policy Gateway & Approval Hub**
  - Terminates every MCP `tools/call` from the agent container (including hooks and background jobs).
  - Evaluates policy, creates approval items, emits JSON-RPC errors on denials.
  - Stores decisions + rationale in the outside DB.
- **UI / Interaction Hub**
  - Shows chat (straight from Matrix/other backend), approvals, and audit events sourced from the policy gateway.
  - No agent code runs here; it simply subscribes to external feeds and the approval store.
- **External Tool Bridges**
  - Seatbelt exec, seatbelt net, other privileged surfaces. Live outside the container; enforce logging and policy.
- **Control-Plane Database**
  - Authoritative store for approvals, audit logs, long-lived configuration. (Chat transcripts live in external systems such as Matrix.)
  - Agent container accesses this only through control-plane MCP servers (no direct credentials).

### Agent Container (self-managed sandbox)

- **Loop Daemon (Agent + handlers)**
  - Runs the agent turn loop; hot-swappable via a lightweight supervisor process.
  - Maintains the agent transcript/event stream and loop-control latch; any UI projection (`UiState`) is built in the control plane.
  - Implementation sketch:

    ```text
    agentd/
      supervisor.py    # entrypoint; handles control API, restarts
      loop_worker.py   # Agent loop + handler stack
      hooks.py         # hook registry & scheduler
      mcp_clients.py   # shared MCP clients (chat, resources, policy gateway)
      db.py            # thin wrapper around agent-scoped DB (SQLite/functional)
      config.py        # loads credentials, handler configs, hook defs from DB
    ```

  - Supervisor boots `loop_worker`, exposes the control API, and watches for restart/shutdown signals.
  - `loop_worker` pulls pending inputs (chat events, hook effects), runs `agent.run_turn(...)`, and streams tool calls through `mcp_clients` (policy gateway enforces approvals).
  - `hooks.py` executes hook code in-process, producing “Effects” (`wake`, `messages`, `invoke`) that the worker consumes on the next turn.

- **Hook Manager**
- **Hook Manager**
  - Provides `loop.enable_hook` / `loop.disable_hook` / per-hook resources (`loop://hooks/{id}`).
  - Hooks run in the container’s Python environment with scoped credentials handed in at registration.
- **Local MCP Clients & Proxies**
  - All outbound tool calls originate here: resources, seatbelt, OpenAI (via a local proxy for rate limiting / logging).
  - The container speaks MCP directly to the control-plane policy gateway for anything that needs approvals.
- **Direct Integrations**
  - Some services (Matrix, Git, custom APIs) may be accessed directly via scoped credentials instead of MCP. The agent can store those tokens in its container DB and talk to the service using native tooling (CLI or SDK). Operators rely on the service account’s permissions plus external auditing (Matrix room history, Git commit history) rather than the policy gateway for oversight.
- **Direct Credentials / Service Accounts**
  - Some integrations (e.g., Gitea, internal APIs) may be granted via scoped service accounts instead of MCP tools. The agent can store these secrets in its container DB or environment.
  - These credentials define the policy boundary (least privilege). Approvals may be bypassed if the scoped account already limits risk; otherwise, the agent can still front them with an MCP proxy if human oversight is desired.
- **Agent-Scoped Database**
  - Embedded store (SQLite, LiteFS, or functional DB). Holds scratch state, hook metadata, cached resources.
  - Agent (and hooks) have full control; sync to control plane happens only via explicit MCP calls.
- **Supervisor**
  - Manages hot-swapping the loop daemon, reloading handlers, restarting on crash.
  - Supports two update flows: external (operator pushes new code) and internal (agent schedules a restart through an MCP tool after updating its configs).
- **Additional Processes**
  - The container is free to launch extra services or nested agents (e.g., secondary loop workers, background daemons). The control plane sees them only through the audit trail/health endpoint.

## Data & Access Model

| Surface                                  | Location                                    | Mutability                                                                       | Access Path                                                                                                                                                                                       |
| ---------------------------------------- | ------------------------------------------- | -------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Approvals, audit, global config          | Control-plane DB                            | Read/write by control plane only                                                 | Agent reads via MCP resources; writes only through approved tools                                                                                                                                 |
| Agent scratch state (hooks, caches)      | Agent container DB                          | Read/write by agent                                                              | Optional replication to control plane via approved tools                                                                                                                                          |
| Chat transcript                          | External chat backend (Matrix, Slack, etc.) | Managed by that service                                                          | Agent posts via native tooling inside the container (e.g., matrixctl/python client with scoped token); humans use the existing frontend; UI reads directly from the backend (no control-plane DB) |
| Audit log (tool calls, policy decisions) | Control-plane DB                            | Append-only by control plane                                                     | Populated by policy gateway; UI can surface notable events                                                                                                                                        |
| Chat inbox (human/assistant)             | Separate MCP chat servers                   | Read/write by owning principal; notifications suppressed for self-authored posts | Agent uses `chat.assistant.post`; human client uses `chat.human.post`                                                                                                                             |

## Execution Flow

1. **Turn Start:** Control plane invokes the loop daemon (or the agent self-wakes). Loop handlers (including loop-control latch) run inside the container.
2. **Tool Calls:** Agent (or hook) issues MCP call via container client → hits policy gateway → (if approved) proxied to target server → response returns through gateway.
3. **Approvals:** Gateway blocks on `ask`, surfaces item to UI. Operators respond; result executes or returns denial. Full history recorded in control-plane DB.
4. **Chat:** Agent uses `chat.assistant.post`. Control plane suppresses notifications from the assistant chat server; the UI shows assistant messages straight from chat records. Human chat server notifies only human-originated messages; the agent subscribes via sidecar client and injects user messages into the next turn.
5. **Yield:** Agent calls `loop.yield_turn`; loop server flips the handler latch, the handler returns `Abort()`. Operators can rely on audit entries or run-status updates to see turn boundaries; explicit timeline markers are optional.

## Hooks & Automation

- Hooks remain optional initial work; once implemented they let the agent install sandboxed routines for “watching this webpage and alerting me”.
- Hook enablement flows:
  1. Agent calls `loop.enable_hook(...)` with code + metadata; policy gateway approves/denies.
  2. Hook manager stores hook code in agent DB, allocates scoped credentials (e.g., read-only DB token, chat assistant token, resources sidecar token).
  3. On matching events (e.g., `resources/updated`), hook executes in container and returns `Effects` (`wake`, `messages`, `invoke`) back to orchestrator.
- Hooks are audited by listing `loop://hooks/{id}`; human operator can disable them via MCP.

## Optional Extensions

- **Write-Enabled DB Views:** Introduce policy-gated MCP tools for mutation (functional/forkable DB writes). Agent proposes changes; control plane applies them after approval.
- **Self-Managed Handlers:** Store handler configs/code in agent DB; agent updates them and requests a supervisor restart via MCP.
- **OpenAI Sampling Proxy:** Run a local proxy in the container to rate-limit/log OpenAI calls; control plane can suspend or adjust budgets without modifying the agent code.

## Container Control Protocol

The agent container exposes a minimal HTTP interface to the host. No timeline or audit data flows over it; those are derived from external systems (Matrix, policy gateway).

| Endpoint            | Method | Purpose                  | Notes                                                                                                                                      |
| ------------------- | ------ | ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `/healthz`          | `GET`  | Liveness/readiness check | Returns 200 with `{ "status": "ok" }`. Optional `/readyz` split if desired.                                                                |
| `/control/restart`  | `POST` | Hot-swap loop worker     | Body `{ "reason": "upgrade" }`. Supervisor drains the current turn (or aborts if forced), reloads code/config from agent DB, and restarts. |
| `/control/shutdown` | `POST` | Graceful stop (optional) | Used when tearing down the container.                                                                                                      |

- Authentication: expose the interface on `localhost`/Unix socket or protect with a simple bearer token injected via Docker secrets.
- No other RPCs are required; agent-initiated work (hooks, tool calls, chat) continues to use outbound MCP clients.
- Additional commands (e.g., `POST /control/reload_handlers`) can be added later if we split restart semantics.

## Operator Workflows

- Approve/deny high-risk tool calls (runtime exec, external network, policy edits).
- Observe chat via Matrix (or chosen backend) and audit feed via the policy gateway; UI can synthesise `UiState` purely from those external sources.
- Inspect hooks via `loop://hooks/{id}`; disable suspicious automation.
- Reconfigure mounts via `compositor_admin` tools—still routed through policy gateway for logging.
- Grant new credentials (DB write scopes, external tool tokens) by injecting them into the agent container (e.g., via MCP tool that writes secrets into the agent DB).

## Summary

This architecture keeps trust boundaries tight—everything that must be guarded (approvals, audit, sensitive tools) stays outside the agent container—while giving the model real leverage inside its sandbox. Deployment looks like this:

- You run the sampling loop, handlers, and automation entirely inside the container.
- You connect external systems (Matrix, Gitea, OpenAI via proxy, custom APIs) directly to that container, either through MCP mounts (with policy approval) or scoped service accounts.
- The control plane remains minimal: UI, policy gateway, approval DB, and any privileged bridges you do not want the agent to own.

The container exposes only a very small surface to the host—e.g., a health endpoint and a heartbeat/ status stream. It does **not** publish detailed timeline events; approvals, denials (`deny_abort`, etc.) and sampling audits come from the policy gateway/proxy outside the container.

Within that setup the agent can “perform brain surgery” on itself—modify handlers, install hooks, persist state—yet every action that crosses the boundary is mediated by the policy gateway (when using MCP) or constrained by the credentials you hand it. Humans can still observe, approve, or revoke access at any time.

### Pilot v0 snapshot

- Only Matrix integration is enabled. The agent polls and posts to the room using native tooling in the container; humans use the standard Matrix client.
- The pilot agent uses OpenAI Responses with two tools: `run_shell_command` (executes shell commands inside the container) and `yield_control` (sleep until new Matrix traffic). No MCP surfaces or additional approvals are present.
- MCP tool surfaces, hooks, DB writes, and self-managed handler reloads remain disabled until later pilots.
