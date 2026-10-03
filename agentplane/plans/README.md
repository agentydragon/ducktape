# Agentplane plans

Agentplane is running on staging. Implemented contracts live beside the app, runner, egress proxy,
Action Service, LLM ingress, acceptance suite, and durable evidence under [`../docs/`](../docs/).
This directory contains current design gates, genuinely deferred decisions, and north-star context;
the [task DAG](task_dag.md) is authoritative for status and dependencies.

**Dependency rule:** the integration app is a user-facing client. Other services must not depend on
its APIs, private tables, implementation, process, or bootstrap, including in v1. The accepted
[service boundary constraint](../docs/service_boundaries.md) and [Sandbox Service extraction](sandbox_service.md)
set the direction: app → independent backends; notifications → Sandbox Service/Action Service.

Transcript search/lookup (`T3`) is deliberately deferred product work and is not in the current
execution sequence. This is a priority decision, not a technical dependency.

## Open plans and gates

- [Thread sync](thread_sync/README.md) — what is still open on the deployed Electric design (eviction, pending-command paging, body compaction, measurement), and the seams and candidates for a second implementation
- [Task DAG](task_dag.md) — remaining work and proposed priorities, including unranked future
  harness-capability candidates, deployed acceptance, UI/history, and native recovery
- [Durable SSH-backed processes](ssh_durable_processes.md) — deferred systemd-backed host daemon for processes that outlive an SSH connection
- [Driver-provided tools and background work](driver_tools_and_background.md) — deferred seam that must reuse the Action contracts
- [Agent access to external systems](external_access.md) — deferred delegated-versus-brokered access choices
- [Profiles](profiles.md) — broader capability profiles remain deferred
- [KubeVirt execution environments](kubevirt_environments.md) — selectable VM environments, guest runner,
  launcher proxy, resource isolation, persistent state and implementation gates
- [User stories](user_stories.md) — north-star product context, not an implementation queue
- [Sandbox Service](sandbox_service.md) — extract independent sandbox lifecycle/session access before
  notification v1; [discovery/access notes](runner_discovery.md) retain network-policy access and the runner-auth TODO
- [Subscriptions and notifications](notifications.md) — standalone service design: SA-authorized session scope,
  explicit inbox acknowledgement, Action-only v1, and runner delivery; later automatic following and wake
- [Push mechanism](push_mechanism.md) — remaining push/subscription design for
  `NO_MANUAL_REFRESH`'s Settings tabs; the Actions attention drawer shipped in PR #8618

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
