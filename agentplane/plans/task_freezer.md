# Agentplane task freezer

Deliberately deferred ideas, not the [dispatch DAG](task_dag.md). Nothing here blocks current work
unless a selected plan explicitly promotes the needed prerequisite. Each group/item states a trigger;
recheck implementation and evidence when promoting it, then add concrete phases/edges to the DAG.
These are not promises to perform every former acceptance test. Waived or redundant live checks
are removed, not stored here as obligations. Security/data-loss bugs with concrete evidence belong
in active work. See [maintenance conventions](../docs/task_dag_maintenance.md).

## Legacy leftovers

Deferred by the operator on 2026-10-10. Revisit when the operator asks.

### `WORKLOAD_AUDIENCE_RENAME` — rename the `agentplane-egress` audience

`workload_auth/README.md` calls `agentplane-egress` a compatibility audience that may become
`agentplane-workload`. Renaming needs every verifier to accept both until running Sandbox Pods carry
the new projected token, or a Sandbox restart: an accept-both release, then dropping the old audience
once every Sandbox Pod started after that release.

## Notifications and operations

Revisit on a concrete latency, cost, delivery or new-source need; not a release checklist.

### `EXTERNAL_AGENT_INBOXES` — inboxes for externally running agents

Explicitly deferred by the operator, 2026-10-16; revisit only when the operator prioritizes a
concrete external-client receiving workflow. V1 OAuth clients can make permitted calls as their
bound SAs, but recipients are Sandbox-hosted Sessions. A future external inbox would need its own
address and delivery/read mechanism; it does not imply moving Connection storage to Kubernetes.

### `DELEGATED_INBOX_ACCESS` — delegated inbox reading and acknowledgement

Explicitly deferred by the operator, 2026-10-16. Revisit on an operator-prioritized need for another
SA to inspect or acknowledge a Sandbox's inbox entries. V1 keeps read/ack owner-SA-scoped and
defines no delegable operations for either. If promoted, evaluate reading and acknowledgement as
independent grants at Sandbox granularity, separate from send and transcript-read authority.

### `NOTIFICATION_DISPATCHER_OWNERSHIP` — review notification and Dispatcher ownership

Operator-requested future consideration, 2026-10-16; revisit when explicitly prioritized or a
concrete coordination/maintenance need justifies changing the boundary. Compare moving Notification
Service responsibilities into Session Dispatcher with retaining independent services. Distinguish
source observation, durable inbox/message storage, read/ack, notice batching and command admission.
Preserve provenance, retention, retry/receipt semantics and independent backend use; do not create a
second command queue. V1 retains today's ownership and does not depend on this review.

### `SANDBOX_EVENT_WAKE_POLICY` — event-specific automatic wake

Revisit when wake cost or a concrete source/event distinction warrants it. V1 retains the
Sandbox's wake-on-any-message versus explicit-resume policy. A notice can batch inbox entries from
multiple sources, and Notifications decides when to send it; Dispatcher sees the notice and owns
wake/delivery. A future design might add switches to Sandbox spec, but must define mixed-batch
semantics, trusted event attribution, decision placement and how unannounced entries make progress.
No per-event switches, per-peer wake grants or owner move are selected or required for v1.

### `SANDBOX_LIFECYCLE_AUTHORIZATION_DESIGN` — delegated lifecycle controls

Revisit on a concrete need for agents to manage lifecycle or explicit operator prioritization.
V1 keeps explicit suspend/resume and wake-setting edits as operator controls while preserving
trusted backend automation. If delegation is needed, compare native Kubernetes RBAC with narrow
backend operations on Sandbox targets/collections; general Sandbox patch access can edit unrelated
spec fields. Define grant administration and preserve lifecycle guards. The longer-term direction
is wake-on-message by default and transparent suspension/resume, not routine granular lifecycle
management. Current defaults are unchanged; this review does not block v1 messaging.

### `NOTIFICATION_WORKER_ISOLATION` — separate notification API and delivery workers

Only if worker deaths cause actual operational trouble. Keep workers in-process; first address the specific failure with current supervision/claims/retries.

### `NOTIFICATION_ACTION_FEED` — remove idle Action-history polling

Only if idle Action-history polling cost matters. Add a read-authorized Action change feed without
giving Notifications operator authority or cross-service database access. Use one shared feed per
replica as an invalidation source; drain canonical events from persisted `actions_after_sequence`
on startup, reconnect, subscription creation and invalidation. Register before catch-up reads,
fence racing invalidations and commit progress with inbox entries. Waiting on the feed must not
hold an inbox lease or occupy a delivery worker. Reconnect with rotating credentials and bounded
backoff; preserve delivery retries and retention scheduling. Test missed signals, reconnect,
concurrent events, subscription creation, ownership and idle-worker availability.

### `NOTIFICATION_SOURCE_DISCOVERY` — discover App-accessible sources

When selecting the next GitHub source UX improvement; tracked by
[#8981](https://github.com/agentydragon/ducktape/issues/8981). Separate accessible repositories,
active subscriptions and healthy delivery, without exposing another agent's subscriptions.

### `NOTIFICATION_SUBSCRIPTION_LIFETIME` — reconsider automatic expiry

When reviewing subscription lifecycle UX. Consider removing `lifetime_days`, or making expiry
opt-in with warning/expiry-notification semantics rather than silently ending monitoring.

### `NOTIFICATION_SOURCE_SCOPE` — additional source capabilities

When an operator selects a concrete use case. Candidates: repository subjects, workflow filters,
tags/releases and deployments/environments; narrower GitHub repository/event grants; automatic
Action following backed by durable authorized intent rather than a best-effort second request.
Do not promise complete historical replay. Broader source authorization needs
`SUBSCRIPTION_AUTHORIZATION_DESIGN` and `SUBSCRIPTION_AUTHORIZATION` first.

### `NOTIFICATION_DELIVERY_SCOPE` — additional destinations and offline behavior

When an operator selects a concrete delivery use case, review notification-triggered provisioning,
offline guarantees and wake budgets, runner-hosted MCP context, per-session identities,
cross-account delivery or successor-session retargeting. These require explicit identity and
lifecycle contracts; existing inbox ownership is not authority to provision, resume or retarget.

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

An optional later addition, explicitly outside v1 cross-agent communication and its prerequisites.
See [information transfer and scope](multiagent_communication.md#information-transfer-and-v1-scope).
Only after an explicit leakage-review requirement. Never substitute classifiers for messaging RBAC
or silently discard accepted messages.

### `INBOX_VACUUM` — remove old live inboxes

Operator, 2026-10-17: not v1; revisit when inbox growth or retention cost is material. Retention
already purges expired payloads and retired inboxes. A vacuum of other inboxes keeps the sender
[receipt](../notification_service/docs/api.md#messages) honest: a removed record answers not
found, meaning no record and never "not delivered". The design states what a sender's idempotent
retry does once its message record is gone.

### `LIVE_CLEAN` — executor heartbeat retention cleanup

When executor heartbeat accumulation becomes material. Stable identity or bounded cleanup must preserve executor claim and unknown-outcome semantics.

## Harness features and native research

Operator has deferred this lane. Promote a specific feature with a concrete user need or observed defect, not the whole matrix. Preserve existing regression coverage.

### `CLAUDE_PERMISSION_MODE_REVIEW` — reconsider native permission modes

Operator-deferred, 2026-10-17. The selected policy is explicit `bypassPermissions` for deployed
Claude harnesses.
Revisit if we want native approval policy, such as `auto` mode's classifier; choose its intended
allow/deny behavior and host approval interaction explicitly, and validate deployed tool use before
changing the mode. This does not block the current unattended-execution rollout.

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

[Driver tools/background plan](driver_tools_and_background.md); reuse Action contracts, preceded by hosted/capture config isolation.

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

### `CODEX_IMAGE_VIEW_ITEM` — project workspace image-view lifecycle

When Agentplane needs accurate item history for Codex workspace viewing. Model the app-server `imageView` item explicitly: its `{id, path}` completion payload has no `status`, so generic projection does not mark it successful. Keep workspace viewing separate from composer uploads; UI preview additionally needs an authorized byte-transfer path.

### `HARNESS_MANUAL_COMPACTION` — user-triggered compaction from the frontend

On requested native compaction controls. Preserve durable transcript history and distinguish request/admission/completion for supported harnesses.

### `CLAUDE_REMOTE_IO_EVAL` — compare with stream-json

On a concrete benefit over stream-json. [RemoteIO plan](claude_remote_io.md); do not promote research into a mandatory transport rewrite.

### `HARNESS_INTERACTIVE_CONTROLS` — durable park, answer, recovery

When selecting questions/permissions UI. Config isolation first; explicit durable
parked/answered/recovered states, not fabricated replies. Claude's pinned-source findings
distinguish transient `request_id` from durable semantic `tool_use_id`; persist the latter and
reconstruct pending controls rather than storing resolver callbacks
([native evidence](../native/docs/claude_source_findings.md#interactive-control-identity-static-inspection-of-21252)).

### `HARNESS_PROJECT_HOOKS` — bounded execution and control replies

When a trusted project hook is needed. Config isolation and bounded execution/control replies first.

### `HARNESS_PLUGINS` — source trust and capability grants

When selecting plugins. Review source trust/capability grants and isolate configuration.

### `HARNESS_PROMPT_SUGGESTIONS` — measure UX before enabling

Only if measured UX value justifies it. Optional and not a dependency for core messaging.

### `NATIVE_SUBAGENT_THREADS` — as linked Agentplane Threads

Deferred until the operator selects child transcript browsing or independent child addressability.
The 2026-10-16 selection is native delegation with root-only normalized output, not child Sessions.
[Completed native evidence](../native/docs/child_sessions.md) establishes visibility and recovery
boundaries; it does not settle child authorization or independent control. The
[native discovery plan](native_session_discovery.md) retains the proposed design. Event ingress can
index runner-declared children if selected, but does not itself implement discovery, durable identity,
or access policy. Multiagent authority decisions can use the evidence without requiring child ingestion.

### `CONTROL_STATE` — dynamic runtime control acceptance

On a selected model/effort capability gap. [Runtime control contract](../docs/runtime_control.md); admission is not native effect.

## Runtime redesign and offline acceptance

Requires a fresh operator product/architecture decision. These alternatives are not prerequisites for service-owned input metadata or ordinary session history.

### `RUNNER_STATE_BOUNDARY_RETHINK` — should the runner own durable state?

The central-sampling portion is now owned by active `CENTRAL_SAMPLER_V1` in
[task DAG](task_dag.md#harness_placement_design--sampler-placement-and-state-authority), informed by
the completed Codex placement and Postgres harness spikes (operator, 2026-10-27). Do not create a competing authority
decision here. A standalone redesign of legacy Sandbox-hosted runners remains deferred until explicitly
selected. Central-admission implementation still waits for the reviewed authority design. Transport direction
is settled separately by the [runner control](../docs/runner_control.md), which currently retains
runner durability.

### `RUNNER_JOURNAL_SETTLEMENT` — settle deltas in the runner journal too

Trigger: measured runner volume pressure from streamed deltas. A runner-side flag reusing the History Service's settlement templates, so the journal also drops settled chunks; today the journal is the only place a live Sandbox keeps every chunk.

### `CLAUDE_OFFLINE_CATCHUP` — recover work done offline

Conditional on thin-runner design: determine which continuation evidence requires native history versus a minimal spool.

### `CODEX_OFFLINE_CATCHUP` — recover work done offline

Equivalent conditional evidence for Codex; no generic success claim across harnesses.

### `RUNNER_CENTRAL_ADMISSION` — durable commands at the central authority

Conditional on the authority decision. Durable central admission/redelivery is distinct from storing input annotations.

### `RUNNER_OUTBOUND_CUTOVER` — migrate to thin, outbound-connected adapters

Only if the thin-adapter/central-authority design is selected, after its per-harness catch-up and safe image transition. Runner control currently uses private mTLS gRPC ([runner control](../docs/runner_control.md)).

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

After revival ([task DAG section 7](task_dag.md#7-our-own-sandbox-resource-sessions-that-outlive-their-pod)), plus reviewed wake authority/budget. Wakes on a user message or notification, never on a read, as the [control ADR](../docs/adr_sandbox_session_control.md) directs.

### `AG` — hosted Agent and Thread model

Hosted Agent/Thread capstone only if that product is selected; lifecycle, surfaces and scoped policy compose. Not a general-purpose capability framework.

## Other deliberately deferred product ideas

Promote on operator selection, not because a prerequisite happens to have landed.

### `THREAD_TITLE_GENERATION` — revisit automatic Thread titles

Operator-requested deferral, 2026-10-26. Automatic Thread title generation was removed from the app.
Reopen it only after the design in [#11817](https://github.com/agentydragon/ducktape/issues/11817) is
resolved; then decide its interaction, owner, and reliable History reads before implementation. Thread
names remain set, changed, and cleared by operators.

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

### `SSHDURABLE` — durable SSH-backed processes

[Durable SSH processes](../../ssh_mcp_server/plans/durable_processes.md) only when a host process must outlive a connection; not a Sandbox lifecycle prerequisite.

### `ANTHROPIC_INCLUDED_API_ROUTING` — use subscription-linked Anthropic API credit

On confirmed eligible subscription-linked credit use. Review provider route, identity and billing before enabling.

### `AIQUOTA_ANTHROPIC_API_CREDIT` — show API credit in aiquota

When reporting that credit is useful and its data source exists. Keep it distinct from Claude Code usage; do not invent quota evidence.

### `HISTORY_SESSION_REKEY` — key History by Sandbox UID and Session ID

Only if the globally unique public Session ID contract changes. History and the app keep the global
Session UUID as their key and record the Sandbox UID beside it (operator, 2026-10-24; #11835); a
`(sandbox_uid, session_id)` key is reconsidered only then.

### `API_AGGREGATION` — serve agent-facing records as a Kubernetes aggregated API

Held by the operator, 2026-10-17. [The exploration](api_aggregation_exploration.md) has the candidates and costs.
`ActionRequest`'s workload half is the only record worth it, once the Action Service is Go and a
`kubectl`-style client surface for it is wanted. RBAC as sender authorization does not need it: a
service asks `SubjectAccessReview`, which belongs to the sender-grant and agent-authority choices.

## Broader verification inventory

**Trigger: an uncovered event/access path is being enabled or an actual incident shows a gap.**
The remaining exhaustive GitHub lifecycle/filter/fork/revocation matrix is not an acceptance gate
for the already shipped service. Add a focused automated regression for changed behavior; use a
bounded live check only for unresolved installation/webhook wiring. Do not induce real GitHub
outages or repeat waived refresh-reuse/backoff tests. Ordinary signature/auth/private-route checks
remain regression obligations when those boundaries change; review existing coverage first.

Compound-disaster tests without a credible operational requirement are intentionally omitted.
A lack of production failure anecdotes is not a reason to reopen correct, tested behavior.

### `RUNNER_IDLE_HARNESS_REAP` — the runner reaps and restarts idle harness processes

**V2, deferred by the operator (2026-10-17).** Runner-owned lifecycle is deployed and
accepted. Revisit when idle-process memory measurements justify reclaiming it; define
eligibility and background-work protection before implementation. An idle
Claude process, one per main Session, can hold more RAM than an idle Codex app server that hosts many
agents. With runner-owned process lifecycle, it can reap an idle
process and start it again when input arrives, with no caller-visible kill or restart. Open: what counts
as idle for a process (not `idle_since` alone, which is display and delivery timing), the threshold, and
that a reap must not end a turn, a background process or a Session another caller is waiting on. Measure
idle RAM per harness first.
Claude runs one process per Session; Codex runs one app-server per Runner for all of its Codex
Sessions, so reaping that process touches every one of them. Reaping must account for native children and
background work ([child evidence](../native/docs/child_sessions.md),
[background work](../docs/background_work.md)), which the current busy signal does not track.

## Frontend

### `FRONTEND_QUERY_CACHE` — a query cache for ad-hoc fetches

**Frozen; operator, 2026-10-30: think about it later.** About eight fetch-in-`useEffect` blocks with
manual cancellation guards, a `setInterval` poll and a hand-built Action request cache could become
TanStack Query (with `openapi-react-query` over the existing `openapi-fetch` client). See the
[idiom audit](standard_idioms.md).

### `FRONTEND_CONNECT_ES` — typed streaming clients

**Frozen; operator, 2026-10-30: think about it later.** Backend watches are read as grpc-gateway NDJSON
with hand-written ProtoJSON decoding; Connect-ES streaming clients generated from the same protos would
replace that, at the cost of revisiting the app's deliberate gateway choice (`app/README.md`).
