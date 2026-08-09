# Agent SDK loop in a Haku sandbox, driven from Haku Console

Status: **deployed feasibility and transport record.** The active roadmap is
[claude_sandbox_haku_runtime.md](claude_sandbox_haku_runtime.md).

This document records the decisions that established the conversational foundation. It is not a
second implementation checklist.

## Deployed shape

```text
browser ── Haku Console chat/API/SSE ── trusted ClaudeAgentSDKClient
                         │                         │
                         │ provision/cleanup       │ stream-JSON WebSocket
                         ▼                         ▼
               Agent Sandbox CR/Pod ─────── thin Claude Code runner
                                                   │
                                                   ▼
                                      Claude iron-proxy ── Anthropic

Claude Code ── streamable HTTP MCP ── Haku Console /mcp ── policy/approval/providers
```

- The Python Agent SDK and `ClaudeSDKClient` run in trusted Haku Console.
- A dedicated sandbox runner imports no SDK code. It starts the pinned Claude CLI and bridges the
  CLI's native stream-JSON stdin/stdout protocol over WebSocket.
- One conversation is pinned 1:1 to one sandbox and one CLI process until disposal.
- Built-in tools execute inside the sandbox. Console MCP calls return through the standard static
  Haku Agent, deployment-owned policy, and operator approval path.
- The runner sees only a placeholder Claude bearer; the Claude-specific iron-proxy substitutes the
  real subscription credential for exact Anthropic destinations.
- Console persists the operator-facing transcript projection. Claude's local JSONL remains
  ephemeral until the successor roadmap chooses a retention/resume design.

## Why this architecture was selected

The Agent SDK's control protocol already supplies the required orchestration:

- `ClaudeSDKClient` provides stateful multi-turn queries and interruption;
- an abstract `Transport` carries stream-JSON without requiring the CLI to share a process or
  filesystem with the SDK;
- in-process Python hooks run in trusted Console before and after sandbox tool execution;
- SDK-hosted MCP is possible, although this runtime deliberately points Claude back to the standard
  Haku Console MCP server;
- session IDs and the CLI JSONL permit resume when the same pinned working directory and transcript
  survive.

The custom transport therefore adds only launch and lifecycle framing. It does not define another
prompt, turn, tool, or approval protocol.

## Fixed tool and trust decisions

- `allowed_tools` is an auto-approval list, not a visibility boundary. `disallowed_tools` removes
  definitions, and `permission_mode="dontAsk"` denies unmatched requests.
- `PreToolUse` is the reliable programmable gate because it runs before tool execution. Hooks may
  inspect, deny, or rewrite tool input, but deployment policy remains authoritative.
- Claude receives no approval-decision tool. Approval records and decisions are Console-owned.
- The sandbox receives no Kubernetes service-account token, downstream provider credential, or
  general Haku runtime authority.
- Raw provider events may be retained for debugging, but operator-facing persistence must remain
  bounded and sanitized.

## Compatibility evidence — 2026-07-31

The one-shot `haku-agent-sdk-smoke` Job completed successfully against source commit `da578377` using
Agent SDK 0.1.48 and bundled Claude CLI 2.1.71. It proved the architecture-blocking assumptions:

- a `claude setup-token` OAuth token authenticates headlessly for an individual Agent SDK runtime;
- mediated proxy/TLS egress supports real inference without direct egress exceptions;
- partial streaming and successful terminal results work through `ClaudeSDKClient`;
- a second turn on the same client retains conversational state and session ID;
- a new client can resume from the CLI JSONL when the working directory is pinned;
- `UserPromptSubmit` and `Stop` Python hooks execute in the SDK host;
- the pod remained non-root, capability-dropped, service-account-token-free, and emptyDir-backed;
- no credential appeared in the inspected structured logs or transcript excerpt.

The production runtime subsequently landed the remote transport, dedicated namespace/proxy,
sandbox lifecycle, Console MCP connection, message projection, Markdown chat, SSE updates,
`LISTEN`/`NOTIFY` dispatch, and interruption support.

## Remaining caveats

These belong to the successor roadmap rather than this feasibility record:

- transcript survival across pod loss and reliable reconnect after Console rollout;
- durable `haku-state` checkout and Git concurrency policy;
- reviewed Haku orientation and lifecycle hooks;
- complete tool-call lifecycle projection and origin audit;
- explicit OTel arrival verification and long-term token rotation/canary operations.

## Historical alternatives retired

- Running the Agent SDK inside the sandbox would move hooks and orchestration across the trust
  boundary for no benefit.
- Reusing the general `haku-sandbox` namespace/proxy would couple unrelated authority and egress.
- Exposing provider MCP servers directly from the runner would bypass Console policy and approval.
- Pointing a sandbox-side `SessionStore` at Console Postgres would require database credentials and
  perimeter egress; Console-side extraction or an explicitly disposable transcript is safer.
