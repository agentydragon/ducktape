# Autonomous Agent Design Notes

## Communication

- Should be able to talk to me via messengers like Telegram or Matrix

## Permissions

- Graduated, tweakable permissions (like the approval-gate MCP server we've been building)

## Current State (OpenClaw in devel)

OpenClaw is wired up in devel. Its exec tool only knows three modes:

- Sandbox by Docker
- Execution in gateway, unsandboxed
- Execution in node, unsandboxed

No graduated permission model — it's either fully sandboxed (Docker) or fully unsandboxed.

## Infrastructure

- Agent should have its own k8s namespace
- A bunch of tokens it can use autonomously
- Some services available only with approvals (e.g., Gmail: read often OK, write only with approval)

## OpenClaw Pros/Cons

### Good

- Good support for messengers

### Problems

- ~~No native MCP support~~ **Correction**: OpenClaw does have native MCP via `@modelcontextprotocol/sdk` (stdio + SSE). Configure in `openclaw.json`. Best for local stdio servers; remote HTTP MCP support has open requests (#8188, #29053).
- Approval workflow doesn't fit well:
  - Need to be able to resume the agent with a notification when approvals are resolved (granted or denied)
  - OpenClaw has wiring for system notifications, but they only get delivered on next heartbeat
  - Our plugin cannot trigger an OpenClaw heartbeat — that's not part of the plugin API
  - Sub-agents and `async-task` exist for background work, but the approval notification path specifically goes through system notifications → heartbeat
- This is a blocker for the permission-based workflow: the agent needs to be notified when a tool use that got blocked on approval is granted/denied, and the agent loop needs to be poked to continue running

## Evaluation Dimensions

When comparing frameworks (OpenClaw, ZeroClaw, others), evaluate along these axes:

### Message Channels

- Telegram support
- Matrix support

### Triggers

- Cron support
- Generic trigger mechanism (e.g., webhooks)

### MCP Support

- Can you connect MCP servers to the agent to let it use their tools?
- Native or via workaround?

### Execution Environment

- What does the agent's shell execution environment look like?
- Can you inject environment variables (per session / per agent run / from plugin logic)?
- Where do commands execute? Same container? Can you run them in another k8s pod? What's the interface?
- Where does the agent store files? Can we sanely use a PVC?

### Asynchronous Execution / Tool Use

- Can the agent start a long-running call, be told it's running in background, take other actions meanwhile, and receive a wake-up/notification on finish?
- Is this only for shell execution, or more general (e.g., any tool call)?

### Extensibility

- Can you define custom tools?
- Can you unregister/register them dynamically, or disable/enable at runtime?
- Can your custom code/plugin deliver system notifications that either:
  - Get injected into agent context at next opportunity (if already running), or
  - Wake the agent up (if idle)?

### Basics

- Web UI that shows conversation(s)

### Nice to Have

- OAuth / Authentik integration

### Notes

Not expecting any single solution to have all of these. The subset that's available determines what's possible. Open to writing extension code but prefer minimal custom work.

## Candidates to Evaluate

### Tier 1: "Claw" Family (personal AI agent platforms)

These are all in the same direct lineage / competitive space as OpenClaw — self-hosted personal AI agents with messenger integrations and tool use.

- **OpenClaw** (current — see pros/cons above). TypeScript, 247k stars. The 800-pound gorilla. Telegram, Matrix (via `@openclaw/matrix` plugin, E2EE via Rust crypto SDK), WhatsApp, Slack, Discord, Signal, iMessage, etc. 5,700+ community skills via ClawHub. Native MCP via `@modelcontextprotocol/sdk` (stdio + SSE). Plugin API for runtime tool registration. Sub-agent architecture for background tasks. Had a critical RCE (CVE-2026-25253). Creator joining OpenAI, project moving to foundation.
- **ZeroClaw** — Rust, single 3.4MB binary, sub-10ms cold start, <5MB RAM. Telegram, Discord, Slack, WhatsApp, iMessage, Matrix, webhooks. Deny-by-default channel policies. Optional Docker sandboxing for shell. 22+ AI providers.
- **NanoClaw** — ~500 lines TypeScript. Built on Anthropic Agent SDK. Runs agents in Linux containers (Apple Container / Docker). Telegram, WhatsApp, Slack, Discord, Gmail. Agent swarms. "Fork and customize" philosophy — not a framework, more a template.
- **IronClaw** — Rust, by NEAR AI (Llion Jones, Transformer co-author). Capability-based security (seL4-inspired). Every skill runs in WASM sandbox. MCP support. Dynamic tool building. Encrypted credential vault. Traffic inspection on outbound. 890 skills. TEE deployment on NEAR AI Cloud. 11.8k stars.
- **PicoClaw** — Go, single binary, runs on $10 RISC-V boards. Telegram, Discord, QQ, DingTalk. Cron scheduling. MCP support (v0.1.4+). Multi-agent. 12k stars. Ultra-lightweight but limited channel support (no Matrix).
- **Nanobot** (HKUDS) — ~4k lines Python. Telegram (recommended), Discord, WhatsApp, Slack, Matrix (added 2026-02-25), Email, etc. MCP support (v0.1.4+). Cron via natural language. 17.8k stars. Memory via MEMORY.md files. Very lightweight (45MB, 0.8s startup).

### Tier 2: Workflow/orchestration platforms (not agent-first, but capable)

- **n8n** — Visual workflow automation, 177k stars. AI Agent nodes + Telegram/Slack/Discord triggers. MCP client support. Human-in-the-loop approval for tool calls (new in 2026). Not an "agent" per se but can build agent-like flows. Self-hosted. Has a "Manager-Executor" pattern community template ("Agent One") for autonomous agents via Telegram.
- **Kagent** — Kubernetes-native agent framework by Solo.io (CNCF sandbox). Built on AutoGen. MCP support + A2A. Focused on DevOps/cloud-native tasks (Argo, Helm, Istio, k8s, Prometheus). Not a personal assistant — more an infra agent. Already partially deployed in our cluster.

### Tier 3: Memory / complementary (not standalone agents)

- **memU** — Not an agent itself, but a memory framework for always-on agents. Knowledge graph from conversations. Could complement any of the above.

### Not considered (too low-level)

- OpenAI Agents SDK, Anthropic Agent SDK, LangGraph, CrewAI, AutoGen, Google ADK — these are SDKs/frameworks for building agents from scratch, not batteries-included platforms.
- Botpress, LangBot, Flowise, Dify — more chatbot/flow builders than autonomous agent platforms.

## Detailed Evaluation by Dimension

### Message Channels

| Framework | Telegram                                                     | Matrix                                                                                            | Other notable                                                           |
| --------- | ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| OpenClaw  | Yes (full, including groups)                                 | Yes (via `@openclaw/matrix` plugin, E2EE via Rust crypto SDK, long-polling, no public URL needed) | WhatsApp, Signal, Slack, Discord, iMessage, Teams, IRC, LINE, 20+ total |
| ZeroClaw  | Yes (allowlisted users, group reply modes, deny-by-default)  | Yes (sync-based, E2EE, no public endpoint needed)                                                 | WhatsApp, Slack, Discord, iMessage, webhooks                            |
| NanoClaw  | Via `/add-telegram` skill (not built-in, must be added)      | No (MicroClaw fork has it)                                                                        | WhatsApp (native), Slack, Discord, Gmail                                |
| IronClaw  | Yes                                                          | Not confirmed                                                                                     | Slack, HTTP webhooks, REPL, web gateway                                 |
| PicoClaw  | Yes (long-polling, voice transcription via Groq Whisper)     | No                                                                                                | Discord, QQ, DingTalk, LINE, WeCom                                      |
| Nanobot   | Yes (recommended channel, media groups, voice transcription) | Yes (added 2026-02-25, E2EE, typing indicators)                                                   | Discord, WhatsApp, Slack, Feishu, Email, 8+ total                       |
| n8n       | Yes (trigger node, but one webhook per bot limit)            | No built-in node (community nodes exist)                                                          | Slack, Discord, email, SMS, 400+ service triggers                       |

### Triggers

| Framework | Cron                                                                                                                                     | Webhooks                                                                         | Other                                               |
| --------- | ---------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- | --------------------------------------------------- |
| OpenClaw  | Yes — persistent jobs in `~/.openclaw/cron/`, at/interval/cron expressions, wakeMode "now" or "next-heartbeat", isolated or main session | Yes — `/hooks/wake` endpoint, Bearer or `x-openclaw-token` auth, SSRF-protected  | Heartbeat (default 30min)                           |
| ZeroClaw  | Yes — cron expressions, RFC3339 timestamps, fixed intervals, one-shot delays                                                             | Yes — HTTP/WebSocket gateway on `127.0.0.1:42617`, pairing-code auth             | Daemon auto-restart with exponential backoff        |
| NanoClaw  | Yes — `task-scheduler.ts` for cron, interval, one-shot                                                                                   | Host orchestrator handles IPC                                                    | Container-per-group isolation                       |
| IronClaw  | Yes — Routines Engine, cron schedules, event triggers, webhook handlers, AI-mediated (agent reasons about trigger)                       | Yes — HTTP webhook channel                                                       | Routines run without active user session            |
| PicoClaw  | Yes — one-time, recurring, cron expressions, jobs execute in agent's security context                                                    | Single shared gateway HTTP server (`127.0.0.1:18790`) for webhook-based channels | Heartbeat via HEARTBEAT.md                          |
| Nanobot   | Yes — natural language cron, built-in cron tool                                                                                          | Gateway mode serves channels                                                     | Subagent spawning                                   |
| n8n       | Yes — Schedule trigger node, cron expressions                                                                                            | Yes — first-class Webhook trigger node                                           | Any of 400+ service triggers (GitHub, Stripe, etc.) |

### MCP Support

| Framework | Native MCP?                      | Details                                                                                                                                                                                                                                     |
| --------- | -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| OpenClaw  | **Yes** — native since late 2025 | Uses `@modelcontextprotocol/sdk`. Configure MCP servers in `openclaw.json` (or `.mcp.json`) with `command`, `args`, `env`. Supports stdio and SSE transport. Open requests for better remote HTTP-based MCP server support (#8188, #29053). |
| ZeroClaw  | **Unclear**                      | Docs mention trait-based extensibility for tools. MCP not explicitly documented as a supported protocol. Tool system is based on Rust traits, not MCP.                                                                                      |
| NanoClaw  | **Partial**                      | Docs mention MCP can extend functionality. Built on Anthropic Agent SDK which has MCP helpers. Not a primary integration path — skills are the main extensibility.                                                                          |
| IronClaw  | **Yes** — first-class            | `ironclaw tool install` supports both WASM tools (sandboxed) and MCP servers (any language, no sandbox). Both are first-class. Can also dynamically build tools from natural language descriptions.                                         |
| PicoClaw  | **Planned** — on roadmap         | MCP client implementation, dynamic tool discovery, and secure execution are planned but not yet shipped as of latest search results.                                                                                                        |
| Nanobot   | **Yes** — since v0.1.4           | Native MCP client. Configure MCP servers in config, tools auto-discovered at runtime.                                                                                                                                                       |
| n8n       | **Yes** — native MCP client node | MCP Client tool node connects to any MCP server. Can also expose n8n workflows as MCP servers for other clients.                                                                                                                            |

### Execution Environment

| Framework | Shell model                                                                                 | Env var injection                                                                                                                                                                                                                                                      | Where commands run                                                                                                                                                   | File storage                                                                                                                           |
| --------- | ------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| OpenClaw  | `sh -lc` (login shell)                                                                      | Global via `~/.openclaw/.env`, per-sandbox via `agents.defaults.sandbox.docker.env` (known bug #17923: injected at `docker exec` not `docker create`), `${VAR}` interpolation, `SecretRef` for external secrets. `OPENCLAW_HOME` overrides `$HOME` for full isolation. | Same host (unsandboxed) or ephemeral Docker container (sandboxed, no network by default, read-only root, 1GB mem limit, non-root user). No native k8s pod execution. | `~/.openclaw/` for config/memory, `~/openclaw/workspace/` for agent workspace. In Docker: `/data` PV with `.openclaw/` + `workspace/`. |
| ZeroClaw  | Workspace-scoped, command allowlist (only explicitly allowed commands like git, npm, cargo) | Config-based, encrypted secrets at rest                                                                                                                                                                                                                                | Same process, sandboxed to workspace by default. Optional Docker.                                                                                                    | `~/.zeroclaw/workspace/`, state in `~/.zeroclaw/state/`                                                                                |
| NanoClaw  | Full shell inside container (Claude Code runs in container)                                 | Via container environment                                                                                                                                                                                                                                              | Isolated Linux container per group (Apple Container on macOS, Docker on Linux). Host orchestrator manages lifecycle.                                                 | Each group gets own mounted directory inside container                                                                                 |
| IronClaw  | WASM sandbox for untrusted tools; credential injection at host boundary                     | Credentials injected at execution boundary, never in LLM context                                                                                                                                                                                                       | WASM sandbox for tools. MCP servers run as separate processes. PostgreSQL for persistent state.                                                                      | PostgreSQL with AES-256-GCM encrypted secrets                                                                                          |
| PicoClaw  | Sandboxed to workspace by default, `deny_patterns`/`allow_patterns` for command filtering   | Via config                                                                                                                                                                                                                                                             | Same process, workspace-scoped                                                                                                                                       | `~/.picoclaw/workspace/` (sessions, memory, state, cron, skills)                                                                       |
| Nanobot   | Built-in ExecTool with `restrictToWorkspace` flag, `deny_patterns`/`allow_patterns`         | Via config                                                                                                                                                                                                                                                             | Same process, workspace-scoped. 20-iteration limit on agent loop.                                                                                                    | `~/.nanobot/workspace/`, memory as `MEMORY.md` + daily notes                                                                           |
| n8n       | Execute Command node, Code node (JS/Python)                                                 | Via workflow variables, environment variables                                                                                                                                                                                                                          | n8n worker process. Queue mode for async workers. Can shell out or call APIs.                                                                                        | n8n database (SQLite/PostgreSQL), binary data in filesystem                                                                            |

### Asynchronous Execution / Tool Use

| Framework | Async/background support                                                                                                                                                                                                                                                                                                                                                             | Scope                                                                 | Wake-up mechanism                                                                                                                                                              |
| --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| OpenClaw  | **Yes** — sub-agents for background/parallel work. `async-task start` returns immediately. Wake triggers can notify on completion. Lane-based command queue processes ops serially within a session, but multiple sessions/sub-agents run concurrently. **However**: system notifications still delivered on next heartbeat only. No way for plugins to trigger immediate heartbeat. | General — sub-agents can run any tools. Multiple concurrent sessions. | Sub-agent completion can use wake triggers. But system notifications (e.g., approval results) still wait for heartbeat (30min default). **Approval workflow blocker remains.** |
| ZeroClaw  | Tokio async runtime, each subsystem in own task                                                                                                                                                                                                                                                                                                                                      | All channel/tool operations are async                                 | Subsystem restart with exponential backoff. Event-driven via async channels.                                                                                                   |
| NanoClaw  | Container-per-group provides isolation. Host orchestrator manages lifecycle.                                                                                                                                                                                                                                                                                                         | Per-container                                                         | IPC between host and container                                                                                                                                                 |
| IronClaw  | **Yes** — parallel jobs in isolated contexts, priority-aware scheduler. Self-repair for stuck operations.                                                                                                                                                                                                                                                                            | General (any tool/routine)                                            | Automatic detection and recovery of stuck operations                                                                                                                           |
| PicoClaw  | **Yes** — subagent spawning for long tasks. Heartbeat reads HEARTBEAT.md, spawns subagent that works independently.                                                                                                                                                                                                                                                                  | Subagent has access to all tools, can message user independently      | Subagent communicates back via message tool                                                                                                                                    |
| Nanobot   | **Yes** — `SpawnTool` creates background `asyncio.Task`. Subagents are fully isolated (own memory, own tool registry minus spawn/message).                                                                                                                                                                                                                                           | General — any background task via subagent                            | MessageBus announces result to user on completion                                                                                                                              |
| n8n       | **Yes** — queue mode with worker nodes. Workflows can wait for external events (webhook callbacks, manual approval).                                                                                                                                                                                                                                                                 | General — any workflow step                                           | Webhook callback, manual approval node, polling                                                                                                                                |

### Extensibility

| Framework | Custom tools                                                                                                                                                                                                                                   | Dynamic registration                                                                                  | System notifications / wake-up                                                                                                                                                                                                        |
| --------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| OpenClaw  | Yes — Skills (markdown, 5,700+ on ClawHub) + Plugins (TS/JS, run in Gateway). Plugins can `api.registerTool()`, `api.registerProvider()`, `api.registerHttpHandler()`, add CLI commands. Tool allow/deny lists via `tools.allow`/`tools.deny`. | Plugins can register tools at runtime via `api.registerTool()`. Skills loaded at startup.             | Webhooks + `message` tool for proactive push. Plugins can register webhook handlers. **But**: system notifications for approval workflow still delivered on next heartbeat only. Cannot trigger immediate heartbeat from plugin code. |
| ZeroClaw  | Yes — Skill packs via TOML manifests, tool allowlisting. Security audit on install.                                                                                                                                                            | Config-based, hot-reloadable config (zero-downtime updates)                                           | Pushover notifications. Event-driven internal architecture (tokio channels).                                                                                                                                                          |
| NanoClaw  | Yes — Skills as Claude Code commands (e.g., `/add-telegram`). Fork-and-customize model.                                                                                                                                                        | Not dynamic — you modify the codebase                                                                 | Host-to-container IPC                                                                                                                                                                                                                 |
| IronClaw  | Yes — WASM tools (sandboxed) + MCP servers + dynamic tool building from natural language                                                                                                                                                       | Tools installable at runtime via `ironclaw tool install`. Routines Engine for event-driven execution. | Routines Engine handles event triggers. Priority-aware scheduler.                                                                                                                                                                     |
| PicoClaw  | Yes — Skills in `skills/` directory, tool descriptions in `TOOLS.md`                                                                                                                                                                           | Loaded from workspace                                                                                 | Heartbeat mechanism via HEARTBEAT.md                                                                                                                                                                                                  |
| Nanobot   | Yes — Built-in tools, Skills, MCP servers. Plan: core vs `pip install nanobot-xxx` extensions.                                                                                                                                                 | MCP tools auto-discovered. Skills loaded from workspace.                                              | MessageBus for internal routing. Subagent completion announcements.                                                                                                                                                                   |
| n8n       | Yes — any n8n node can be a tool (400+ integrations). Custom code nodes (JS/Python). MCP client. Sub-workflows as tools.                                                                                                                       | Workflows are the unit of composition — enable/disable at will.                                       | Human-in-the-loop approval node. Webhook callbacks. Chat triggers.                                                                                                                                                                    |

### Web UI

| Framework | Web UI      | Details                                                                                                                                                                               |
| --------- | ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| OpenClaw  | **Yes**     | Vite + Lit SPA at `:18789/`. Chat, sessions, config, nodes, logs, skills, canvas (agent-driven dashboards). Device pairing approval. Community dashboards (cost tracking, cron mgmt). |
| ZeroClaw  | **Minimal** | Gateway serves browser-based chat alongside webhook integrations. Not a full management UI.                                                                                           |
| NanoClaw  | **No**      | CLI + messenger-based interaction only.                                                                                                                                               |
| IronClaw  | **Yes**     | Web gateway with SSE/WebSocket streaming. Browser-based chat UI.                                                                                                                      |
| PicoClaw  | **No**      | CLI + messenger-based interaction only.                                                                                                                                               |
| Nanobot   | **Yes**     | Socket.IO WebSocket web UI with HTTP polling fallback. Gateway mode.                                                                                                                  |
| n8n       | **Yes**     | Full workflow editor, execution history, credential management, AI agent chat interface. Most polished UI of all candidates.                                                          |

### OAuth / Authentik

| Framework | OAuth/OIDC support                                                                                                                                                                                              |
| --------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| OpenClaw  | No native OIDC for gateway auth (open request #4642). Workaround: trusted proxy auth (nginx + oauth2-proxy, Traefik + forward auth, Caddy, Pomerium). OAuth for model providers (token refresh, multi-account). |
| ZeroClaw  | Composio integration for 1000+ OAuth app integrations. Auth profiles stored encrypted. No OIDC for platform auth.                                                                                               |
| NanoClaw  | No                                                                                                                                                                                                              |
| IronClaw  | Encrypted credential vault. No documented OIDC/Authentik integration.                                                                                                                                           |
| PicoClaw  | No                                                                                                                                                                                                              |
| Nanobot   | No                                                                                                                                                                                                              |
| n8n       | **Yes** — native OIDC/SAML SSO (but requires Startup license at $400/mo). Free alternative: `n8n-oidc` plugin works with Authentik, Keycloak, etc. via env vars.                                                |

## Summary / Shortlist

**For our use case** (Telegram + Matrix, MCP tools, approval workflow with wake-up, k8s deployment, Authentik):

- **IronClaw** — strongest on MCP (native, first-class), security (WASM sandbox), and async (parallel jobs, self-repair, routines engine with event triggers). Weakest on Matrix (unconfirmed). Routines Engine could solve the approval wake-up problem.
- **Nanobot** — has both Telegram and Matrix, native MCP, async via subagents, very lightweight. Weakest on security model (workspace sandboxing only). Python codebase easiest to extend for our needs.
- **ZeroClaw** — has both Telegram and Matrix (with E2EE), hot-reloadable config, event-driven async. MCP support unclear. Rust codebase harder to extend.
- **n8n** — best UI, native MCP, human-in-the-loop approval (exactly what we need!), Authentik via `n8n-oidc`. But it's a workflow engine, not an autonomous agent — building an agent-like experience requires significant workflow design. No native Matrix.
- **OpenClaw** — broadest channel support, native MCP, rich plugin API, sub-agents for background work. But the heartbeat-only system notification delivery remains a hard blocker for approval workflows specifically. Gateway auth is token-only (OIDC via reverse proxy workaround).
