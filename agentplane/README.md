# Agentplane

Agentplane runs coding agents in Kubernetes sandboxes and mediates what they do outside them:
network egress with credentials substituted at the proxy, operator-approved or policy-approved
Actions, and notifications. Its services are separately deployable; the Action Service alone is
an MCP approval gateway for any MCP-capable LLM product.

**Design principles:** [`docs/principles.md`](docs/principles.md). Check new designs against it.

## Services

- [Action Service](action_service/README.md): ActionRequests, Decisions, auto-approval policies,
  and an MCP endpoint over OAuth.
- [Sandbox Service](sandbox_service/README.md): sandbox lifecycle and access to runner sessions.
- [Runner](runner/README.md) and [native drivers](native/README.md): run Claude Code or Codex in a
  sandbox and journal what they do.
- [Egress proxy](egress/README.md): authenticates sandbox workloads and substitutes credentials.
- [LLM ingress](llm_ingress/README.md): authenticated hop in front of LiteLLM.
- [Notification service](notification_service/README.md): subscriptions and session inboxes.
- [Indexing](indexing/README.md): Git semantic search.
- [Integration app](app/README.md): the human-facing frontend over the services above, and a
  client of them.

Status, open work and design gates: [`plans/`](plans/README.md). Implemented contracts:
[`docs/`](docs/).
