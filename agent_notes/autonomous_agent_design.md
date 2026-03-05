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

- No native MCP support — they have an MCP skill but it involves running `mcporter` utility in their exec environment
- Approval workflow doesn't fit well:
  - Need to be able to resume the agent with a notification when approvals are resolved (granted or denied)
  - OpenClaw has wiring for system notifications, but they only get delivered on next heartbeat
  - Our plugin cannot trigger an OpenClaw heartbeat — that's not part of the plugin API
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

- **OpenClaw** (current — see pros/cons above). TypeScript, 247k stars. The 800-pound gorilla. Telegram, Matrix, WhatsApp, Slack, Discord, Signal, iMessage, etc. 700+ community skills via ClawHub. No native MCP (uses `mcporter` workaround). Had a critical RCE (CVE-2026-25253). Creator joining OpenAI, project moving to foundation.
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
