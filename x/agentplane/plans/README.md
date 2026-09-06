# Agentplane plans

Agentplane is running on staging. Implemented contracts live beside the app, runner, egress proxy,
Action Service, LLM ingress, acceptance suite, and durable evidence under [`../docs/`](../docs/).
This directory contains current design gates, genuinely deferred decisions, and north-star context;
the [task DAG](task_dag.md) is authoritative for status and dependencies.

## Open plans and gates

- [Task DAG](task_dag.md) — authoritative landed/open dependency map
- [Operations and access](operations_and_access.md) — Action schema, Executor wiring, and delivery gates
- [Asynchronous approvals](async_approvals.md) — unresolved pending/result delivery and notification path
- [Driver-provided tools and background work](driver_tools_and_background.md) — deferred seam that must reuse the Action contracts
- [Agent access to external systems](external_access.md) — deferred delegated-versus-brokered access choices
- [BuildBuddy hosted remote-run authentication](buildbuddy_remote_auth.md) — unresolved hosted-run credential boundary
- [Profiles](profiles.md) — broader capability profiles remain deferred
- [User stories](user_stories.md) — north-star product context, not an implementation queue

## Landed evidence and completed slices

- Workload-token substitution foundation — PR
  [#5685](https://github.com/agentydragon/ducktape/pull/5685), summarized in
  [workload authentication](../docs/workload_authentication.md)
- Shared `SandboxPrincipal` — PR
  [#5696](https://github.com/agentydragon/ducktape/pull/5696) and
  [`../sandbox_auth/README.md`](../sandbox_auth/README.md)
- Authenticated LLM ingress — PR
  [#5698](https://github.com/agentydragon/ducktape/pull/5698) and
  [`../llm_ingress/README.md`](../llm_ingress/README.md)
- Standalone Action Service with human Decision path and fixture-only echo executor — PR
  [#5700](https://github.com/agentydragon/ducktape/pull/5700) and
  [`../action_service/README.md`](../action_service/README.md)
- Launch presets first slice — PR
  [#5648](https://github.com/agentydragon/ducktape/pull/5648) and
  [launch-preset evidence](../docs/launch_presets.md)
- BuildBuddy local HTTP/gRPC substitution — PR
  [#5650](https://github.com/agentydragon/ducktape/pull/5650) and the
  [egress specification](../egress/SPEC.md); hosted `bb remote` remains open
- Credentialless Sandbox egress — [accepted ADR](../docs/adr_sandbox_proxy_gateway.md)
- Agent-facing egress rules API boundary — draft PR
  [#5701](https://github.com/agentydragon/ducktape/pull/5701), still in review
- Claude/Codex provider and harness evidence —
  [protocols](../docs/provider_protocols.md), [input queue](../docs/claude_input_queue.md), and
  [native harness evidence](../docs/harness_evidence.md)
- A2A suitability — [evaluated and not adopted](../docs/a2a.md)
