# Agentplane plans

Agentplane is running on staging. Implemented contracts live beside the app, runner, egress proxy,
Action Service, LLM ingress, acceptance suite, and durable evidence under [`../docs/`](../docs/).
This directory contains current design gates, genuinely deferred decisions, and north-star context;
the [task DAG](task_dag.md) is authoritative for status and dependencies.

Transcript search/lookup (`T3`) is deliberately deferred product work and is not in the current
execution sequence. This is a priority decision, not a technical dependency.

## Open plans and gates

- [Task DAG](task_dag.md) — remaining-work dependency map, including `INPUT_DELIVERY`: re-read native queue research and refresh captures before changing the common protocol
- [Durable SSH-backed processes](ssh_durable_processes.md) — deferred systemd-backed host daemon for processes that outlive an SSH connection
- [External MCP connections](external_mcp_connections.md) — client compatibility, enrollment retention, and Haku migration
- [Driver-provided tools and background work](driver_tools_and_background.md) — deferred seam that must reuse the Action contracts
- [Agent access to external systems](external_access.md) — deferred delegated-versus-brokered access choices
- [Profiles](profiles.md) — broader capability profiles remain deferred
- [User stories](user_stories.md) — north-star product context, not an implementation queue

## Implemented contracts

Use the [Action Service specification](../action_service/SPEC.md) and
[README](../action_service/README.md) for catalog, Decisions, events, bounded waits, cancellation,
generic MCP, configured Identities, Connection grants, and OAuth/consent. The
[app README](../app/README.md) owns the consent and Action-review presentation contracts.
[Executor liveness](../docs/executor_liveness.md), [operator federation](../docs/operator_federation.md),
[workload authentication](../docs/workload_authentication.md),
[launch presets](../docs/launch_presets.md), and [action policies](../docs/action_policies.md) own
the other implemented contracts.
Deployed acceptance is tracked in the DAG; code or CI evidence alone does not satisfy it.
