# Agentplane plans

Agentplane is running on staging. Implemented contracts live beside the app, runner, egress proxy,
Action Service, LLM ingress, acceptance suite, and durable evidence under [`../docs/`](../docs/).
This directory contains current design gates, genuinely deferred decisions, and north-star context;
the [task DAG](task_dag.md) is authoritative for status and dependencies.
[Maintenance conventions](../docs/task_dag_maintenance.md) distinguish dispatchable phases,
operator decisions and proportional acceptance from the [low-priority freezer](task_freezer.md).

## Open plans and gates

- [Architecture overview](architecture_overview.md) — four whole directions for where Agentplane's
  objects and data live, the operator's workflows through each, what Agentplane borrows from
  Kubernetes and what each direction keeps or replaces, what is right under every direction, and the
  experiments that would narrow the choice; no direction chosen
- [Dispatcher and Gateway as one service](dispatcher_gateway_merge.md) — proposed merge of the Session
  Dispatcher and the Runner Gateway's proxy into one gRPC service with an optional PostgreSQL queue: the command
  states in both modes, ownership by the replica holding the runner connection, and the order of work; awaiting
  operator review
- [Sandbox tags and History's Sandbox records](sandbox_tags.md) — agreed design for inbox and
  history tags on the Sandbox and History's own record of each Sandbox, which outlives it
- [Native session discovery](native_session_discovery.md) — remaining harness characterization and
  deferred shared-session design with identity, attribution and durability gates
- [Thread sync](thread_sync/README.md) — what is still open on the deployed Electric design (pending-command paging, body compaction, measurement), and the seams and candidates for a second implementation
- [Task DAG](task_dag.md) — remaining work and proposed priorities, including unranked future
  harness-capability candidates, deployed acceptance, UI/history, and native recovery
- [Driver-provided tools and background work](driver_tools_and_background.md) — deferred seam that must reuse the Action contracts
- [Claude RemoteIO transport](claude_remote_io.md) — evaluate an optional `--sdk-url` bridge through Agentplane egress, including compaction-summary value and recovery gates
- [Agent access to external systems](external_access.md) — deferred delegated-versus-brokered access
  choices and broader authorization-system evaluation, separate from multiagent implementation
- [Cross-agent communication and authorization](multiagent_communication.md) — agreed requirements
  (SA attribution, Sandbox scopes and tags, live bundles) and what is open after messaging shipped:
  the transcript-read rule and archive split, discovery, Session creation and Sandbox launch
- [Generated HTTP facades](grpc_http_facade.md) — what is left: History reads, the app-contract
  question, browser error codes, attribution, and direct browser access as a later option
- [MCP edge extraction](mcp_edge_extraction.md) — retirement of Actions' integrated Connection/OAuth
  ownership; staging's edge handoff is merged
- [Kubernetes API aggregation](api_aggregation_exploration.md) — held design exploration of serving
  agent-facing records through an extension apiserver, with RBAC via `SubjectAccessReview` as the
  cheaper route
- [Orchestrating agents from an MCP client](mcp_orchestration.md) — proposed workflow, tool surface and authority
  for Claude or any MCP Connection to launch worker Sandboxes, give them tasks and read their outcomes: one Action
  group run as the operator's act until each backend can grant callers itself
- [Scoped Session reads](scoped_session_reads.md) — proposed ServiceAccount read policy for
  retained Session history
- [KubeVirt execution environments](kubevirt_environments.md) — the live gates for VM Sandboxes that no
  DAG node names; the design is [VM Sandboxes](../docs/vm_sandboxes.md)
- [User stories](user_stories.md) — north-star product context, not an implementation queue
- [Input metadata](notification_presentation.md) — a sequenced follow-up to command admission
- Notification follow-ups: [subscription authorization](task_dag.md#subscription-authorization-before-broader-sources),
  [missed-update reliability](task_dag.md#github-notice-reliability-after-missed-updates), and
  [deferred Action feed](task_freezer.md#notification_action_feed--remove-idle-action-history-polling).
  Current behavior belongs in the [service docs](../notification_service/README.md).
