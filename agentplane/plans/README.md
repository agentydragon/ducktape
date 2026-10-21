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
- [Cross-agent communication and authorization](multiagent_communication.md) — agreed SA attribution,
  Sandbox scopes, Sandbox tags, live bundles and Session inboxes; shared inbox/messaging
  service with subscriptions and batched notices; tentative service-enforced PolicySet rules,
  remaining schema/API decisions and deferred agent lifecycle delegation
- [Generated HTTP facades](grpc_http_facade.md) — extension of the approved gRPC-Gateway direction
  to the browser, through the app first, with direct browser access as a later option; the
  per-service tracks are in the task DAG
- [Action Service Go port](action_service_go_port.md) — stages, library choices, the OAuth authorization
  server on `zitadel/oidc`, stored-data risks and cutover for porting the service as it is
- [Kubernetes API aggregation](api_aggregation_exploration.md) — held design exploration of serving
  agent-facing records through an extension apiserver, with RBAC via `SubjectAccessReview` as the
  cheaper route
- [Scoped Session reads](scoped_session_reads.md) — proposed ServiceAccount read policy for
  retained Session history
- [KubeVirt execution environments](kubevirt_environments.md) — the live gates for VM Sandboxes that no
  DAG node names; the design is [VM Sandboxes](../docs/vm_sandboxes.md)
- [User stories](user_stories.md) — north-star product context, not an implementation queue
- [Input metadata](notification_presentation.md) — a sequenced follow-up to command admission
- [Subscriptions and notifications](notifications.md) — standalone service design: SA-authorized session scope,
  explicit inbox acknowledgement, shipped Actions/GitHub delivery, remaining reliability acceptance,
  and deferred Kubernetes monitoring, automatic following and wake
