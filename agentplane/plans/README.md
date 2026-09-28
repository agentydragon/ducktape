# Agentplane plans

Agentplane is running on staging. Implemented contracts live beside the app, runner, egress proxy,
Action Service, LLM ingress, acceptance suite, and durable evidence under [`../docs/`](../docs/).
This directory contains current design gates, genuinely deferred decisions, and north-star context;
the [task DAG](task_dag.md) is authoritative for status and dependencies.

Transcript search/lookup (`T3`) is deliberately deferred product work and is not in the current
execution sequence. This is a priority decision, not a technical dependency.

## Open plans and gates

- [Thread sync](thread_sync/README.md) — what is still open on the deployed Electric design (eviction, pending-command paging, body compaction, measurement), and the seams and candidates for a second implementation
- [Task DAG](task_dag.md) — remaining work and proposed priorities: deployed command acceptance, UI usability/history, native recovery, and deferred cluster-browser/subagent work
- [Durable SSH-backed processes](ssh_durable_processes.md) — deferred systemd-backed host daemon for processes that outlive an SSH connection
- [Driver-provided tools and background work](driver_tools_and_background.md) — deferred seam that must reuse the Action contracts
- [Agent access to external systems](external_access.md) — deferred delegated-versus-brokered access choices
- [Profiles](profiles.md) — broader capability profiles remain deferred
- [User stories](user_stories.md) — north-star product context, not an implementation queue
- [Push mechanism](push_mechanism.md) — shared design for `UISHELL_DRAWER`'s badge and
  `NO_MANUAL_REFRESH`'s Settings tabs, not yet confirmed

## Implemented contracts

Use the [Action Service specification](../action_service/SPEC.md) and
[README](../action_service/README.md) for catalog, Decisions, events, bounded waits, cancellation,
generic MCP, configured Identities, Connection grants, and OAuth/consent. The
[app README](../app/README.md) owns the consent and Action-review presentation contracts.
[Executor liveness](../docs/executor_liveness.md), [operator federation](../docs/operator_federation.md),
[workload authentication](../docs/workload_authentication.md),
[launch presets](../docs/launch_presets.md), and [action policies](../docs/action_policies.md) own
the other implemented contracts.
[Sandbox Actions](../action_service/sandbox/README.md) owns the exec-target contract for agents hosted
outside the cluster: sandboxes that run as the calling ServiceAccount, and Kubernetes reach from
inside one.
[Thread, runner, and harness layering](../docs/thread_layering.md) owns the command/Event
architecture, pending Thread identity continuity, native recovery, and optional
app-first acceptance. [Thread view synchronization](../docs/thread_view_sync.md)
owns the proposed conversation model and sync-engine integration, on-demand Raw, history/catch-up and
frontend state; it is distinct from the implemented raw-replay API.
Deployed acceptance is tracked in the DAG; code or CI evidence alone does not satisfy it.
