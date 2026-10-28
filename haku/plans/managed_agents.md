# Haku on Anthropic Managed Agents

Runtime B remains an experimental alternative to Haku's current web routine.
The self-hosted prototype and its parked deployment are documented in
<../runtime/x/managed_agent/self_hosted/README.md>; adopting it is undecided.
The runtime comparison is in <runtime_options.md>.

## Warm-session design

A supervisor would keep one live managed-agent session and send a `user.message`
for each scheduled, webhook, or manual wake. Wakes received during a run should
queue in order. `haku-state` remains the durable record; a new session must be
able to reorient from it after termination or compaction.

The supervisor would hold the control-plane API key separately from the worker's
environment key. It needs lossless stream reconnection using history and event
deduplication, a guard against post-idle status races, terminal-session recovery,
and authenticated webhook delivery. Its timer replaces the prototype's scheduled
deployment, which starts a fresh session per tick.

## Before adopting Runtime B

- Decide whether warm sessions' context reuse justifies a supervisor, and verify
  idle billing and worker workspace persistence across wakes.
- Follow the prototype's deployment reactivation conditions, including a fresh
  vault and credentials, an image publication path, and a worker smoke test.
- Choose which MCP sources can use a public gated endpoint with vault-backed
  credentials and which stay in-cluster and are reached from the worker. The
  former haku-console `/mcp` endpoint is retired and needs a replacement.
  Vault-backed MCP calls run on Anthropic's side and require public reachability;
  verify self-hosted vault credential support and keep worker-side secrets within
  the worker perimeter.
- Decide whether Anthropic Console traces and workspace limits provide sufficient
  attribution and control, or whether to mirror transcripts into `haku-traces`.
- Validate the SDK and API surface used by the chosen design against the pinned
  versions before implementing it.
