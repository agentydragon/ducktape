# Agentplane task freezer

Deliberately deferred ideas, not the [dispatch DAG](task_dag.md). Nothing here blocks current work
unless a selected plan explicitly promotes the needed prerequisite. Each group/item states a trigger;
recheck implementation and evidence when promoting it, then add concrete phases/edges to the DAG.
These are not promises to perform every former acceptance test. Waived or redundant live checks
are removed, not stored here as obligations. Security/data-loss bugs with concrete evidence belong
in active work. See [maintenance conventions](../AGENTS.md#task-dag-maintenance).

## Notifications and operations

Revisit on a concrete latency, cost, delivery or new-source need; not a release checklist.

### `NOTIFICATION_WORKER_ISOLATION` — separate notification API and delivery workers

Only if worker deaths cause actual operational trouble. Keep workers in-process; first address the specific failure with current supervision/claims/retries.

### `NOTIFICATION_GITHUB_RETENTION` — bound GitHub delivery receipt storage

Only when measured receipt storage warrants it. Preserve dedup, creation boundaries and active-subscription replay; do not copy Session-delta policy onto webhooks.

### `NOTIFICATION_ACTION_FEED` — remove idle Action-history polling

Only if idle polling cost matters. One authorized feed per replica plus canonical catch-up; see [remaining work](notifications.md#deferred-event-driven-actions-consumption).

### `NOTIFICATION_NOTICE_PACING` — stage-aware batching before another notice

When redundant busy-turn notices are a measured problem. Start with bounded, durable timers and uncovered cursor ranges; distinguish prepared, admitted, confirmed and acknowledged. Reuse progress/status for the sidebar, but do not block its initial view on new pacing.

### `NOTIFICATION_SOURCE_WIRING` — extract shared wiring as sources accumulate

Extract from concrete repeated source implementations, not a hypothetical universal plugin API.

### `HOME_ASSISTANT_NOTIFICATIONS` — entity and event subscriptions

On an operator-selected source/use case. Define bounded entity/event filters, retained state versus edge semantics and authorized source access first.

### `CRON_NOTIFICATIONS` — scheduled notifications for agents

On scheduling demand. Review missed-tick/timezone/overlap policy before implementing durable schedules; it does not authorize wake.

### `KUBERNETES_MONITORING` — agents observe rollout progress and outcomes

On a selected rollout-following use case. Review Deployment identity/watch recovery and decide whether Notifications is the owner. If implemented as subscriptions, depend on the main DAG's `SUBSCRIPTION_AUTHORIZATION_DESIGN` and `SUBSCRIPTION_AUTHORIZATION`: source observation scope is not implied by owning an inbox.

### `AGENT_MESSAGE_CLASSIFICATION` — optional outbound content safeguards

Only after an explicit leakage-review requirement. Never substitute classifiers for messaging RBAC or silently discard accepted messages.

### `LIVE_CLEAN` — executor heartbeat retention cleanup

When executor heartbeat accumulation becomes material. Stable identity or bounded cleanup must preserve executor claim and unknown-outcome semantics.

## Harness features and native research

Operator has deferred this lane. Promote a specific feature with a concrete user need or observed defect, not the whole matrix. Preserve existing regression coverage.

### `INPUT_DELIVERY` — remaining native queue and recovery evidence

Pin only the native queue/interrupt/recovery gap needed for an adopted operation. Do not repeat the ordinary command path or invent receipts from static protocol declarations.

### `CLAUDE_RECOVERY` — native execution before durable runner evidence

If adopting automatic recovery for the native-effect-before-durable-receipt window, establish that exact boundary in controlled native tests before runner changes.

### `CODEX_RECOVERY` — native execution before durable runner evidence

Same narrow evidence-first rule for Codex; no dependency on completing Claude research.

### `CODEX_RECOVERY_PROTOCOL` — reconcile through documented app-server history APIs

When the supported public history API can replace private rollout parsing. Refresh the selected CLI pin and prove retained/absent/revised/unknown semantics; see [investigation](../debug/codex_app_server_history_apis.md).

### `LLM_INGRESS_REQUEST_RECENCY` — direct outbound request timing

If event-derived Thread recency proves too approximate, consider recording a metadata-only
request timestamp at the authenticated LLM ingress when forwarding toward LiteLLM. Today ingress
verifies Sandbox/Pod identity, **not Session identity**: multiple Threads in the same Sandbox can
make only a Sandbox-level timestamp trustworthy. Per-Thread measurement would first need a trusted
Session attribution path; do not use caller-supplied Thread headers or label shared-Sandbox traffic
as one Thread's requests. A forwarded request is still not proof the provider accepted it or that
its cache was hit. Review retention and read authorization before persisting timestamps; do not log
prompts or credentials for this indicator. This does not block the event-derived UI candidate.

### `CLAUDE_FRESH_RESUME_CACHE_SPIKE` — prefix/reasoning and cache evidence

On a measured fresh-process cost problem or portability proposal. Separate cache cost from correctness; a successful native resume is not provider cache reuse.

### `CODEX_FRESH_RESUME_CACHE_SPIKE` — prefix/reasoning and cache evidence

Same measured-cost trigger for Codex, independently of Claude.

### `RUNNER_IMAGE_UPGRADE_PROOF` — same-storage image replacement evidence

Before automated upgrades, close only missing same-storage behavior for the affected harness. Codex manual pause/patch/resume is already proven; do not re-run it as a gate for unchanged legacy mappings.

### `RUNNER_IMAGE_ROLLOUT` — upgrade runner images on existing Sandboxes

On requested automatic/operator-assisted fleet rollout. Requires appropriate upgrade evidence; preserve writer fencing and rollback. A guarded manual migration does not need a fleet-upgrade product.

### `HARNESS_CONFIG_ISOLATION` — keep project and host settings isolated

When enabling a hosted harness feature: isolate hosted configuration from deterministic capture scenarios and host/project settings.

### `DT` — driver-provided declarations and background control

Previously P2, still deferred. [Driver tools/background plan](driver_tools_and_background.md); reuse Action contracts, preceded by hosted/capture config isolation.

### `HARNESS_MCP_TASKS_EVAL` — compare with Action receipts

When considering MCP Tasks interoperability. Compare pinned client behavior with existing Action receipts before selecting an integration.

### `HARNESS_SKILLS` — both native harnesses

When selecting project-scoped skills/commands. Requires config isolation and project trust; do not widen test captures globally.

### `HARNESS_WEB_SEARCH` — source evidence in Thread

When selecting routed search. Requires config isolation, reviewed network route and retained source evidence.

### `THREAD_UPLOADS` — authorized upload, retained bytes, agent access

When selecting file/image attachments. Review authorized bytes/storage/retention before composer/agent access.

### `HARNESS_VISUAL_INPUT` — composer, protocol, storage, replay

Depends on the upload contract and hosted config isolation when selected; prove native attachment semantics and retained authorized replay.

### `HARNESS_MANUAL_COMPACTION` — user-triggered compaction from the frontend

On requested native compaction controls. Preserve durable transcript history and distinguish request/admission/completion for supported harnesses.

### `CLAUDE_REMOTE_IO_EVAL` — compare with stream-json

On a concrete benefit over stream-json. [RemoteIO plan](claude_remote_io.md); do not promote research into a mandatory transport rewrite.

### `HARNESS_INTERACTIVE_CONTROLS` — durable park, answer, recovery

When selecting questions/permissions UI. Config isolation first; explicit durable parked/answered/recovered states, not fabricated replies.

### `HARNESS_PROJECT_HOOKS` — bounded execution and control replies

When a trusted project hook is needed. Config isolation and bounded execution/control replies first.

### `HARNESS_PLUGINS` — source trust and capability grants

When selecting plugins. Review source trust/capability grants and isolate configuration.

### `HARNESS_PROMPT_SUGGESTIONS` — measure UX before enabling

Only if measured UX value justifies it. Optional and not a dependency for core messaging.

### `NATIVE_SUBAGENT_THREADS` — as linked Agentplane Threads

Explicitly deferred. [Native discovery plan](native_session_discovery.md) retains characterization and design; multiagent authority decisions compare this boundary without requiring native child ingestion.

### `CONTROL_STATE` — dynamic runtime control acceptance

On a selected model/effort capability gap. [Runtime control contract](../docs/runtime_control.md); admission is not native effect.

## Runtime redesign and offline acceptance

Requires a fresh operator product/architecture decision. These alternatives are not prerequisites for service-owned input metadata or ordinary session history.

### `RUNNER_STATE_BOUNDARY_RETHINK` — should the runner own durable state?

Choose whether command durability moves centrally and the runner becomes a thin adapter. Central-admission changes require this decision. Transport direction is settled separately by the [runner channel](../docs/runner_channel.md), which retains runner durability.

### `RUNNER_JOURNAL_SETTLEMENT` — settle deltas in the runner journal too

Trigger: measured runner volume pressure from streamed deltas. A runner-side flag reusing the History Service's settlement templates, so the journal also drops settled chunks; today the journal is the only place a live Sandbox keeps every chunk.

### `CLAUDE_OFFLINE_CATCHUP` — recover work done offline

Conditional on thin-runner design: determine which continuation evidence requires native history versus a minimal spool.

### `CODEX_OFFLINE_CATCHUP` — recover work done offline

Equivalent conditional evidence for Codex; no generic success claim across harnesses.

### `RUNNER_CENTRAL_ADMISSION` — durable commands at the central authority

Conditional on the authority decision, after archive cutover. Durable central admission/redelivery is distinct from storing input annotations.

### `RUNNER_OUTBOUND_CUTOVER` — migrate to thin, outbound-connected adapters

Only if the thin-adapter/central-authority design is selected, after its per-harness catch-up and safe image transition. A transport-only dial-out rollout is separately sequenced in the main DAG and does not require this capstone.

### `COMMAND_QUEUE_DECISION` — where submission becomes durable

Only if offline acceptance is requested. Compare runner-first with a central service queue; do not assume the integration app must own it. Metadata persistence does not select this option.

### `THREAD_COMMAND_DELIVERY` — optional app command delivery queue

Conditional on the queue decision; implement only at the chosen authority. Old app-outbox proposals are alternatives, not a ready implementation plan.

### `THREAD_OUTBOX_CUTOVER` — one ingress if the app queue is chosen

Conditional on selecting an outbox: one ingress, no competing relays. Remains behind queue implementation.

### `NEWTHREAD_DURABLE` — server-owned sandbox+thread provisioning

When promising submit-and-close-browser provisioning. Review server-owned create/open/submit orchestration after idempotent creation and bootstrap receipts; only require an outbox if the chosen contract needs offline acceptance.

### `UISHELL_NEWTHREAD_SANDBOX` — pre-scoped "+ New thread" on a Sandbox's page

After a durable combined-start contract is selected; pre-scoped composer. Manual creation remains available.

### `UISHELL_NEWTHREAD_LANDING` — sidebar "+" unscoped new-thread composer

Same prerequisite, unscoped composer. Do not implement durability as a browser-owned chain.

### `THREAD_ON_DEMAND_RUNTIME` — disposable Sandbox for a durable Thread

After revival ([task DAG section 8](task_dag.md#8-sessions-that-outlive-their-sandbox)), plus reviewed wake authority/budget. No implicit wake on reads or notifications.

### `AG` — hosted Agent and Thread model

Hosted Agent/Thread capstone only if that product is selected; lifecycle, surfaces and scoped policy compose. Not a general-purpose capability framework.

## Other deliberately deferred product ideas

Promote on operator selection, not because a prerequisite happens to have landed.

### `T3` — thread search and lookup

Transcript full-text search/lookup remains deferred; ordinary paginated Thread browsing is independent.

### `ELEVATE` — agent-requested temporary permission

Operator-reviewed temporary permission requests for ServiceAccount/Sandbox callers; preserve explicit authority, expiry and revocation.

### `ACCESS` — delegated versus brokered external access

[External access design](external_access.md): delegated versus brokered credentials/grants, not decided by current egress wiring.

### `EGRESS_CHANGE` — agent-requested egress policy expansion

Review who may request/approve/persist egress expansion before selecting an Action or configuration API; depends on the relevant access-authority choice.

### `FORK` — per-task identity fork

Per-task identity fork only for a concrete concurrent external-agent need. Depends on a reviewed identity/delegation lifecycle, not accidental inheritance.

### `PROFILES` — cross-cutting capability profiles

[Profiles](profiles.md) remains north-star context, not an active privilege-framework project.

### `SSHDURABLE` — durable SSH-backed processes

[Durable SSH processes](ssh_durable_processes.md) only when a host process must outlive a connection; not a Sandbox lifecycle prerequisite.

### `ANTHROPIC_INCLUDED_API_ROUTING` — use subscription-linked Anthropic API credit

On confirmed eligible subscription-linked credit use. Review provider route, identity and billing before enabling.

### `AIQUOTA_ANTHROPIC_API_CREDIT` — show API credit in aiquota

When reporting that credit is useful and its data source exists. Keep it distinct from Claude Code usage; do not invent quota evidence.

## Broader verification inventory

**Trigger: an uncovered event/access path is being enabled or an actual incident shows a gap.**
The remaining exhaustive GitHub lifecycle/filter/fork/revocation matrix is not an acceptance gate
for the already shipped service. Add a focused automated regression for changed behavior; use a
bounded live check only for unresolved installation/webhook wiring. Do not induce real GitHub
outages or repeat waived refresh-reuse/backoff tests. Ordinary signature/auth/private-route checks
remain regression obligations when those boundaries change; review existing coverage first.

Compound-disaster tests without a credible operational requirement are intentionally omitted.
A lack of production failure anecdotes is not a reason to reopen correct, tested behavior.
