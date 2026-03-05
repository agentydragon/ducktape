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

- OpenClaw (current — see pros/cons above)
- ZeroClaw
- (others TBD)
