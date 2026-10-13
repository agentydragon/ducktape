# Agentplane plans

Agentplane is running on staging. Implemented contracts live beside the app, runner, egress proxy,
Action Service, LLM ingress, acceptance suite, and durable evidence under [`../docs/`](../docs/).
This directory contains current design gates, genuinely deferred decisions, and north-star context;
the [task DAG](task_dag.md) is authoritative for status and dependencies.
[Maintenance conventions](../AGENTS.md#task-dag-maintenance) distinguish dispatchable phases,
operator decisions and proportional acceptance from the [low-priority freezer](task_freezer.md).

**Dependency rule:** the integration app is a user-facing client. Other services must not depend on
its APIs, private tables, implementation, process, or bootstrap, including in v1. The accepted
[service boundary constraint](../docs/service_boundaries.md) and the [Sandbox Service](../sandbox_service/README.md)
set the direction: app → independent backends; notifications → Sandbox Service, Runner Gateway,
History Service and Action Service.

Transcript search/lookup (`T3`) is deliberately deferred product work and is not in the current
execution sequence. This is a priority decision, not a technical dependency.

## Open plans and gates

- [Native session discovery](native_session_discovery.md) — remaining harness characterization and
  deferred shared-session design with identity, attribution and durability gates
- [Thread sync](thread_sync/README.md) — what is still open on the deployed Electric design (pending-command paging, body compaction, measurement), and the seams and candidates for a second implementation
- [Task DAG](task_dag.md) — remaining work and proposed priorities, including unranked future
  harness-capability candidates, deployed acceptance, UI/history, and native recovery
- [Durable SSH-backed processes](ssh_durable_processes.md) — deferred systemd-backed host daemon for processes that outlive an SSH connection
- [Driver-provided tools and background work](driver_tools_and_background.md) — deferred seam that must reuse the Action contracts
- [Claude RemoteIO transport](claude_remote_io.md) — evaluate an optional `--sdk-url` bridge through Agentplane egress, including compaction-summary value and recovery gates
- [Agent access to external systems](external_access.md) — deferred delegated-versus-brokered access choices
- [Scoped Session reads](scoped_session_reads.md) — proposed ServiceAccount read policy for
  retained Session history
- [Kubernetes grants through PolicySets](kubernetes_rbac_groups.md) — runtime grants, accounts without a
  Sandbox, dropping the old policy kinds, and the verification still owed on a deployed environment
- [KubeVirt execution environments](kubevirt_environments.md) — the live gates for VM Sandboxes that no
  DAG node names; the design is [VM Sandboxes](../docs/vm_sandboxes.md)
- [User stories](user_stories.md) — north-star product context, not an implementation queue
- [Input metadata](notification_presentation.md) — a sequenced follow-up to command admission
- [History Service](history_service.md) — the target split (runner, Sandbox Service, Runner
  Gateway, History Service, app facade) and the invariants the refactor converges on
- [Subscriptions and notifications](notifications.md) — standalone service design: SA-authorized session scope,
  explicit inbox acknowledgement, shipped Actions/GitHub delivery, remaining reliability acceptance,
  and deferred Kubernetes monitoring, automatic following and wake

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
architecture, Thread identity continuity, native recovery, and optional
app-first acceptance. [Thread view synchronization](../docs/thread_view_sync.md)
owns the conversation model and sync-engine integration, on-demand Raw, history/catch-up and
frontend state; it is distinct from the implemented raw-replay API.
Require bounded deployed acceptance only for an identified deployment-specific uncertainty; existing
automated evidence suffices for covered behavior. Do not reopen waived or operator-accepted checks.
