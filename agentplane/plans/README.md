# Agentplane plans

Agentplane is running on staging. Implemented contracts live beside the app, runner, egress proxy,
Action Service, LLM ingress, acceptance suite, and durable evidence under [`../docs/`](../docs/).
This directory contains current design gates, genuinely deferred decisions, and north-star context;
the [task DAG](task_dag.md) is authoritative for status and dependencies.
[Maintenance conventions](../AGENTS.md#task-dag-maintenance) distinguish dispatchable phases,
operator decisions and proportional acceptance from the [low-priority freezer](task_freezer.md).

## Open plans and gates

- [Native session discovery](native_session_discovery.md) — remaining harness characterization and
  deferred shared-session design with identity, attribution and durability gates
- [Thread sync](thread_sync/README.md) — what is still open on the deployed Electric design (pending-command paging, body compaction, measurement), and the seams and candidates for a second implementation
- [Task DAG](task_dag.md) — remaining work and proposed priorities, including unranked future
  harness-capability candidates, deployed acceptance, UI/history, and native recovery
- [Driver-provided tools and background work](driver_tools_and_background.md) — deferred seam that must reuse the Action contracts
- [Claude RemoteIO transport](claude_remote_io.md) — evaluate an optional `--sdk-url` bridge through Agentplane egress, including compaction-summary value and recovery gates
- [Agent access to external systems](external_access.md) — deferred delegated-versus-brokered access
  choices and broader authorization-system evaluation, separate from multiagent implementation
- [Cross-agent communication and authorization](multiagent_communication.md) — agreed SA attribution,
  Sandbox scopes, explicit collections, live bundles and Session inboxes; shared inbox/messaging
  service with subscriptions and batched notices; tentative service-enforced PolicySet rules,
  remaining schema/API decisions and deferred agent lifecycle delegation
- [Agent CLI](agent_cli.md) — proposed `agentplane` command, JSON and exit-code contract for
  Actions, notifications and egress rules, with one open exit-code question
- [Kubernetes API aggregation](api_aggregation_exploration.md) — held design exploration of serving
  agent-facing records through an extension apiserver, with RBAC via `SubjectAccessReview` as the
  cheaper route
- [Scoped Session reads](scoped_session_reads.md) — proposed ServiceAccount read policy for
  retained Session history
- [Kubernetes grants through PolicySets](kubernetes_rbac_groups.md) — accounts without a
  Sandbox, dropping the old policy kinds, and the verification still owed on a deployed environment
- [KubeVirt execution environments](kubevirt_environments.md) — the live gates for VM Sandboxes that no
  DAG node names; the design is [VM Sandboxes](../docs/vm_sandboxes.md)
- [User stories](user_stories.md) — north-star product context, not an implementation queue
- [Input metadata](notification_presentation.md) — a sequenced follow-up to command admission
- [Subscriptions and notifications](notifications.md) — standalone service design: SA-authorized session scope,
  explicit inbox acknowledgement, shipped Actions/GitHub delivery, remaining reliability acceptance,
  and deferred Kubernetes monitoring, automatic following and wake
