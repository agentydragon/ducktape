# Agentplane plans

Agentplane is running on staging. Implemented contracts live beside the app, runner, egress proxy,
Action Service, LLM ingress, acceptance suite, and durable evidence under [`../docs/`](../docs/).
This directory contains current design gates, genuinely deferred decisions, and north-star context;
the [task DAG](task_dag.md) is authoritative for status and dependencies.
[Maintenance conventions](../docs/task_dag_maintenance.md) distinguish dispatchable phases,
operator decisions and proportional acceptance from the [low-priority freezer](task_freezer.md).

Start dispatch from the [stakeholder priorities](task_dag.md#stakeholder-priorities-2026-10-30-pdt),
stale since their sprint ended on 2026-11-13 until the operator restates them: session resilience
first, then the daily CI agent and caller-privilege Session spawning, with small launch-UX
improvements alongside. Component TODOs and research plans do not set a competing queue.

## Open plans and gates

- [Central Anthropic follow-ups](../sampler/plans/anthropic.md) — deferred BuildBuddy
  credentials, background compaction, dynamic tools and optional Langfuse export
- [Central sampler workflow gaps](../sampler/plans/codex_parity.md) — concrete reliability
  regressions and remaining delegation usability/exposure, followed by Codex feature candidates
- [Stable sampler provider prefix](sampler_stable_prefix.md) — immutable per-conversation prompt
  baseline, provider-native updates and fallbacks for unsupported changes
- [Architecture overview](architecture_overview.md) — four whole directions for where Agentplane's
  objects and data live, the operator's workflows through each, what Agentplane borrows from
  Kubernetes and what each direction keeps or replaces, what is right under every direction, and the
  experiments that would narrow the choice; no direction chosen
- [Decoupling from Kubernetes](kubernetes_decoupling.md) — what ties Agentplane to Kubernetes, which
  tests need it, and four composable options (API-server-only test tier, seams behind service APIs,
  environments without Kubernetes, moving the record store); no direction chosen
- [History tags and History's Sandbox records](sandbox_tags.md) — older Sandbox-scoped History
  proposal; inbox tags ship, but grouping ownership, grants and lifetimes need review for independent
  Sessions before this is treated as an approved prerequisite
- [Thread sync](thread_sync/README.md) — what is still open on the deployed Electric design (pending-command paging, body compaction, measurement), and the seams and candidates for a second implementation
- [Task DAG](task_dag.md) — remaining work and proposed priorities, including unranked future
  harness-capability candidates, deployed acceptance, UI/history, and native recovery
- [Session runtime model](session_runtime_model.md) — current environment component boundaries and
  agreed names/layout; future Session vocabulary for environment-bound
  native harnesses, central sampling, execution attachments and backend-specific constraints
- [Authored conversation CLI experiment](authored_conversation_experiment.md) — our own loop with
  Qwen through LiteLLM, a shared thread operated through tools, and bounded comparative win
  conditions; the eventual history architecture remains open
- [Thread spike](thread_spike.md) — an off-by-default Thread in Notifications that the operator and a
  hosted Session act on through the CLI and an app pane; scoped, not released
- [Thread prior art](thread_prior_art.md) — how other agent systems, and this project's coordinator
  chat, route an agent's speech through explicit message tools; for the Thread spike
- [Sampling outside Sandboxes](sampling_outside_sandboxes.md) — research on running Codex's agent loop in the
  control plane with only exec servers in Sandboxes: what Codex already routes to an environment, code mode,
  where AGENTS.md comes from, what it would enable and cost, and why Claude stays put; central upstream Codex and Claude remain open
- [Native harness interfaces](native_harness_interfaces.md) — what the Runtime translates between
  Claude/Codex and the neutral Commands and Events, who reads each form, and native pass-through
  versus Runtime translation with where a shared commands layer would live; Runtime translation
  stays by default, no migration planned
- [Claude RemoteIO transport](claude_remote_io.md) — evaluate an optional `--sdk-url` bridge through Agentplane egress, including compaction-summary value and recovery gates
- [Identities, workload authentication and Kubernetes accounts](identity_and_kubernetes_accounts.md) —
  the deployed target (one `Identity`, three credential forms), identity-lifetime rules and open
  questions
- [Remaining `environment` rule operations](environment_rules.md) — `read`, `suspend` and `delete`
  grants, Session creation without an environment, and the positions on #15436
- [Cross-agent communication and authorization](multiagent_communication.md) — broader
  cross-principal transcript reads, archive policy and Session grouping; Sandbox-scoped examples do
  not settle the independent Session contract
- [Caller-privilege Session delegation](session_delegation_workflows.md) — natural-agent usability,
  instruction composition, budgets and deferred environment provisioning; the shipped tools are
  documented in [Session delegation](../action_service/session_delegation/README.md)
- [Persistent agents and scheduled wake](persistent_agents.md) — declaring a managed agent
  (instructions, execution Identity, placement, subscriptions, notice cadence) as a Flux CR applied
  by a new reconciler; declaration path decided, remaining design unscheduled, nothing released
- [Generated HTTP facades](grpc_http_facade.md) — remaining app contracts, Session-opening
  follow-ups, browser error codes, attribution, and direct browser access as a later option
- [Egress relay rename](egress_relay_rename.md) — deferred rollout for the relay binary,
  image, settings and template names
- [Notification Service split](notification_service_split.md) — the seam considered for
  `NOTIFICATION_SERVICE_SPLIT`; not released
- [Storage representations and compression](storage_representations.md) — measured content copies
  across sampler logs, History events/projections and webhooks; content ownership, deduplication,
  compression and metadata work coordinated with the authored-conversation design
- [Scoped Session reads](scoped_session_reads.md) — held broader cross-principal read policy
- [KubeVirt execution environments](kubevirt_environments.md) — the live gates for VM Sandboxes that no
  DAG node names; the design is [VM Sandboxes](../docs/vm_sandboxes.md)
- [Remaining VM storage work](vm_block_storage.md) — the retained legacy VM's operator hold and the
  deferred move of Pod and VM state to block storage
- [User stories](user_stories.md) — north-star product context, not an implementation queue
- [Input metadata](notification_presentation.md) — a sequenced follow-up to command admission
- Notification follow-ups: [subscription authorization](task_dag.md#subscription-authorization-before-broader-sources),
  [missed-update reliability](task_dag.md#github_mergeability_freshness--obtain-a-current-result-after-base-only-changes), and
  [deferred Action feed](task_freezer.md#notification_action_feed--remove-idle-action-history-polling).
  Current behavior belongs in the [service docs](../notification_service/README.md).

## Other active design and rollout notes

- [Runner delivery and harness identity](../docs/runner_delivery_and_harness_design.md) — approved
  Session addressing and lifecycle direction, current delivery boundaries, and open send-authority,
  store and harness-identity decisions
- [Session and Environment model](session_environment_model.md) — selected placement baseline and
  open zero-environment Session, attachment, authority and lifetime decisions
- [Session command authority](session_send_authority.md) — retained outcomes, revocation and
  cross-principal inbox sends
- [Credential destination transport](../egress/plans/credential_transport.md) — historical source
  inventory and a still-unselected scheme/port restriction proposal; desired config is not live
  traffic evidence
