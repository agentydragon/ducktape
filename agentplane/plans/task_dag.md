# Agentplane task DAG

This is the authoritative map of **unfinished** Agentplane tasks. Edges are technical dependencies;
operator priority is separate. Remove a task when completed; do not retain done nodes, shipped-foundation
sections or acceptance-history recaps here. Completed work and evidence belong in component contracts,
not this backlog. See the [Action Service specification](../action_service/SPEC.md),
[service integration details](../action_service/README.md),
[workload authentication](../docs/workload_authentication.md),
[operator federation](../docs/operator_federation.md),
[launch presets](../docs/launch_presets.md), and [action policies](../docs/action_policies.md).

## Operator priority

All Agentplane components and their acceptance contracts are multi-replica by default. Durable
state belongs to its authority: runner SQLite for admitted commands and Events, PostgreSQL for
the app archive and Action Service, and Kubernetes for Sandbox intent. Cross-replica change fanout uses
the authority's notification/watch mechanism (PostgreSQL `NOTIFY` for Action Service state), with
reconnect/replay from durable state rather than process-local memory. A single-replica deployment
is an explicit temporary operational constraint, never an implicit correctness assumption.

**Immediate operator priority:** `THREAD_READ_POLICY_DESIGN` — specify scoped
ServiceAccount access to selected Session history and derived Thread views now. Ship
`THREAD_READ_POLICY` only after `THREAD_ARCHIVE_OWNERSHIP` provides an acyclic,
durable archive source and `SANDBOX_COMPARTMENT_BOUNDARY` prevents co-resident
sessions from crossing trust domains. None requires the hosted runtime pivot.

Other priorities and candidates:

- **P2:** driver-hosted tools (`DT`). This does not block the current API-level
  acceptance closure.
- **Unranked future harness capabilities:** project skills and commands, web search,
  file/image uploads, visual input, MCP Tasks interoperability research, native subagents,
  interactive controls, project hooks/plugins, prompt suggestions, and a Claude RemoteIO
  transport evaluation. The existing P2 item `DT` keeps its priority and covers
  Action-backed tools and background-work control. The new candidates are an inventory, not
  an execution order or a priority claim against the rest of this DAG. Their win/work estimates are
  provisional; compare them with the full roadmap when scheduling. `HARNESS_CONFIG_ISOLATION` is
  the shared technical prerequisite.

Transcript search/lookup (`T3`) remains deferred. Priority is not a dependency between these tracks.

## DAG

**A node is one atomic piece of work.** A migration that replaces three services running on
three clocks is three nodes, not one, even when a single sentence describes all of them: one
node cannot be half done, and a node nothing can finish hides which third is blocked. Where
the pieces only mean something together, they feed a **capstone** node that depends on all of
them and carries the "it is all migrated" claim -- the capstone is what other work waits on,
and the pieces are what gets dispatched.

```mermaid
flowchart TB
    classDef active fill:#dbeafe,stroke:#1d4ed8,color:#1e3a8a,stroke-width:3px
    classDef decision fill:#ffedd5,stroke:#c2410c,color:#7c2d12,stroke-width:2px,stroke-dasharray:5 3
    classDef future fill:#f3f4f6,stroke:#6b7280,color:#374151
    classDef milestone fill:#ede9fe,stroke:#6d28d9,color:#4c1d95,stroke-width:2px

    ELEVATE["Planned behavior<br/>agent-requested temporary permission<br/>ServiceAccount and Sandbox callers, operator-approved"]:::future
    INPUT_DELIVERY["Remaining native evidence<br/>input/interrupt/recovery gaps<br/>exact upstream requests and queue fates"]:::active
    T3["Deferred product work<br/>thread search and lookup<br/>later prioritization"]:::future
    PC_EGRESS_CREDENTIALS["Remaining configuration<br/>label public-coder's OpenClaw caller<br/>for Action Service admission"]:::future
    PC_EGRESS["Capstone<br/>public-coder-agent egress migration<br/>proven equivalent, cut over, old proxy retired"]:::milestone
    ANTHROPIC_INCLUDED_API_ROUTING["Unranked integration<br/>use eligible subscription-linked API credits<br/>reviewed provider route and billing"]:::future
    AIQUOTA_ANTHROPIC_API_CREDIT["Unranked reporting<br/>show subscription-linked API credit<br/>separate from Claude Code usage"]:::future
    ACCESS["Deferred design<br/>delegated vs brokered external access<br/>grants and revocation"]:::future
    EGRESS_CHANGE["Deferred design<br/>agent-requested egress<br/>policy expansion"]:::future
    BINDING_SUBJECT_ARITY["Schema cleanup<br/>singular subject across binding kinds<br/>before multi-subject use"]:::future
    NOTIFICATION_ACTION_FEED["Deferred optimization<br/>event-driven Action consumption<br/>replace idle history polling"]:::future
    NOTIFICATION_GITHUB_RETENTION["Storage follow-up<br/>bound GitHub webhook receipt payloads<br/>preserve dedup and subscription replay"]:::future
    NOTIFICATION_WORKER_ISOLATION["Deferred reliability refactor<br/>separate notification HTTP and delivery workers<br/>independent failure domains"]:::future
    NOTIFICATION_NOTICE_PACING["Incremental improvement<br/>stage-aware notice pacing<br/>avoid redundant busy-turn notices"]:::future
    HOME_ASSISTANT_NOTIFICATIONS["Unranked future source<br/>Home Assistant events and state changes"]:::future
    NOTIFICATION_SOURCE_WIRING["Conditional future refactor<br/>extract shared source wiring<br/>from concrete implementations"]:::future
    CRON_NOTIFICATIONS["Unranked future capability<br/>scheduled / cron notifications<br/>durable schedules and missed-tick policy"]:::future
    MULTIAGENT_MODEL["Unranked design<br/>Agentplane-level agent relationships<br/>authority vs harness-native children"]:::decision
    AGENT_SANDBOX_LAUNCH["Unranked capability<br/>agent-requested Sandbox launch<br/>bounded specs and delegated ownership"]:::future
    AGENT_MESSAGING_DESIGN["Unranked design<br/>agent-to-agent message authority<br/>choose transport and delivery contract"]:::decision
    AGENT_MESSAGING["Unranked capability<br/>send and receive authorized agent messages<br/>durable delivery and batching"]:::future
    AGENT_MESSAGE_CLASSIFICATION["Optional later safeguard<br/>classify agent messages for leakage<br/>without silently losing delivery"]:::future
    NOTIFICATION_PRESENTATION["Unranked future capability<br/>structured notification provenance<br/>compact frontend presentation"]:::future
    THREAD_NOTIFICATION_INDICATOR["Unranked UI improvement<br/>Thread sidebar pending-notice indicator<br/>including upcoming delivery"]:::future
    KUBERNETES_MONITORING["Unranked future capability<br/>agent-visible Kubernetes rollout monitoring<br/>notifications are an option"]:::future
    DT["P2 deferred<br/>Action-backed driver tools and background control"]:::future
    HARNESS_CONFIG_ISOLATION["Unranked prerequisite<br/>separate hosted feature config from capture scenarios<br/>keep project and host settings isolated"]:::future
    HARNESS_MCP_TASKS_EVAL["Unranked reading project<br/>MCP Tasks in pinned Claude and Codex clients<br/>compare with Action receipts"]:::decision
    HARNESS_SKILLS["Unranked candidate<br/>project-scoped skills and commands<br/>both native harnesses"]:::future
    HARNESS_WEB_SEARCH["Unranked candidate<br/>routed web search<br/>source evidence in Thread"]:::future
    HARNESS_VISUAL_INPUT["Unranked candidate<br/>image attachments and visual input<br/>composer, protocol, storage, replay"]:::future
    THREAD_UPLOADS["Unranked candidate<br/>attach files and images to a Thread<br/>authorized upload, retained bytes, agent access"]:::future
    HARNESS_MANUAL_COMPACTION["Unranked future control<br/>user-triggered harness compaction<br/>from the frontend"]:::future
    CLAUDE_REMOTE_IO_EVAL["Unranked transport evaluation<br/>Claude RemoteIO through egress proxy<br/>compare with stream-json"]:::decision
    HARNESS_INTERACTIVE_CONTROLS["Unranked candidate<br/>questions and permission decisions<br/>durable park, answer, recovery"]:::future
    HARNESS_PROJECT_HOOKS["Unranked candidate<br/>trusted project hooks<br/>bounded execution and control replies"]:::future
    HARNESS_PLUGINS["Unranked candidate<br/>project plugins and skill packages<br/>source trust and capability grants"]:::future
    HARNESS_PROMPT_SUGGESTIONS["Optional, lowest estimated win<br/>Claude prompt suggestions<br/>measure UX before enabling"]:::future
    THREAD_OUTLIVES_SANDBOX["Deferred design<br/>a Thread lifecycle that outlives its Sandbox<br/>hosted rather than Sandbox-bound"]:::future
    THREAD_ARCHIVE_PLACEMENT["Archive ownership decision<br/>Sandbox Service component or independent history service<br/>no backend-to-app dependency"]:::decision
    THREAD_ARCHIVE_STORE["Durable raw Event store<br/>one log identity, replayable prefix and cursor<br/>survives Sandbox deletion"]:::future
    THREAD_ARCHIVE_BACKFILL["One-off history import<br/>app raw prefixes and legacy runner locator mapping<br/>validate IDs, cursors and coverage"]:::future
    THREAD_ARCHIVE_INGEST["Live archive ingester<br/>runner replay, exact duplicates and fencing<br/>independent of fold projection"]:::future
    THREAD_ARCHIVE_UI_CUTOVER["App projection cutover<br/>consume archive replay, track fold lag<br/>retire app archive writes and SA bypass"]:::future
    THREAD_ARCHIVE_OWNERSHIP["Capstone<br/>single durable Session Event archive authority<br/>app is a consumer, not a backend source"]:::milestone
    SESSION_EVENT_RETENTION["Storage follow-up<br/>measure and compact redundant Session deltas<br/>preserve native/debug and replay contracts"]:::future
    APP_ALEMBIC_SQUASH["One-off app schema cleanup<br/>new baseline after identity/archive cutover<br/>stamp each deployed database before pruning"]:::future
    SANDBOX_COMPARTMENT_DESIGN["Trust-boundary decision<br/>Sandbox compartment assignment and enforcement<br/>shared filesystem and SA"]:::decision
    SANDBOX_COMPARTMENT_BOUNDARY["Enforce Sandbox trust domain<br/>reject incompatible Open and replacement<br/>no false cross-compartment isolation"]:::future
    RUNNER_STATE_BOUNDARY_RETHINK["Deferred architecture question<br/>should runner own a database at all?<br/>thin harness adapter vs durable journal"]:::decision
    RUNNER_OUTBOUND_CHANNEL["Future transport<br/>worker dials central service<br/>authenticate, fence and reconnect"]:::future
    CLAUDE_OFFLINE_CATCHUP["Claude offline catch-up evidence<br/>turn continues; sync after outage or crash<br/>native history vs minimal spool"]:::future
    CODEX_OFFLINE_CATCHUP["Codex offline catch-up evidence<br/>turn continues; sync after outage or crash<br/>native history vs minimal spool"]:::future
    RUNNER_CENTRAL_ADMISSION["Future central authority<br/>durable command receipt and redelivery<br/>without runner journal as source"]:::future
    RUNNER_OUTBOUND_CUTOVER["Future migration capstone<br/>outbound-connected thin adapters<br/>central commands and Event authority"]:::milestone
    THREAD_PORTABLE_STATE["Deferred shared portability contract<br/>snapshot, fence, and restore runner/native state<br/>outside disposable Sandbox storage"]:::decision
    CLAUDE_PORTABLE_STATE["Conditional Claude implementation<br/>native snapshot/restore<br/>only on supported evidence"]:::future
    CODEX_PORTABLE_STATE["Conditional Codex implementation<br/>native snapshot/restore<br/>only on supported evidence"]:::future
    THREAD_ON_DEMAND_RUNTIME["Deferred runtime lifecycle<br/>new Sandbox on activity/notice<br/>restore and resume a durable Thread"]:::future
    HOSTED_THREAD_SURFACES["Deferred design<br/>read and control surfaces for a hosted Thread<br/>beyond today's Sandbox-bound view"]:::future
    THREAD_READ_POLICY_DESIGN["Immediate policy design<br/>compartments, grants and revocation<br/>read distinct from send and create"]:::active
    THREAD_READ_POLICY["Scoped read implementation<br/>enforce SA grants at archive boundary<br/>list, raw, evidence and feeds"]:::future
    CROSS_THREAD_DELIVERY["Deferred design<br/>agents send to other Threads<br/>command vs notification inbox"]:::decision
    THREAD_CREATE_POLICY["Deferred design<br/>SA-authorized Thread creation<br/>scoped Sandbox and stable identity"]:::decision
    AG["Capstone<br/>hosted Agent and Thread model<br/>lifecycle, surfaces and read policy together"]:::milestone
    CONNECTION_SA_REBIND["Planned mutation<br/>rebind a Connection's ServiceAccount in place<br/>no mutation exists; only a fresh OAuth consent does"]:::future
    SANDBOX_RBAC["Managed Kubernetes access<br/>catalog choices and SA bindings<br/>live acceptance pending; see #8596"]:::active
    KUBERNETES_RBAC_POLICIES["Deferred design<br/>reusable Kubernetes RBAC policies<br/>shared bindings, propagation, compact UI"]:::future
    CALLER_GRANT_VIEW["Planned UI<br/>one grant view for Sandboxes and unmanaged agents<br/>an unmanaged agent's policy is invisible today"]:::future
    MANAGED_SA_RBAC["Planned Kubernetes access<br/>RoleBindings as a managed grant kind<br/>any managed ServiceAccount, Sandbox-backed or not"]:::future
    CLAUDE_AI_SA["Planned identity<br/>the claude.ai account's deliberate authority<br/>cluster diagnostics and agent-readable reads; reaches Forgejo as haku"]:::future

    UISHELL_NEWTHREAD_SANDBOX["Deferred combined UI<br/>pre-scoped '+ New thread' on a Sandbox's page<br/>Sandbox selected, Thread fields editable"]:::future
    UISHELL_NEWTHREAD_LANDING["Deferred combined UI<br/>sidebar '+' unscoped new-thread composer<br/>Sandbox/preset/model pickers + prompt"]:::future
    COMMAND_QUEUE_DECISION["Deferred decision<br/>accept commands while runner unavailable?<br/>current slice uses runner admission first"]:::decision
    COMMAND_PROGRESS_WIDGET["Unranked UI<br/>shared in-progress command indicator<br/>input, model, reasoning effort"]:::future
    THREAD_OPEN_RELOAD_RECOVERY["Open recovery follow-up<br/>runner committed but mapping absent<br/>safe reload reconciliation"]:::future
    BOOTSTRAP_PROGRESS_CONTRACT["Planned contract<br/>one bootstrap attempt with durable progress/result<br/>no HTTP-held script execution"]:::future
    SANDBOX_CREATE_RECONCILE["Lifecycle acceptance<br/>lost Create reply and partial grants<br/>current UID, no deleted-object tombstone"]:::future
    CLAUDE_RECOVERY["Required evidence then implementation<br/>Claude execution before durable runner proof<br/>native correlation and safe recovery"]:::active
    CODEX_RECOVERY["Required evidence then implementation<br/>Codex execution before durable runner proof<br/>native correlation and safe recovery"]:::active
    CLAUDE_FRESH_RESUME_CACHE_SPIKE["Independent Claude spike<br/>fresh-process resume from copied native state<br/>prefix/reasoning and cache evidence"]:::future
    CODEX_FRESH_RESUME_CACHE_SPIKE["Independent Codex spike<br/>fresh-process resume from copied native state<br/>prefix/reasoning and cache evidence"]:::future
    CODEX_RECOVERY_PROTOCOL["Deferred interoperability follow-up<br/>Codex reconciliation via documented app-server APIs<br/>replace private rollout inspection"]:::future
    SANDBOX_LIFECYCLE_DURABILITY["Planned lifecycle correctness<br/>retained state through suspension<br/>archive before managed storage deletion"]:::future
    RUNNER_IMAGE_UPGRADE_PROOF["Image upgrade evidence<br/>pause, patch CR image, resume on same storage<br/>both harnesses and rollback"]:::decision
    RUNNER_IMAGE_ROLLOUT["Supported operator workflow<br/>upgrade image of an existing Sandbox<br/>preserve Thread state and resume safely"]:::future
    SANDBOX_VM_ISOLATION["Deferred provider integration<br/>selectable KubeVirt environments<br/>production service, gateway and lifecycle proof"]:::future
    THREAD_IDENTITY_NEW["New Session identity<br/>one canonical UUID for Session/Thread<br/>create/Open across app and runner"]:::future
    THREAD_EVENT_CONTINUITY["Identity cutover capstone<br/>legacy mapping plus new IDs, one runner journal<br/>exclusive writer across incarnations"]:::milestone
    THREAD_COMMAND_DELIVERY["Deferred backend<br/>app outbox delivery to existing runner<br/>only if app-first acceptance is chosen later"]:::future
    NATIVE_SUBAGENT_THREADS["Unranked candidate<br/>enable and adopt native subagents<br/>as linked Agentplane Threads"]:::future
    NEWTHREAD_DURABLE["Deferred combined workflow<br/>server-owned sandbox+thread provisioning<br/>survive browser close and app restart"]:::future
    THREAD_OUTBOX_CUTOVER["Deferred cutover<br/>all product commands via app outbox if chosen<br/>no competing relay path"]:::future
    THREAD_SUCCESSOR_DELIVERY["Deferred decision<br/>unsettled Thread command across<br/>successor runner session"]:::future
    THREAD_SYNC_STOPPED_RECOVERY["Remaining recovery UX<br/>restart stopped Thread synchronization safely<br/>without a manual Refresh thread button"]:::future

    THREAD_OUTLIVES_SANDBOX --> AG
    SANDBOX_COMPARTMENT_DESIGN --> SANDBOX_COMPARTMENT_BOUNDARY
    SANDBOX_COMPARTMENT_BOUNDARY --> THREAD_READ_POLICY
    THREAD_READ_POLICY_DESIGN --> THREAD_READ_POLICY
    THREAD_ARCHIVE_PLACEMENT --> THREAD_ARCHIVE_STORE
    THREAD_ARCHIVE_STORE --> THREAD_ARCHIVE_BACKFILL
    THREAD_ARCHIVE_STORE --> THREAD_ARCHIVE_INGEST
    THREAD_ARCHIVE_BACKFILL --> THREAD_ARCHIVE_UI_CUTOVER
    MULTIAGENT_MODEL --> AGENT_MESSAGING_DESIGN
    MULTIAGENT_MODEL --> AGENT_SANDBOX_LAUNCH
    AGENT_MESSAGING_DESIGN --> AGENT_MESSAGING
    AGENT_MESSAGING --> AGENT_MESSAGE_CLASSIFICATION
    THREAD_ARCHIVE_INGEST --> THREAD_ARCHIVE_UI_CUTOVER
    THREAD_ARCHIVE_UI_CUTOVER --> THREAD_ARCHIVE_OWNERSHIP
    RUNNER_STATE_BOUNDARY_RETHINK --> RUNNER_OUTBOUND_CUTOVER
    RUNNER_OUTBOUND_CHANNEL --> RUNNER_OUTBOUND_CUTOVER
    CLAUDE_OFFLINE_CATCHUP --> RUNNER_OUTBOUND_CUTOVER
    CODEX_OFFLINE_CATCHUP --> RUNNER_OUTBOUND_CUTOVER
    RUNNER_CENTRAL_ADMISSION --> RUNNER_OUTBOUND_CUTOVER
    RUNNER_IMAGE_ROLLOUT --> RUNNER_OUTBOUND_CUTOVER
    THREAD_ARCHIVE_OWNERSHIP --> RUNNER_OUTBOUND_CUTOVER
    THREAD_ARCHIVE_OWNERSHIP --> THREAD_READ_POLICY
    THREAD_ARCHIVE_OWNERSHIP --> THREAD_EVENT_CONTINUITY
    RUNNER_IMAGE_UPGRADE_PROOF --> RUNNER_IMAGE_ROLLOUT
    RUNNER_IMAGE_ROLLOUT --> THREAD_EVENT_CONTINUITY
    THREAD_IDENTITY_NEW --> THREAD_EVENT_CONTINUITY
    THREAD_EVENT_CONTINUITY --> APP_ALEMBIC_SQUASH
    THREAD_ARCHIVE_OWNERSHIP --> SESSION_EVENT_RETENTION
    THREAD_ARCHIVE_OWNERSHIP --> APP_ALEMBIC_SQUASH
    THREAD_OUTLIVES_SANDBOX --> THREAD_ON_DEMAND_RUNTIME
    THREAD_PORTABLE_STATE --> THREAD_ON_DEMAND_RUNTIME
    THREAD_PORTABLE_STATE --> CLAUDE_PORTABLE_STATE
    THREAD_PORTABLE_STATE --> CODEX_PORTABLE_STATE
    CLAUDE_FRESH_RESUME_CACHE_SPIKE --> CLAUDE_PORTABLE_STATE
    CODEX_FRESH_RESUME_CACHE_SPIKE --> CODEX_PORTABLE_STATE
    CLAUDE_PORTABLE_STATE -. Claude runtime .-> THREAD_ON_DEMAND_RUNTIME
    CODEX_PORTABLE_STATE -. Codex runtime .-> THREAD_ON_DEMAND_RUNTIME
    SANDBOX_LIFECYCLE_DURABILITY --> THREAD_ON_DEMAND_RUNTIME
    SANDBOX_COMPARTMENT_BOUNDARY --> THREAD_ON_DEMAND_RUNTIME
    THREAD_EVENT_CONTINUITY --> THREAD_ON_DEMAND_RUNTIME
    HOSTED_THREAD_SURFACES --> AG
    THREAD_READ_POLICY --> AG
    PC_EGRESS_CREDENTIALS --> PC_EGRESS

    HARNESS_CONFIG_ISOLATION -. prerequisite .-> HARNESS_SKILLS
    HARNESS_CONFIG_ISOLATION -. prerequisite .-> HARNESS_WEB_SEARCH
    HARNESS_CONFIG_ISOLATION -. prerequisite .-> HARNESS_VISUAL_INPUT
    HARNESS_CONFIG_ISOLATION -. prerequisite .-> NATIVE_SUBAGENT_THREADS
    HARNESS_CONFIG_ISOLATION -. prerequisite .-> HARNESS_INTERACTIVE_CONTROLS
    HARNESS_CONFIG_ISOLATION -. prerequisite .-> HARNESS_PROJECT_HOOKS
    HARNESS_CONFIG_ISOLATION -. prerequisite .-> HARNESS_PLUGINS
    HARNESS_CONFIG_ISOLATION -. prerequisite .-> HARNESS_PROMPT_SUGGESTIONS
    HARNESS_CONFIG_ISOLATION -. prerequisite .-> DT
    THREAD_EVENT_CONTINUITY --> THREAD_SUCCESSOR_DELIVERY
    CLAUDE_RECOVERY -. native continuation evidence .-> THREAD_SUCCESSOR_DELIVERY
    CODEX_RECOVERY -. native continuation evidence .-> THREAD_SUCCESSOR_DELIVERY
    CODEX_RECOVERY -. required recovery evidence .-> CODEX_RECOVERY_PROTOCOL
    COMMAND_QUEUE_DECISION -. if app-first acceptance chosen .-> THREAD_COMMAND_DELIVERY
    THREAD_COMMAND_DELIVERY --> THREAD_OUTBOX_CUTOVER
    THREAD_OUTBOX_CUTOVER --> NEWTHREAD_DURABLE
    NEWTHREAD_DURABLE --> UISHELL_NEWTHREAD_SANDBOX
    NEWTHREAD_DURABLE --> UISHELL_NEWTHREAD_LANDING

    ACCESS -. authority choice .-> EGRESS_CHANGE
    ACCESS -. Kubernetes authority and credential choices .-> SANDBOX_RBAC
    SANDBOX_RBAC -. subject generalization .-> MANAGED_SA_RBAC
    KUBERNETES_RBAC_POLICIES -. reusable policy contract .-> MANAGED_SA_RBAC
    MANAGED_SA_RBAC -. third grant kind to render .-> CALLER_GRANT_VIEW
    CLAUDE_AI_SA -. one account by hand, then the kind .-> MANAGED_SA_RBAC
```

The Thread correctness/UI track is independent of Action Service milestones.
Its authority and failure contracts are in
[Thread, runner, and harness layering](../docs/thread_layering.md).
The current slice uses the runner's command queue: make runner admission, app archival,
pending-command UI, and reconnect reliable. Accepting commands while no runner is
available is deferred under `COMMAND_QUEUE_DECISION`, along with the app outbox and
combined-start workflow. Normal replay and controls do not depend on that later choice.

The graph's edges gate integrated acceptance, not starting an independently reviewable
change. UI reducers and visual cases can proceed against established Events while
native recovery research runs. Automatic recovery is gated separately for each harness
and operation by its evidence; do not claim it from a working ordinary command path.

**Proposed next dispatch wave:** finish deployed relay verification and operator-login
acceptance, then use two independent lanes: conversation UI fixes and one narrowly scoped
native-evidence gap at a time. The coordinating agent owns deployed acceptance and landing/CI.
Native evidence does not block independent UI work.
Reuse existing agent worktrees. Each self-contained change gets its own PR against
`devel`; stack only on required implementation content and remove completed tasks as
their work lands.

The combined start composers require `NEWTHREAD_DURABLE` before promising “submit
and walk away.” A browser-owned provisioning chain is not a correct intermediate
version of that promise. Manual Sandbox creation and explicit open/resume remain
available while combined start is deferred.

### `EGRESS_CHANGE` — agent-requested egress policy expansion

**Deferred design:** define how an agent can request an expansion or change to its egress rules.
The request may become a policy-gated Action with operator approval, or use another reviewed
configuration path. Keep the authority, approval, persistence, and rollback model open until a
concrete caller and policy owner are chosen. This does not grant agents a direct policy mutation
path and does not block current credential-placeholder egress.

### `CONNECTION_SA_REBIND` — rebind a Connection's ServiceAccount in place

**Planned mutation:** no mutation exists today, frontend or backend, to change which ServiceAccount
an existing Connection acts as. `connections.py`'s `ConnectionAuthority` and the exposed
`ConnectionService` (`list`/`callerServiceAccounts`/`rename`/`unbind` in `client.ts`) cover listing,
renaming, and unbinding, but the only way to change a Connection's bound ServiceAccount is a fresh
OAuth consent authorization (`consent.tsx`) that creates a new grant — a new revision, prior grants
revoked, not an in-place edit. The frontend's OAuth-clients settings table currently shows the
ServiceAccount as read-only text for exactly this reason.

**Design questions, not yet settled:** should rebinding revoke the prior grant's revision the same
way a fresh consent does, or coexist with it; does it need its own audit trail distinct from a
reconnect; and does it require re-running eligibility checks (the ServiceAccount must still carry
`agentplane.allegedly.works/use-action-service: "true"`) at rebind time, not just at original consent.
No dependency on anything else; nothing waits on this. Once it exists, the settings table's
ServiceAccount column becomes a real dropdown instead of static text.

### `SANDBOX_RBAC` — verify deployed Sandbox Kubernetes grants

**Remaining acceptance:** exercise two managed Haku
Sandboxes and an unrelated one through real `kubectl`/sidecar requests; verify Role
rule edits affect both existing bindings, external OAuth Haku's MCP sandbox reports its
actual static SA, and only intended runners can read the Coinbase Secret. Verify
deletion cleanup and credential use without recording the key. The exact sequence is
in [agent RBAC](../../cluster/docs/agent_rbac.md). Runtime grant editing and revocation
remain later work.

### `CLAUDE_AI_SA` — the claude.ai account's deliberate permissions and egress

**Remaining authority review:** `agentplane-staging/claude-ai` is both the Claude.ai MCP
Connection's principal and the ServiceAccount used by Sandboxes it creates. Review its current
access for that arbitrary-shell surface before adding grants. The
[staging policy source](../../cluster/cdk8s/agentplane/actions_staging_policies.py),
[Agent RBAC documentation](../../cluster/docs/agent_rbac.md), and
[Haku wiring notes](../../haku/TODO.md) hold the current grant inventory and credential details.

Decide which Kubernetes Roles beyond the diagnostics reader this account needs and at what scope.
Preserve its existing Pod-bound identity as the authority boundary. Also decide whether its
Kubernetes egress rule should remain all-verbs/all-paths once RBAC bounds access, and whether GitHub
reads approved for its Connection should also be available to its Sandboxes; the egress and Action
policy bindings are separate grants and can drift.

**Acceptance:** a stated, reviewed authority for the account, rendered by the generator rather than
accumulated; a real API request from inside a sandbox succeeds for the intended operations and is
refused outside them; and removing the account or its label still disables the whole path.

### `KUBERNETES_RBAC_POLICIES` — reusable Kubernetes RBAC policies

**Deferred design:** decide whether Agentplane should let one named Kubernetes access policy be
bound to many Sandboxes or managed ServiceAccounts, so changing that policy updates every existing
binding. A preset such as `public-coder` should be selectable as one policy in Sandbox creation,
with its detailed permissions inspectable elsewhere, rather than presenting a long list of repeated
namespace-level metadata and log grants.

The deployment-side `cluster/cdk8s/agent_access_profiles.py` already factors static identity and
preset selections, but those profiles resolve to individual catalog grant names; they are not
independently managed runtime policies. Each Sandbox still stores its expanded grant selections
and gets its own RoleBinding or ClusterRoleBinding objects. The pain is the repeated per-Sandbox,
per-scope bindings and UI choices, not a claim that every underlying Role's rules are independently
authored for every Sandbox.

**Questions, not yet settled:** should a policy be a named bundle of Kubernetes Role/ClusterRole
references, an Agentplane-owned rule set compiled into those native objects, or another explicit
model? Kubernetes RoleBindings are namespace-scoped while ClusterRoleBindings are cluster-scoped;
work out how a multi-namespace policy expands without broadening authority, and who owns, reconciles,
updates, audits, rolls back, and cleans up those objects. Determine how edits reach existing
Sandboxes, how migration from persisted grant lists works, and how the UI shows the policy name
while still exposing its effective scope and permissions. Keep Flux ownership and other RBAC
reconcilers from fighting Agentplane over the same objects.

This is a Kubernetes-specific representation and lifecycle question. It does not settle or require
the broader cross-cutting capability profile in `PROFILES`, and it does not choose to replace
Kubernetes RBAC as the enforcement model. No storage/API shape or migration is decided here.

### `CALLER_GRANT_VIEW` — one grant view for Sandboxes and unmanaged agents

**Remaining UI and API work:** expose each managed ServiceAccount's Action policy and egress
bindings by account, then let an operator inspect them from a caller roster even when the account
has no Sandbox. The account-keyed read helpers and caller roster already exist; this needs the
missing app routes and joined grant view, plus the Kubernetes view when `MANAGED_SA_RBAC` lands.

**Settled intent:** Sandboxes and unmanaged agents share one Action policy view rather than each
getting their own, and share the Kubernetes permissions view too once `MANAGED_SA_RBAC` provides one.
The subject is the account in every case, so what differs between the two is what else the page can
say about the subject -- a Sandbox has a Pod, a state and a template; an unmanaged agent has a
Connection -- not how its grants are read or rendered.

**Questions, not yet settled:** whether the shared view is a per-account page or one roster table
with grant columns; and whether the roster is the `CALLER_LABEL` set or every account the app
manages, which differ exactly for an account whose label was removed to cut it off while its
bindings still exist.

### `MANAGED_SA_RBAC` — Kubernetes RoleBindings as a managed grant kind

**Planned Kubernetes access:** grant and inspect Kubernetes RoleBindings on the ServiceAccounts the
app manages the way it already does egress policies and Action policy sets -- so an agent can be
allowed to talk to the apiserver by a grant on its account, rather than through a governed Action or
a hand-written manifest.

`SANDBOX_RBAC` is the Sandbox-scoped form of this, reached through launch fields and presets. The
generalization is the subject: a caller with no Sandbox, such as an OAuth-bound externally hosted
agent, has an account and can hold bindings, but no Sandbox to hang them off. That difference is the
design question rather than a detail -- a Sandbox's grants are garbage-collected by an
`ownerReference` on the Sandbox, and an account that outlives every Sandbox has no such owner, so
what creates, owns and reclaims its RoleBindings is unsettled.

**Further questions, not yet settled:** whether expiry works as it does for the other two kinds,
given that Kubernetes RBAC has no expiry of its own and something must sweep; and whether this
shares `ACCESS`'s credential boundary or only its authority decisions. The reusable policy and
Role-versus-bundle representation belongs to `KUBERNETES_RBAC_POLICIES`. Respect existing GitOps
ownership: an account's bindings must not fight a reconciler for the same objects.

## Named gates and acceptance evidence

### Unranked future harness capabilities

These candidates describe native features currently disabled, narrowed, or not surfaced by
Agentplane's hosted adapters. The `DT` row is the existing P2 item; other rows are unranked. Their
value and effort estimates are rough planning inputs, not a ranking. The harness protocol roster
records the current switches and uncovered wire surfaces
([protocol roster](../native/docs/protocol_roster.md)). Keep the test capture profile deterministic;
feature enablement belongs in an isolated hosted profile, not by widening every capture. Rows are
ordered by identifier only. For Claude-specific behavior, start from the partial Claude Code
reverse engineering in the sibling `gaffer-private` repository and extend it where a candidate
needs evidence that work does not already provide.

- **`DT` (existing P2) — high win, medium–high work:** Surface Action-backed tools through
  Claude driver MCP and Codex dynamic tools, plus the existing list/status and per-task stop floor
  for background work. Reuse Action Service decisions, execution, and idempotency; it
  remains deferred pending a named consumer. See [driver tools and background work](driver_tools_and_background.md).
- **`CLAUDE_REMOTE_IO_EVAL` — unranked decision:** Compare Claude RemoteIO with `stream-json`,
  prove the scoped egress interception and runner-owned bridge, and measure compaction-summary
  visibility before choosing an implementation. See [Claude RemoteIO transport](claude_remote_io.md).
- **`HARNESS_MCP_TASKS_EVAL` — unranked capability reading project:** Read the current MCP
  Tasks protocol and the pinned Claude CLI and Codex client implementations (and verify with
  native captures where available). Determine which clients actually negotiate task-capable
  tool calls, obtain a durable task ID, inspect/follow results, cancel, and recover across
  disconnects; identify transport/version or UI gaps. Compare the Task lifecycle with Action
  Service's durable request/receipt/events semantics before recommending an integration.
  Distinguish MCP Tasks from Claude subagents, Codex multi-agent features, and FastMCP's
  internal Python task machinery. Do not assume either harness supports Tasks or treat this
  investigation as a fix for direct-tool response loss.
- **`HARNESS_INTERACTIVE_CONTROLS` — high win, high work:** Support questions and permission
  requests that park a turn, survive reconnect/restart, accept or reject a durable decision, and
  resume safely. Keep user decisions distinct from Action Service authorization.
- **`HARNESS_PLUGINS` — medium win, high work:** Enable project plugin/skill packages with
  explicit source trust and capability grants. Define install/update behavior and test that plugin
  tools cannot exceed the Thread's authority.
- **`HARNESS_PROJECT_HOOKS` — medium win, high work:** Run project hooks only behind an explicit
  trust boundary; support bounded execution, hook replies, timeout/cancellation, and evidence in
  the Thread. Do not inherit arbitrary host hooks.
- **`HARNESS_PROMPT_SUGGESTIONS` — low win, low work:** Consider Claude's prompt suggestions only
  if user research shows meaningful composer value; keep optional and avoid extra model work
  without evidence.
- **`HARNESS_SKILLS` — high win, low–medium work:** Enable project-scoped skills and custom
  commands for Claude and Codex. Prove the project catalog is available in a Thread while
  host-global settings and unrelated user configuration stay out.
- **`THREAD_UPLOADS` — unranked candidate:** Allow a user to upload files and images for an
  agent to inspect, bound to an authorized Thread rather than an arbitrary Sandbox path. Design
  size/type limits, durable bytes and scoped references, safe workspace materialization or
  harness attachment, and cleanup/retention across restart and deletion. Expose the attachment
  on a user message with replayable provenance; do not mistake a filename or a preview for bytes
  the agent actually received. Test reconnect, duplicate submission, cross-Thread access denial,
  missing Sandbox, and both harnesses. Browser-provided images additionally require
  `HARNESS_VISUAL_INPUT`; that node's workspace `view_image` fixture is a different input path.
- **`HARNESS_VISUAL_INPUT` — high win, high work:** Carry supported image input/viewing through the
  harness protocol and retained Thread history; prove Claude's image input path and Codex viewing
  an image already in its workspace. Text-only transcripts are insufficient acceptance. For Codex,
  keep workspace `view_image` distinct from user composer attachments: the completed app-server
  item is `{type: "imageView", id, path}`, with no `status` or image bytes. A completed
  `commandExecution` instead carries `status` plus fields such as `aggregatedOutput`, `exitCode`,
  and `durationMs`; the current generic adapter would misclassify `imageView` as unsuccessful unless
  it models this item explicitly. Test with a fixture image in the Codex workspace and a scripted
  `view_image` call; assert the following model request contains that image as `input_image`, and
  that Agentplane retains/projects the `imageView` lifecycle and path. This proves image transport,
  not visual understanding; use a live model check for semantic recognition. The JSON item alone
  cannot render a preview in the UI: that needs the runner to retain or transfer the viewed bytes
  through an authorized media reference for replay and rendering.
- **`HARNESS_WEB_SEARCH` — high win, medium work:** Route native search through an approved,
  observable egress path; retain source evidence and links in the Thread. Confirm provider/tool
  availability before wiring either harness.
- **`NATIVE_SUBAGENT_THREADS` — high win, high work:** Enable Claude Task and Codex multi-agent
  features; discover child identity, output, completion, and restart/resume, then expose linked
  child Threads with parent/child provenance and reconnect deduplication. Start with read-only
  inspection; child control is a separate evidence-gated extension.

#### `HARNESS_CONFIG_ISOLATION` — separate hosted features from capture configuration

**Unranked prerequisite:** production adapters currently reuse narrow scenario launch/config
helpers. Separate deterministic capture settings from an explicit hosted feature profile before
turning on native capabilities. Prove that each Thread receives only its selected Agentplane
configuration, project-scoped settings stay inside the workspace boundary, and machine/user-global
configuration cannot silently expand tools or permissions. Keep feature choices independently
switchable so each candidate can be enabled and accepted on its own. This is a harness-configuration
boundary, separate from the deferred product capability profile in [Profiles](profiles.md).

#### `NATIVE_SUBAGENT_THREADS` — adopt harness-native subagents as Threads

The [characterization matrix](native_session_discovery.md#matrix) gates the shared session model. Passing
scripted tests cover child creation/tool/completion and Claude continuation through `SendMessage`.
The same plan’s [deferred design](native_session_discovery.md#proposed-shared-design) sketches runner-owned
read-only discovery and its identity, attribution, and durability gates. Discovery is not implemented;
independent control and native restart reconciliation remain evidence-gated follow-ups.

**Unranked candidate:** when Claude or Codex starts a native subagent, discover its native identity
and available transcript/events and expose it as a linked child Thread in Agentplane. Verify
creation, output, completion, and restart/resume from separate harness evidence; do not infer child
messages from the parent's tool summary. Preserve native frames and parent/child provenance, and
deduplicate rediscovery across reconnects and restarts.

Decide the mapping to Sandbox, Thread, and harness incarnation before implementation. Adoption is
not spawning another Agentplane-managed harness or claiming the parent's command receipts for a
child. Read-only inspection may be the first useful slice; independent input, interrupt, model
control, and resume are separate evidence-gated capabilities. This has no dependency on the current
delivery/UI batch.

### `ELEVATE` — agent-requested temporary permission

**Planned behavior:** a caller that knows it will need an Action outside its current policy asks
for it through an Action of its own: the request names the set to add (or the binding to remove),
the subject (its own ServiceAccount or Sandbox; never another caller), and an `expiresAt`. The
operator sees it rendered as that specific request, with the set's lists shown, not as a generic
approval card. Approval writes the `ActionPolicyBinding` with the requested expiry through the same
authority the app uses; denial writes nothing. Both caller classes get this: a ServiceAccount-bound
OAuth client and a harness in a Sandbox.

**Design gate / acceptance:** the requesting Action is an ordinary Action with an ordinary Decision,
so the request itself can be auto-approved by policy later, never by default. A granted binding is
evaluated like any other, once per subsequent Action at admission. Prove: a Sandbox requests a set,
the operator approves, the next matching Action auto-approves and the Decision names the new
binding; the same request from a different subject grants nothing to the requester; expiry ends it.

### `T3` — thread search and lookup

**Deferred product work:** search and look up stored threads at a later product-planning point.
This is technically independent of the MCP facade, but it is intentionally not in the current work
sequence. Existing transcript persistence and unrelated lifecycle reliability work are not
reclassified as search implementation by this deferral.

### `ACCESS` — delegated versus brokered external access

**Deferred design:** choose per-system whether an Action uses the Agent's delegated identity, a
brokered operator credential, or a hybrid. Keep target-side RBAC and egress enforcement authoritative;
use grants/revocation reconciliation where a broker mints delegated authority. This is the broader
external-access policy behind the operator-linked providers and the SSH MCP server, not a prerequisite for the completed credentialless MCP vertical.
The [external-access design](external_access.md) is the source of truth for these choices;
its [Kubernetes decisions](external_access.md#kubernetes-sandbox-access-decisions) also
cover `SANDBOX_RBAC`, separately from that task's Sandbox/preset UI and lifecycle wiring.
Evaluate existing authorization engines/protocols and possible hybrids against the concrete
[GitHub, Kubernetes, and HTTP compatibility probes](external_access.md#compatibility-evaluation-github-kubernetes-http)
before selecting a shared decision service; protocol reuse does not settle grant or credential ownership.

**Acceptance evidence:** a selected system proves the credential boundary, approval behavior, and
revocation/expiry semantics without putting a reusable privileged credential in the harness.

### `BINDING_SUBJECT_ARITY` — one subject shape across both binding kinds

**Planned schema:** `EgressBinding.spec.subjects` is an array (`minItems: 1`); `ActionPolicyBinding`
names one `subject`. Everything inside them is now the same `ServiceAccountRef`, so arity is the
only difference left, and `agentplane/crds/generate.py` has to special-case array-versus-object
to splice it in.

Nothing writes the plural side. The one checked-in `EgressBinding` (staging's `claude-ai`) names a
single subject and `EgressInventory.grant` writes exactly one entry, so the multi-subject shape is
an untested degree of freedom in the authorization path. Collapsing it to a singular `subject` makes
the two CRDs identical rather than merely compatible; the cost is that a seed granting several
accounts one policy becomes several objects, which is already what per-binding `expiresAt` wants.

Its own clock, and cheaper before something starts using it than after.

### `PC_EGRESS_CREDENTIALS` — label public-coder's Action Service caller

**Remaining configuration:** add
`agentplane.allegedly.works/use-action-service: "true"` to the existing `openclaw` ServiceAccount.
The Deployment already uses that account; its egress sidecar, binding, and credential substitutions
are configured.

The label admits the account only to an Action Service instance that watches the
`public-coder-agent` namespace. Namespace admission and ActionPolicyBindings belong with that
instance; the egress production cutover and behavior proof remain in `PC_EGRESS`.

### `PC_EGRESS` — public-coder-agent egress migration

**Remaining migration:** deploy a dedicated production Agentplane instance that watches
`public-coder-agent`, configure its egress credentials and reviewed Action policies, then point the
OpenClaw sidecar at it. The checked-in Deployment sets `replicas=0`; verify live state and arrange
a resumed or equivalent controlled deployment for real-agent acceptance.
Compare effective destinations, credential substitution, Action behavior, denied traffic, and
rollout/restart behavior with the existing proxy before a reversible cutover. Compare the
production Action catalog and policies with the current Haku Console grant path as well. Do not
widen the current egress or Action contract.

After acceptance, remove only OpenClaw-specific proxy dependencies and Console credential wiring.
Keep the Iron path used by the devbox and shared `iron-proxy` resources. The separate
`haku-egress-proxy` still serves Haku Sandboxes, Haku CI, and another OpenClaw workload; confirm
current consumers before removing or splitting any shared resource. Source inventory starts at
[`public_coder/proxy.py`](../../cluster/cdk8s/public_coder/proxy.py) and
[`haku_egress_proxy.py`](../../cluster/cdk8s/haku_egress_proxy.py).

### `ANTHROPIC_INCLUDED_API_ROUTING` — use subscription-linked Anthropic API credit

**Unranked future integration:** verify the actual eligibility, amount, expiry, API credential
mechanism, and terms for Anthropic's subscription-linked API credit before wiring a model route
to it. Use an authorized credential owner and scoped egress/proxy configuration; do not substitute
a Claude Code subscription OAuth usage token for an API credential without explicit support.
Prove which API calls draw down this credit, how routing/fallback behaves when exhausted, and
how paid spend is bounded. Keep the decision to use the credit independent from the quota UI.

### `AIQUOTA_ANTHROPIC_API_CREDIT` — show API credit in aiquota

**Unranked reporting:** read an authoritative Anthropic API credit/balance/usage surface if
available, through an authorized read-only credential path. Distinguish included API credit,
paid API spend, and aiquota's existing Claude subscription usage windows and extra spend; do
not present one as the other. Preserve units, reset/expiry, unavailable and stale states, and
historical observations without logging credentials. Test changes in balance and the limit
boundary. This can be investigated independently of the model-route integration.

### `INPUT_DELIVERY` — remaining native queue and recovery evidence

**Remaining evidence:** close the harness-specific queue and recovery gaps, independently of
live Action/MCP acceptance. The admission/effect outcomes and app presentation are in
[Thread, runner, and harness layering](../docs/thread_layering.md#command-protocol-intent-admission-then-outcome).
Do not rebuild the ordinary command/reconnect path or repeat already-pinned scenarios.
Claude fresh-process resume framing is under investigation in
[#6996](https://github.com/agentydragon/ducktape/pull/6996); it is not recovery support.
Long-running Bash interruption, partial output, and queued-message fate are tracked in
[#6807](https://github.com/agentydragon/ducktape/issues/6807). Reuse native tests and the
sibling `gaffer-private` checkout; new evidence gates only the operations it proves.

- Claude: UUID command handles, lifecycle receipts, capability negotiation, targeted withdrawal,
  interrupt receipts/queued survivors, and coalescing that can make cancellation batch-granular.
  Do not interpret an ambiguous `cancelled:false` as a guaranteed no-op or fabricate receipts.
- Codex: distinguish joined input in the active turn's in-memory pending list from the separate
  experimental durable `thread/queue/*` API. Examine queue promotion, dispatch/delete locking,
  interruption, and correlated user-message evidence; an RPC acknowledgement is not consumption
  or completion, and a queue-changed notification alone does not prove promotion.

**Observed evidence boundary:** some queue behavior comes from declarations/static analysis, not
capture-pinned tests. Refresh live probes/captures against both pinned binaries for the behavior
being adopted before changing the common protocol; then encode supported behavior in scripted
native-harness CI tests against the controlled model endpoint. Missing capabilities and blocked
experiments stay explicit, not normalized into success. This research may justify common-protocol
changes, but it does not preselect a queue facade, selective cancellation, or a new persistence layer.

**Acceptance evidence:** exercise disconnect before delivery, delivery before observed admission,
reconnect/replay with the same command id, and restart. Prove duplicate-ID handling at each actual
boundary rather than assuming native idempotency. Include Claude coalescing/interrupt/withdrawal
and Codex join-versus-durable-queue cases, preserving raw native evidence and harness differences.
In both harnesses, queue several inputs, interrupt before native message confirmation, and prove
each input is confirmed, terminally dropped/no-op, or later confirmed; none may remain indefinitely
admitted without a terminal outcome. No acknowledgement, retry, steering, cancellation, or completion may be invented by the
runner. Keep unsupported operations native or explicitly unavailable. **Deferred:** generic queue
management and unproven per-input cancellation.

### `THREAD_OPEN_RELOAD_RECOVERY` — find a committed Open without a Thread mapping

**Failure window:** the runner may commit Open/Attach, but the app may lose the reply or fail
before committing the `(sandbox, session_id) → Thread` mapping. The browser can check
that mapping by its stable session ID, but after a reload it retains only the ID, not
the original spec and setup script. "No Thread yet" therefore means **unknown**, not
"no session"; retrying with freshly assembled defaults or a new ID can create a
second or conflicting session.

**Design and acceptance:** choose an authorized way to inspect the runner's retained
session and adopt/reconcile the exact Open (or explicitly report that it cannot be
recovered), without trusting a guessed ID or persisting secrets in browser storage.
Scope reads to the caller's Sandbox and distinguish a current Sandbox UID from an
old/deleted one. Exercise reply loss before and after runner commit and before and
after app mapping commit, reload, changed spec/script, missing Sandbox, and two app
replicas. Do not infer that a session never existed from an absent mapping.

### `BOOTSTRAP_PROGRESS_CONTRACT` — do not hold Open through script execution

Sandbox Service currently awaits the runner's `Initialize` _terminal result_ for
the Sandbox binding's bootstrap script **before** Open/Attach. The runner retains
script identity, ordered output, and result, but Sandbox Service has no independent
authorized bootstrap start/status/progress API. Thus the Open RPC can remain open
for the duration of a long script despite no longer waiting for the app archive.

**Decided invariant: one bootstrap attempt per Sandbox, never a retry mechanism.**
Today `Initialize` replays success but an identical later call re-executes a failed
or interrupted script; change that behavior before an asynchronous reconciler can
call it safely. A repeat must report the retained result/state, including failure or
interruption, without launching another process. On runner restart, persist an honest
interrupted/unknown outcome for an attempt that has no terminal result; do not invent
success/failure or restart it. To run a different initialization, create a distinct
Sandbox rather than retrying inside the old one. Pin tests for failure, lost response,
reconnect, and runner restart.

Specify a durable start receipt and scoped progress/result reads or feed without
making Open wait for the script; keep successful bootstrap as an Open precondition
unless a separately reviewed contract changes that. Separate the configured
launch/RPC deadlines from the acceptance promise. Per-session setup progress is not this Sandbox initialization.

### `SANDBOX_CREATE_RECONCILE` — lost lifecycle response and partial provisioning

Create currently mints a _new random suffix_ from the caller's slug inside
`SandboxInventory.create`, before creating a same-name ServiceAccount and the Sandbox CR.
If the reply is lost, the caller does not know the name, so Get by name cannot recover
it and a retry can create a second Sandbox. Create may also commit the CR then fail
while provisioning grants; Kubernetes intent is not Pod readiness.

**Implementation and acceptance:** give each Create a caller-retained stable identity and
known target name before the request is sent; retry must use that same name and exact
choices. Record the identity and normalized intent on the CR and verify both and
caller authorization before treating an existing object as success; mismatches are
conflicts, not adoption. Handle existing/in-flight same-name ServiceAccounts and
ambiguous Kubernetes writes without deleting an SA if its CR may have committed.
The existing provisioning-intent annotation and reconciler should finish incomplete
grants rather than creating another Sandbox. Exercise reply loss at SA creation,
CR commit, owner-reference patch, and grant provisioning; concurrent retries,
conflicts, restart, and deletion/recreation under the same name. Compare current
UIDs to distinguish replacement; Kubernetes has no get-by-UID or retained tombstone,
so absence after deletion remains unknown without a separate durable request ledger.
Do not promise exactly-once across deletion from a name lookup alone.

### `COMMAND_QUEUE_DECISION` — where submission becomes durable

**Deferred beyond this slice:** [queue placement](../docs/thread_layering.md#queue-placement-decision)
compares runner admission first with accepting commands in an app outbox before the
runner is reachable. Build the runner-first path now. Revisit app-first acceptance
when its additional availability promise is needed.

Keep #6625's outbox and #6985's mixed Thread-record design outside the current merge
sequence. Review them for independently useful changes to salvage into appropriate
slices; do not stack new work on their deferred queue design. Preserve the runner's
own journal in either option.

### `COMMAND_PROGRESS_WIDGET` — shared progress for in-flight commands

**Unranked future UI improvement:** show one compact, accessible progress treatment for
pending user input, model changes, and reasoning-effort changes. Reuse the same widget
beside an input bubble or command card; include the requested model or effort in the
command card. Do not create a parallel progress log or duplicate a command when its
local copy becomes a projected history row.

Advance only through observable states: retained locally (including admission
unconfirmed after a lost reply), runner admission confirmed by `CommandAdmitted`, and
the operation-specific effect or terminal failure/no-op. Input confirmation,
`ModelChanged`, and `ReasoningEffortChanged` settle their respective commands; a
model or effort change may remain pending until a native selection confirms it.
Neither the HTTP reply alone nor the runner's internal `dispatch_planned` flag proves
the harness received the command; no dispatch Event or protocol change is needed.
Stop the spinner on terminal outcomes while preserving failure/no-op explanations.

Acceptance: exercise the shared state transitions, retry/reconnect and reload from
local plus projected state, commands sent from another browser, and mobile/desktop
renderings. Keep the stage names accessible without pinning incidental wording.

### `CLAUDE_RECOVERY` — native execution before durable runner evidence

**Evidence first, then runner implementation:** pin the uncovered crash window where
Claude has acted on input but the runner has not durably recorded its outcome. Test
the exact mocked-LLM requests and native correlation/history available after resume.
Existing interrupt/queued-input evidence is a starting point, not proof of this window.
Use the Claude RE when needed, with additional RE work in its own PR.

Land a focused native-behavior PR, then runner recovery changes justified by that
evidence, with real-process crash tests. Prove duplicate-input prevention and original
command provenance; gate unsupported automatic recovery rather than inventing an
outcome. Follow [the command recovery contract](../docs/thread_layering.md#command-protocol-intent-admission-then-outcome).

### `CODEX_RECOVERY` — native execution before durable runner evidence

**Evidence first, then runner implementation:** pin Codex's corresponding crash window,
including what `turn/start` acceptance, native persisted history, and subsequent resume
can establish. Assert exact mocked-LLM input contents and duplicate behavior. Keep
Codex scenarios separate where its native semantics differ from Claude's.

Land the native evidence and resulting runner changes as independently reviewable
PRs, with real-process crash tests preserving command provenance. The same
[recovery contract](../docs/thread_layering.md#command-protocol-intent-admission-then-outcome)
applies; neither harness waits for the other's research to land its own proven change.

### `CODEX_RECOVERY_PROTOCOL` — reconcile through documented app-server history APIs

**Remaining migration:** replace Agentplane's private rollout parsing with the public Codex
history APIs. At implementation time, first update the runner's Codex CLI pin and native protocol
fixtures to the newest release selected for the runner; target that protocol rather than maintaining
a compatibility matrix for older CLI versions. Evaluate metadata-only `thread/read` followed by
`thread/timeline/list`, `thread/turns/list`, and `thread/items/list`. In the runner-matched 0.157.0
protocol, the unified timeline API is experimental, while turns/items pagination are not marked
experimental in the request registry even though the public docs classify them that way; item
pagination is also history-store-dependent. Before implementation, refresh these details against the
release selected for the runner and verify its negotiation requirements and store support. Determine
whether the returned fields support the existing retained, absent, revised, and unknown decisions,
including interrupted tool and reasoning items. The [API investigation](../debug/codex_app_server_history_apis.md)
records the current protocol baseline and migration constraints. History items are Codex's persisted
display projection, so an item missing from a page does not prove it is absent from raw model context
or rollout records. Preserve command provenance and never replay old side effects. Keep outcomes
unknown where the public protocol cannot establish them, and record any upstream protocol gap
instead of silently depending on internal rollout details.

Agentplane still reads Codex's persisted history for reconciliation. `excludeTurns` only avoids
returning the full transcript in a resume response; it does not replace this migration.

### Fresh-process native resume and prefix-cache spikes

These are separate **evidence questions per harness**, not automatic-recovery
implementations. Extend ordinary Bazel targets in
`agentplane/harness_tests/claude/test_turns.py` and
`agentplane/harness_tests/codex/test_turns.py`: both already resume a real pinned
harness in a fresh process against a mock model endpoint, and Codex asserts retained
encrypted reasoning and prompt-cache key. Use `agentplane/runner/test_restart.py` for
the runner-level writer/journal handoff when needed. No Sandbox Pod-level acceptance
test is required for this native-state question; the existing
`agentplane/acceptance/test_suspend_resume.py` tests same-volume lifecycle behavior,
not transfer to new storage.

For each harness, add a copied-state variant with an isolated new home/workspace and
capture the model request at the mock endpoint. Use a controlled, non-sensitive
multi-turn prefix with tool interactions and reasoning items where exposed. Stop the
old process, copy only its documented or observed native session artifacts to the
isolated test workspace, and issue native resume plus one new turn. Compare the
original and resumed model requests against each provider's current
documented cache-eligibility rules: model and relevant settings, system/tools/messages
and their ordering, cache boundaries/controls, cache key where applicable, and
opaque/cacheable reasoning state where exposed. Assert that the resumed request retains
the eligible prior prefix (apart from the new turn); compare with same-process and cold
start controls. Extend the mock request capture for cache-relevant headers if needed.
A mock provider cannot report a real cache hit, but deterministic request-shape
assertions are the acceptance criterion here; do not require a live-provider usage
probe or treat cache misses from one real invocation as proof of a changed prefix.
Record byte/structural differences and the exact artifact inventory rather than
inferring eligibility from a rendered transcript. Do not manufacture old reasoning
content from an app Event archive.

- **`CLAUDE_FRESH_RESUME_CACHE_SPIKE`:** test Claude Code's native saved-session resume
  into a fresh CLI process with both unchanged local state and a copied-state target.
  Pin whether its continued request carries the same cacheable user/assistant/tool and
  reasoning prefix and cache boundary/controls under documented eligibility rules.
- **`CODEX_FRESH_RESUME_CACHE_SPIKE`:** test Codex app-server's native thread resume in
  a fresh process with the same two state placements. Pin which persisted history and
  reasoning/opaque items its continued request uses and whether the prompt-cache key
  and prefix remain eligible. Do not infer equivalence from a successful
  `thread/resume` reply.

A changed cacheable prefix, lost reasoning continuity, or missing eligibility signals
is a **finding** for that harness, not a license to invent replay or bulldoze past a
native limitation. If documented cache eligibility cannot be demonstrated, gate the
corresponding on-demand suspend/resume feature and retain a running/retained-volume
option until viable native support exists. Neither harness's result blocks the other's
investigation.

### `SANDBOX_LIFECYCLE_DURABILITY` — preserve state through suspension and deletion

**Planned lifecycle correctness:** verify the actual runner state mount, native artifacts,
graceful shutdown, and explicit native resume across Pod replacement. Implement
[archive-before-deletion](../docs/thread_layering.md#storage-loss-and-sandbox-lifecycle)
with quiesced writers and the final runner prefix copied before managed storage removal.

Acceptance covers suspended/resumed Sandboxes, app downtime during suspension,
unreachable runners at deletion, and incomplete recovery state. Storage inspection
and native evidence can proceed independently; full archive-preservation acceptance
requires Event durability and app replication. Gate lifecycle automation on its own
evidence without blocking ordinary messaging and UI work.

### `RUNNER_IMAGE_UPGRADE_PROOF` — same-storage image replacement evidence

First test the existing Sandbox CR/PVC lifecycle without claiming a supported API:
quiesce/fence the runner, pause the Sandbox, patch its stored `podTemplate` image,
resume on the **same storage**, and check Claude and Codex native state, journal
prefix/cursors and pending-command behavior. Exercise an incompatible image and
rollback. Pin whether the controller actually replaces the Pod and preserves the
state mount; failure is a finding for the eventual workflow, not permission to
reconstruct native sessions from app history.

### `RUNNER_IMAGE_ROLLOUT` — upgrade runner images on existing Sandboxes

**Planned operator workflow:** a runner image version is recorded in each Sandbox CR's
`podTemplate`, so updating the default image only affects newly created Sandboxes. Define
and implement a supported way to move an existing Thread to a fixed runner image while
preserving its Sandbox storage and native session state. After
`RUNNER_IMAGE_UPGRADE_PROOF`, make the tested pause/patch/resume sequence a
supported operator workflow rather than relying on hand-editing CRs.
Specify writer fencing, interruption handling, and rollback, then verify that an existing
Thread resumes on the fixed image without losing state or repeating side effects.
This is a prerequisite for a live `THREAD_EVENT_CONTINUITY` runner/protocol cutover
that keeps existing Threads resumable; it does not require portable state or renaming
their runner storage. A brief pause is acceptable, not an untested zero-downtime handoff.

### `SANDBOX_VM_ISOLATION` — selectable VM-backed Sandbox isolation

**Deferred provider integration:** the pinned-stack prototype and guest acceptance are complete
([platform evidence](../debug/kubevirt/evidence.md),
[guest runtime acceptance](../debug/kubevirt/runtime-20261003.md)). They do not add a KubeVirt
provider to the production Sandbox Service or exercise the production egress gateway.

Implement the creation-time `agent_sandbox` / `kubevirt` choice and provider lifecycle in the
Sandbox Service, API, app, and deployment; publish and run the integrated guest image through
the production egress path. Complete the remaining admission-failure, guest resource-boundary,
and lifecycle/recovery acceptance in the [KubeVirt environment plan](kubevirt_environments.md).
No existing-environment conversion or live migration is implied. This remains independent of
current container correctness work and ordinary Sandbox Service extraction.

### `THREAD_IDENTITY_NEW` — assign one canonical ID to new histories

Have the creator/Open path use one canonical UUID for a newly created logical Session
and its Thread view, with the runner using that ID for its new storage. The app must
not mint a second public Event-log identity on first sight. Test lost Open replies,
retries and first Event ingestion; this node does not change legacy runner IDs or
require copying a harness's state to new storage.

### `THREAD_EVENT_CONTINUITY` — identity/storage cutover capstone

Depends on new-ID creation, the imported legacy association in the new archive,
and the proven supported runner image rollout. Do not claim a live cutover complete
until existing Threads resume on a compatible image with the same native state and
Event prefix; these pieces can be built and reviewed independently.

**Identity choice for one-Session/one-Thread:** a Thread is a fold over one logical
Session's Event log, plus operator UI metadata; it is not a second execution or Event
source. Existing `agentplane/runner/test_restart.py` crash and SIGTERM cases reopen
the **same Session ID**, resume the native harness, and assert one contiguous Event
sequence. `agentplane/acceptance/test_suspend_resume.py` also resumes both harnesses
after Pod removal on retained storage. No further harness experiment is needed to
establish this same-storage identity behavior. Choose one globally unique canonical
UUID for new logical Sessions and their Thread views. This does not collapse their
different interfaces or metadata: access to a live runner remains distinct from
authorization to read retained history. Sandbox, runner process, and native harness
identities remain separate. A future disposable Sandbox restores that same logical
Session ID only if it restores and fences its complete runner journal and native state;
portable-state/cache tests gate that additional claim, not the shared-ID choice.
Today runner `session_id` is a client-chosen string scoped by Sandbox, while the app
mints a distinct UUID Thread ID on first sight. For existing histories, retain the
app UUID as the canonical public Session/Thread ID and import the association to the
existing `(sandbox, runner session_id)` as durable data in the new history authority.
The runner ID remains the internal storage/runtime locator: **do not rename** runner
state or native directories, workspace paths, or notification destinations. Preserve
source IDs, Event prefixes/cursors, and current Thread URLs. For new histories, use
the canonical UUID as the runner Session ID as well. Inventory and back up existing
associations before a one-off import; validate that both harnesses can resume against
their unchanged state and that the archived Event prefixes match. Remove transitional
import code after cutover, not the durable association needed to address legacy
storage. Existing running Threads also need the `RUNNER_IMAGE_ROLLOUT` pause/patch/
resume path before a runner/protocol upgrade; an archive mapping alone does not
upgrade the runner in an existing Sandbox. A future multi-Session Thread requires a
separate explicit model, not a delay to the one-to-one design.

**Identity/storage cutover:** implement
[one high-water mark per Event log](../docs/thread_layering.md#one-event-high-water-mark-per-log-across-harness-sessions):
a canonical durable log identity, explicit incarnation association, and a retained
runner journal on the landed exclusive writer fence. For the current mode, preserve
the static Sandbox association without encoding it as an immutable property of the
logical Session; portable runtimes may attach a future Sandbox to the same Session.
App and browser checkpoints refer to the runner's sequence. Native recovery remains
separately evidence-gated.

A successor reopens the journal; it cannot replace missing state with “app cursor + 1.”
Test runner replacement, fenced old writers, native resume, and unavailable recovery
state. Change protocol, runner storage, app routes, tests, and specification atomically.
New protocols need no general backwards-compatibility layer, but the one-off
association import must preserve existing histories, source IDs/cursors, and URLs.
Quiesce/fence old writers for the archive ownership cutover, back up databases and
volumes, verify imported mappings and Event prefixes, then resume and check both
harnesses before removing the one-off import code. Keep migration evidence and a
rollback procedure. Do not rekey native state or require copied-volume portability
proof for this same-storage identity cutover; portability has separate gates.

Replay of an existing single-session Thread can improve independently; multiple
incarnations must not ship by inventing a second Event counter. This item does not
choose successor command replay (`THREAD_SUCCESSOR_DELIVERY`).

### `THREAD_COMMAND_DELIVERY` — optional app command delivery queue

**Deferred for this slice, conditional on `COMMAND_QUEUE_DECISION`:** if the app promises acceptance while the
runner is unavailable, implement the
[app queue contract](../docs/thread_layering.md#optional-app-queue) for existing Threads
before combined creation. An unconsumed outbox is not a product ingress.

Cover immutable ids/payloads, multi-replica delivery, cancellation/expiry, unavailable
targets, and response loss at admission. Delivery order must not wait for terminal
effects; the runner owns scheduling. Input, model, interrupt, and stop need one coherent
app admission policy. No automatic cross-successor replay is implied.

### `THREAD_OUTLIVES_SANDBOX` — a Thread lifecycle beyond its Sandbox

**Deferred design:** today a Thread is bound to the Sandbox that runs it. A hosted Thread outlives
one, which is a durability and ownership question about the Thread record itself, separable from
what any surface shows of it.

### `APP_ALEMBIC_SQUASH` — consolidate the final integration-app schema

**After identity and archive cutovers:** the integration app has 17 Alembic revisions
and a deployed `alembic_version_app` stamp. Once all target databases have reached
the final schema and passed the one-off data migration, create a new baseline from
the final models and transactionally stamp each verified database at that baseline
under the existing migration lock. Test both a fresh database and a real copy of an
old-head database; do not delete historical revisions while any deployment could
still start from their stamp. After every environment is confirmed at the new baseline,
remove the transitional stamp/import machinery and obsolete revisions, while retaining
the migration runbook and rollback backups. Action Service and other Alembic histories
are independent; this task does not silently squash them.

### `SESSION_EVENT_RETENTION` — reduce redundant Session history storage

**Measured staging growth (2026-10-09 UTC):** the app `event` relation was ~6.2 GiB
(~11m rows); `thread_native_link` and `thread_evidence` added ~959 and ~847 MiB.
A 0.1% sample of `event` found ~5.9k native frames (3.05 MB sampled payload)
and ~5.5k `text_delta` entries (1.37 MB); together they dominated sampled rows
and payload bytes. The Sandbox Service's `session_event` copy was only partly
backfilled when measured: do not extrapolate its current size or count the old app
copy as permanent savings before the cutover and legacy-table retirement.

**Post-ownership implementation:** measure storage by Event kind and by native
harness frame subtype, including TOAST/index and derived fold/link/evidence cost.
Choose a documented, configurable retention policy for intermediate text/arguments/
output deltas: keep them while a turn is incomplete, optionally discard _only after_
a durable terminal/full-value observation makes replay and debug reads safe. Native
protocol wires remain available by default; evaluate a separate opt-in native-delta
policy only after proving which frames are reconstructible without losing harness-
specific packets, opaque reasoning, or recovery evidence. Never infer that a final
visible UI value reproduces a native frame. Prove live follow/cursor continuity,
partial/interrupted turns, raw reads, fold rebuilds, runner replay and native resume
for both harnesses; use non-destructive migration/compaction with size and correctness
checks before deleting historical data. Preserve the authority's append/replay
contract or explicitly version a new compacted-history contract and its clients.

### `THREAD_ARCHIVE_PLACEMENT` — choose the durable history authority

Decide between an archive component in Sandbox Service and an independent history
service, with no Sandbox Service → app runtime dependency. Choose raw Event, source
identity and cursor contracts, retention beyond Sandbox/PVC deletion, and where
canonical IDs and compartment/grant state reside. UI fold placement is independent.
This is a design decision; it does not itself transfer a row or authorize SA reads.

### `THREAD_ARCHIVE_STORE` — retained raw Event prefix and replay

Implement the selected authority's durable store and raw read/follow contract for
operator/service consumers. Validate source identity, sequence and exact duplicates,
reject conflicting payloads, and expose the retained contiguous cursor even if a
fold fails. The store must survive Sandbox deletion. SA-scoped history reads remain
gated on `THREAD_READ_POLICY`, not merely on the existence of an endpoint.

### `THREAD_ARCHIVE_BACKFILL` — one-way import of existing histories

Inventory, back up and import app-held raw Event prefixes, old public UUIDs and the
`(sandbox, runner session_id)` locators, plus source and feed checkpoints. Verify
counts, exact payloads and cursor/high-water continuity without renaming runner
native files or directories. Preserve existing Thread URLs; include a way to bridge
Events arriving between snapshot and cutover. The importer reads app-held data once;
there is no Sandbox Service → app runtime query or permanent migration shim.

### `THREAD_ARCHIVE_INGEST` — live runner-to-archive copying

Follow/replay runner Events into the new authority with per-log concurrency/fencing
or validated idempotency under multiple replicas. Persist ingestion checkpoints
independently of UI fold transactions. Define outage catch-up, source changes,
conflicting duplicates, and the cutover fence; shadow comparison is not permission
for two independent authorities to publish different histories.

### `THREAD_ARCHIVE_UI_CUTOVER` — consume the archive from the app

Switch app UI folds to replay from the new archive with their own epoch/checkpoint,
explicit lag/error and native evidence links. Retire the app's archive writes and
its direct SA transcript-read bypass; it may keep rebuildable fold rows and operator
metadata. Verify restarts, deleted Sandboxes and caught-up prefixes before removing
the old app archive authority. Do not expose SA reads until the policy gate passes.

### `THREAD_ARCHIVE_OWNERSHIP` — archive cutover capstone

Only after the store, one-way backfill, live ingester and app-consumer cutover are
verified is the app no longer the raw Event authority. The service-boundary design
below is the contract for those independently finishable slices.

**Service-boundary contract:** move the durable copy of runner Session Events
and its authorized raw read/follow API out of the integration app so agents can read
retained history through Sandbox Service. This does **not** require Thread folds or operator UI metadata to move out of the app;
canonical Session/Thread associations must be available in the history authority. Sandbox Service is a plausible
home because it opens runner sessions and owns runner reachability, but it currently
owns Kubernetes intent and has no archive database; historical Event storage must
outlive a Sandbox CR, Pod, or PVC. Compare an archive component within Sandbox Service
to an independently deployable history service; choose by durable ownership and least
privilege, not by today's endpoint names. `FollowSession` is a runner stream, not a
retained-archive read after Sandbox deletion.

The runner's journal remains authoritative for admission and Event publication; the
archive stores an independently replayable, contiguous copied prefix. Today the app
copies each batch **and** advances its fold in one database transaction. That coupling
is an implementation choice, not a requirement of append-only Event ingestion. The
archive ingester can commit an Event batch and its own checkpoint independently of
a fold projector. Replay from either the live runner while its journal exists or the
retained archive after deletion allows the fold to catch up or rebuild. Validate
source identity, cursor order and exact-payload duplicate replay; reject conflicting
entries rather than silently overwriting them. If folding fails, retain and expose the
raw prefix and explicit fold lag/error; do not stall archive ingestion just because a
projection cannot interpret an Event. Each projector commits its own epoch/checkpoint
with its derived rows, and fold views must never claim to cover a later raw cursor.

**Fold placement remains open:** folds could stay in the app as rebuildable,
platform-independent UI-friendly projections (retaining links to richer native/debug
evidence), or move alongside the archive if agent readers need the normalized view
there. A shared, versioned fold implementation need not dictate where its materialized
rows live. Either choice requires a documented replay, lag, error, native-link and
authorization contract once archive and fold commits are decoupled. For the first
agent-facing cutover, serve the retained raw Session Events through Sandbox Service;
keep the app's existing folds for its UI, with no agent-facing folded-read requirement
and no duplicate projector. One `read` grant covers both folded and raw/native
representations of the same history **when** a folded agent route is introduced.
If that route is added while folds remain app-only, do not have Sandbox Service fetch
them from the app: materialize an agent-facing projection from the archived Events
independently or defer the route. Keep canonical classification and grants in the
archive authority, with the durable Session/Thread association available without
querying the app. An independent history service upstream of both the app and
Sandbox Service is another acyclic option. Do not trust
an arbitrary forwarded caller header. Moving the Event archive does not itself make
native harness state portable.

**Migration gate:** define one durable archive authority and a staged, observable
transfer of existing Session Event prefixes, canonical IDs and legacy mappings, grants,
feeds, and cursor/high-water state. Multiple ingester replicas can cooperate only
with per-log claim/fencing or validated idempotent replay; there must not be two
independent archive owners. Transfer fold rows/checkpoints only if fold ownership
moves; otherwise point the app projector at the new replayable archive feed. Pin
revocation, operator access, restart, lag, and deleted-Sandbox behavior. If a legacy
conversion is destructive, make its loss explicit rather than claiming incomplete
histories are resumable. Split owner/ingestion/read/cutover into independently testable
slices above. Design `THREAD_READ_POLICY_DESIGN` in parallel, but do not expose SA transcript
reads through Sandbox Service until its source is an independent, durable archive:
not an app-backed broker and not the live runner's `FollowSession`. Backfill existing
app-held histories through a one-way migration, never a runtime service-to-app read.
Block direct SA reads of app history rather than retaining a bypass of the new grants.

### `SANDBOX_COMPARTMENT_DESIGN` — choose the co-residency rule

Decide where a Sandbox trust domain is assigned, how it relates to a Thread's durable
compartment and grants, and what happens to legacy mixed-compartment Sandboxes. Shared
filesystem and ServiceAccount credentials mean an archive ACL alone cannot enforce
isolation. Record the rule before implementation; do not rely on launch presets as
enduring authorization.

### `SANDBOX_COMPARTMENT_BOUNDARY` — enforce co-resident trust domains

**Enforcement after `SANDBOX_COMPARTMENT_DESIGN`:** multiple runner sessions
in one Sandbox share a filesystem and Sandbox ServiceAccount (including its grants, mounts,
and secrets). Threads in distinct compartments cannot safely share that Sandbox merely
because app/archive reads are filtered: either session can inspect the other's workspace.
Conservatively require one compartment/trust domain per Sandbox (or one Thread per Sandbox)
until stronger isolation is proved. Even matching compartments do not by themselves prove
that differently privileged sessions may share credentials or working files.

Decide where the Sandbox compartment is assigned and enforced on Open, including direct
Sandbox Service callers: the independent history authority owns canonical classification
and grants; Sandbox Service must reject a launch incompatible with the current Sandbox
trust domain rather than trusting an arbitrary client-supplied label. A launch preset
may supply an initial value, not become a persistent Agent type. For existing
co-resident Threads with mixed intended audiences, default to no ServiceAccount read
exposure and require reviewed reclassification or a new Sandbox; do not silently
merge their histories. Test mismatch, concurrent Open, same-name
Sandbox replacement, shared workspace/SA access, and historical archives after deletion.

### `RUNNER_STATE_BOUNDARY_RETHINK` — should the runner own durable state?

**Deferred design question, not a decided refactor:** today each Sandbox runner stores
its command journal and Event log in SQLite on Sandbox-attached storage alongside
native harness artifacts. A bulk runner-state schema migration or move off those
volumes could require inspecting and migrating each Sandbox PVC individually.
Pin the guarantees and migration tradeoffs of making the runner a thin, mostly
stateless adapter that launches/controls a harness and carries its protocol
traffic to Sandbox Service (or an independent history/command authority),
with durable command admission, Event ordering, and replay outside each runner PVC.
The native harness may still require persistent files; removing runner SQLite does
not by itself make Claude/Codex state or workspaces portable.

Treat Claude Code's RemoteIO worker protocol as the **leading candidate for the
Claude side**, not merely an arbitrary comparison. The client-protocol debundle in
the sibling `agentydragon/gaffer-private` repository informs [the existing
`CLAUDE_REMOTE_IO_EVAL`](claude_remote_io.md): SSE command delivery, worker epochs,
delivery receipts, uploaded Events, and internal transcript hydration. If Claude
uses this with its own infrastructure, that is strong prior evidence for the wire
shape; start by implementing a small compatible server and testing it rather than
inventing a competing Claude transport. We need not discover Claude's private
server storage design first. We **do** need to provide our own durable admission,
acknowledgement, replay and recovery guarantees at the chosen authority, and confirm
interop with the pinned CLI. This does not by itself settle the Codex adapter or
whether the general runner owns a database. Share the evaluation evidence without
making transport adoption a hard dependency for the boundary decision.

Examine whether the current boundary — a harness-neutral runner ↔ Sandbox Service
protocol — has pushed persistence and harness-independent recovery into the runner
unnecessarily. Compare per-runner storage with a central durable authority for
admission acknowledgements, deduplication and lost replies, ordered Event publication,
writer fencing across restarts/replicas, recovery after disconnect, and runner-offline
operation. Keep harness-specific mechanics behind an adapter without forcing the
service to understand native frames. Pin failure modes and migration/rollback costs,
including existing Threads and their PVCs, before choosing either design. This
investigation does **not** block the near-term archive, same-storage image rollout or
identity cutovers, which must preserve today's runner journal contract until a
replacement is designed and proved. Do not silently discard state or assume the
archive alone can replace native resume or command recovery.

### `RUNNER_OUTBOUND_CHANNEL` — worker-initiated transport

Build an authenticated outbound connection from each Sandbox runner/adapter to the
central service. Bind it to the Sandbox UID and worker incarnation, fence previous
connections, and support reconnect, backpressure and multi-replica server failover.
Prove the connection shape first while leaving the current runner journal authoritative;
outbound transport alone does not move admission or Event durability.

### `CLAUDE_OFFLINE_CATCHUP` / `CODEX_OFFLINE_CATCHUP` — recover work done offline

Independently for each harness, run an existing turn across central-service loss and
return, then repeat with a worker/process crash. Check which exact observations and command
effects can be recovered from native on-disk history, and which require a small local
spool. Resume from verified per-source cursors; reject conflicting or missing prefixes
rather than pretending a reconstructed UI transcript is an exact Event log. Continued
turns should be able to finish while disconnected, then catch up when service returns.
New remotely submitted work need not be accepted during an outage unless the central
admission contract explicitly promises it. Codex's native persistence and relay's
in-memory buffering are not by themselves proof of lossless catch-up after a crash.

### `RUNNER_CENTRAL_ADMISSION` — durable commands at the central authority

Specify and implement central command identity, acceptance, idempotency, delivery,
and acknowledgement under lost replies and worker replacement, without relying on
the runner SQLite journal as the source of accepted commands. Define whether offline
submission is rejected or durably queued, and distinguish receipt, dispatch and native
effect. Use the selected archive authority for raw Event prefixes without creating a
second independent Event log; retain writer fencing and exact replay.

### `RUNNER_OUTBOUND_CUTOVER` — migrate to thin, outbound-connected adapters

**Deferred migration direction:** after the runner-state boundary decision, prove
the outbound channel, each harness's offline catch-up and central admission
independently.
Cut over existing Sandboxes only with a tested image rollout and a durable archive
already able to reconcile their Event prefixes. Preserve old runner IDs and native
files; verify pending commands, exact Event cursors and rollback across worker/server
crashes before retiring runner-side journal state where its guarantees have actually
moved. This is not required for scoped agent reads, archive extraction, or same-storage
image upgrades. Native harness storage may still live on Sandbox PVCs; this migration
targets the **additional** Agentplane runner database and inbound control path, not
native-session portability or zero downtime.

### `THREAD_PORTABLE_STATE` — durable state beyond a Sandbox volume

**Deferred portability design:** the app's archived Event/transcript history can outlive a
Sandbox, but native Claude/Codex history, runner journal, workspace files, and resume
metadata currently depend on runner storage inside the Sandbox lifecycle. An archive of
rendered Events is not enough to reconstruct a native session or safely replay tool effects.

**Restore authority question (preferred direction):** retain or copy a bounded, version-compatible
snapshot of the native harness state (and separately the runner journal/workspace state),
then asking the native harness to resume. Do not copy an entire home directory without
inventorying credentials, paths, and per-Sandbox configuration. Reconstructing a new
harness session from Agentplane's interpreted operations would make Agentplane the
authority for harness-specific history, including opaque reasoning and tool outcomes;
it could silently lose cacheable prefix fidelity or repeat side effects. Keep semantic
reconstruction a separate, explicit design requiring native API support and evidence
that exact resume semantics and cache behavior survive, not a fallback after snapshot
restore fails. An unavailable or incompatible native snapshot means resume is unavailable
for that Thread, not permission to synthesize a plausible transcript.

Inventory the exact artifacts and version constraints for both harnesses and decide an
owned, versioned export/snapshot and restore contract for a stable Thread identity. Quiesce
and fence the old writer, copy a verifiable complete prefix and native state before
destructive storage removal, and define explicit unavailable/unknown outcomes on partial
snapshots. Retained artifacts must not include reusable Pod/ServiceAccount credentials
or silently widen their future access. A migration may intentionally leave legacy
Threads read-only or require an opt-in destructive transition, but must not claim
seamless resume from display history.

**Per-harness implementation nodes:** `CLAUDE_PORTABLE_STATE` and `CODEX_PORTABLE_STATE`
apply this shared contract only after their respective fresh-process cache-eligibility
spikes establish support. Neither harness gates the other's implementation. An
unsupported harness remains on retained storage; do not claim portable resume for it.
Keep the shared contract separate from `SANDBOX_LIFECYCLE_DURABILITY` (archive before
deletion), which must be complete before a disposable runtime removes managed storage.

### `THREAD_ON_DEMAND_RUNTIME` — disposable Sandbox for a durable Thread

**Deferred lifecycle after portable-state evidence:** provision a fresh, correctly scoped
Sandbox and runner when a prompt or authorized notification arrives for a durable Thread;
restore native and runner state, attach under one exclusive writer fence, and resume the
same logical conversation. After idle shutdown, keep the Thread, permissions, archived
Events, and restoration artifacts without retaining its old Sandbox CR/Pod as identity.
Specify whether queued input is accepted before a runner exists (`COMMAND_QUEUE_DECISION`),
wakeup deduplication, startup/bootstrap once for the new Sandbox, image selection/rollback,
expiry/cost, and behavior when native resume is impossible. Current notification inboxes
are UID-pinned to a Sandbox/session and retire after its removal; a deleted-Sandbox
Thread needs a new durable address and delivery authority, not a claim that those inboxes
already wake it. Test crashes and replica races through suspension, deletion,
reprovisioning, and notification wakeup. Ship warm continuation only for a harness
whose own portable-state implementation passed its native history and documented
cache-eligibility tests; otherwise stop at a documented unsupported outcome rather
than replaying a display transcript.

This is not a prerequisite for near-term `RUNNER_IMAGE_ROLLOUT`: first test the simpler
pause/patch/restart-with-the-same-storage route for updating an existing Sandbox image.
The runtime pivot may later supersede that operational workflow without invalidating
stable Thread IDs, archived history, or compartment grants.

### `HOSTED_THREAD_SURFACES` — read and control for a hosted Thread

**Deferred design:** the read and control surfaces a hosted Thread needs, beyond the Sandbox-bound
view the derived read model serves. Separate from the lifecycle: a Thread can outlive its Sandbox
before anything new reads it that way, and these surfaces can be designed against a Thread that
does not yet.

### `THREAD_READ_POLICY_DESIGN` — scoped read policy contract

Specify the compartment/explicit-ID grant vocabulary and authorized assignment,
reclassification, revocation and caller identity semantics. Inventory list, raw,
evidence and stream routes plus app SA bypasses; separate read from future send/create.
This design can proceed now, independent of archive migration and enforcement. No
agent-facing history read is shipped by this design node alone.

### `THREAD_READ_POLICY` — enforce scoped ServiceAccount Thread reads

**Implementation after design/archive/isolation gates:** today `TokenReviewer`
admits named ServiceAccount subjects, but the app's `require_caller` router
dependency does not apply per-Thread authorization.
An admitted token can read the full Thread list and raw Events, not just its own history.
Do not add new token subjects as a substitute for scoped grants; remove direct SA
access to app history routes before advertising Sandbox Service's grants. Operator
sessions retain their existing broader view; token authentication alone conveys no
history scope.

**Leading scope candidate to evaluate:** operator-defined Thread _compartments_ (or
collections), not agent types. A stable Thread has one explicit compartment and a
ServiceAccount may have separate `read` grants for named compartments; exact Thread-ID
grants can handle exceptional delegation. Example: a director SA reads the compartments
for its own conversations, finance-private discussions, and selected public-coder work,
but receives `send` authority only for a narrower set. Neither being a subordinate in an
organizational hierarchy nor using the same launch preset grants access. Presets may
suggest a compartment at launch, but the independent history authority must validate
and persist the assignment on durable history and its Thread association, not infer it
from the current preset or live Sandbox. Existing histories need a default that
exposes nothing to SA callers until classified by an authorized operator.

Unlike hierarchical intelligence _levels_, compartments have no implied dominance:
`read(finance-private)` does not imply `read(public-coder)` or `send(finance-private)`.
A Thread-level label grants access to its whole history, including prior messages;
reclassification should be operator-authorized and audited, with any wider disclosure
reviewed explicitly. Use another Thread rather than mixing unrelated confidentiality
scopes inside one transcript. Decide whether one compartment per Thread suffices before
adding multi-label OR semantics that could unexpectedly widen access.

**Trust-boundary gate:** `SANDBOX_COMPARTMENT_BOUNDARY` must prevent distinct
compartments from silently co-residing in a shared Sandbox before scoped read grants
can be advertised as confidentiality isolation. App archive authorization alone cannot
protect runner-local files or Sandbox credentials.

**Design gate:** choose who creates compartments, assigns/reclassifies Threads, grants
scoped verbs to ServiceAccounts, and revokes them; pin SA identity and replacement
semantics without silently inheriting another principal's access. Filter list/discovery
in the authoritative history/policy store and check direct reads, Events, observations,
evidence/frame routes, live feeds and replay at the same boundary. Treat an
unauthorized Thread as not found, reauthorize reconnects, and stop feeds on revocation.
Audit adjacent mutation, media, and bulk/sync endpoints before claiming that a caller
can see _only_ authorized Threads. Test selected versus other compartments, archived
and deleted Sandboxes, two replicas, reclassification, and revocation during SSE.
Co-design the grant vocabulary with future `CROSS_THREAD_DELIVERY` and
`THREAD_CREATE_POLICY`: **read does not imply send or create**. Do not block the
first read implementation on choosing command versus mailbox delivery, a hosted
Thread lifecycle, or whether UI folds move with the durable Session Event archive.

### `CROSS_THREAD_DELIVERY` — send a message to another agent's Thread

**Deferred design, distinct from read access:** decide whether an agent's message is a Thread
command (requiring a reachable runner, a stable command ID, runner admission, and eventual
Thread history) or a durable peer notification to its inbox (delivery/acknowledgement rather
than a user command). They may serve different use cases; do not label an inbox receipt as
command execution or silently turn a notice into a user turn. The Notifications Service
today has source subscriptions and inboxes pinned to a Sandbox UID and runner session ID,
not a generic cross-agent send API or an inbox owned by a stable Thread ID.

Specify sender provenance, recipient opt-in, per-target send grants, scope across Sandbox
replacement/Thread succession, payload limits, duplicate/lost-response recovery, abuse
controls, and what happens while the runner is offline. Resolve who owns a mailbox after
its Sandbox or session disappears. A read grant alone never authorizes sending, receiving
on another agent's behalf, or acknowledging its inbox. Design the authorization vocabulary
with `THREAD_READ_POLICY` without delaying its first read-only implementation.

### `THREAD_CREATE_POLICY` — create a Thread as a ServiceAccount caller

**Deferred design, separate from reading or sending:** specify which ServiceAccount may
open a Thread in which current Sandbox, and whether creating a new Sandbox is a separate
capability. Require explicit target scope and stable client-chosen session identity; do
not infer create authority from read grants, shared Sandbox names, or a caller's ability
to send notifications. Specify caller ownership, accepted spec/defaults, quota/abuse
limits, retry after a lost Open response, and Thread visibility to the creator. Keep the
current runner/Open receipt and app mapping distinct from any future durable app-owned
pending-Thread workflow (`NEWTHREAD_DURABLE`). Co-design grant representation and audit
with `THREAD_READ_POLICY`, but let that narrow read work proceed independently.

### `AG` — hosted Agent and Thread model

**Capstone** over `THREAD_OUTLIVES_SANDBOX`, `HOSTED_THREAD_SURFACES`, and
`THREAD_READ_POLICY`. It carries the claim that the hosted model exists, and nothing
of its own; deferred peer-send and Thread-create policy are separate capabilities.

### `DT` — driver-provided declarations and background control

**P2, deferred pending a real consumer:** a driver may declare model-visible tools and control
background work, but any such runner surface reuses the Action Service contracts rather than a
second tool-request lifecycle; the settled harness behavior and the seam are in
[driver tools and background work](driver_tools_and_background.md).

### `NOTIFICATION_WORKER_ISOLATION` — separate notification API and delivery workers

**Deferred reliability improvement:** the notification HTTP server currently starts delivery
workers in its lifespan. A fatal worker failure takes an HTTP replica out of service; if it repeats
on both replicas, inbox reads, acknowledgements, subscription management and GitHub webhook
receipt are unavailable even though the API and database may still be healthy. The worker
supervision in [#9390](https://github.com/agentydragon/ducktape/pull/9390) makes crashes
observable and restartable but intentionally does not change this failure domain.

Run the continuous delivery loop and PostgreSQL wakeup listener in a separate, multi-replica
worker Deployment. The HTTP Deployment handles authenticated API calls and webhook ingress
without owning worker tasks; its readiness/liveness reflect only its own ability to serve.
Give worker pods their own liveness/readiness and restart policy; no client-facing Service is
needed for them. Keep source-specific webhook verification in the API, worker-side source
reconciliation and delivery in workers, and scope credentials/RBAC/secret mounts to each role.
Retain the existing PostgreSQL inbox, claim fencing, retry and `NOTIFY`-as-wakeup semantics:
workers must resume due work after a missed wakeup or replica restart rather than rely on
process-local queues. Do not promise exactly-once external delivery solely from a lease.

**Acceptance:** kill a worker during delivery and prove the API still serves reads and explicit
acknowledgements and can durably receive new webhook events. Verify surviving/restarted workers
resume due inboxes across replicas without concurrent ownership, with errors visible and queue
age/backlog monitored. Exercise independent rollouts and worker-only failure/restart without
restarting or draining healthy HTTP pods. Diagnose any current worker crash separately; this
split is not its root-cause fix.

### `NOTIFICATION_GITHUB_RETENTION` — bound GitHub delivery receipt storage

**Measured staging growth (2026-10-09 UTC):** `notifications` was about 941 MiB;
`github_delivery` alone was 907 MiB (roughly 147k rows). `entry` was 27 MiB.
A 1% table sample put `workflow_run` and `check_run` first by stored payload bytes,
followed by `check_suite`; remeasure on both instances before changing retention.
`store.cleanup()` expires _inbox entry_ payloads after 30 days and purges retired
inboxes, but does not expire the separate GitHub ingress receipts or their payloads.

**Independent retention implementation:** decide how long a full receipt is needed for
active subscription matching, creation-position boundaries, webhook-redelivery
idempotence and cross-replica recovery. Then age out full `github_delivery` payloads
with an explicit bounded dedup identity/tombstone policy (or expire whole receipts
only after proving those properties); keep late redeliveries from creating duplicate
notices. Test active and newly created subscriptions, backlog/restarts, late webhook
redelivery, payload expiry and bounded cleanup batches. Measure actual PostgreSQL
relation/TOAST/index sizes before and after; a smaller logical payload does not by
itself reclaim on-disk space. Do not apply Session-delta retention rules to webhooks.

### `NOTIFICATION_ACTION_FEED` — remove idle Action-history polling

**Deferred optimization:** replace the notification source's five-second history polling with
one read-authorized change feed per replica, followed by catch-up from canonical Action events.
Reuse the existing Action Service committed-event signals; no operator authority or cross-service
DB access. [Acceptance](notifications.md#deferred-event-driven-actions-consumption) includes reconnect,
missed-signal recovery, subscription-creation races and idle-without-polling behavior.
The shared PostgreSQL listener refactor did not implement this cross-service feed.

### `NOTIFICATION_NOTICE_PACING` — stage-aware batching before another notice

**Incremental improvement, policy/timers TBD:** the current per-inbox quiet/max-wait debounce
batches entry creation, not opportunities to act on a previous notice. Entries can accumulate
while that input waits in the runner or during a long tool call/compaction; a second notice may
be unnecessary if the agent's eventual read picks up all entries after its last acknowledged
cursor. Try inexpensive improvements before requiring hook or native queue changes:

- Keep inbox persistence and reads immediate. Distinguish prepared, runner-admitted, harness-
  confirmed, actually model-visible (when observable), read, and acknowledged states. Harness
  confirmation does **not** prove the model sampled or handled a notice; a read does not ack.
- Preserve the existing single-unconfirmed-notice behavior: accumulate newer entries rather than
  preparing a second immutable notice. Keep its command ID, coverage, receipt/retry behavior and
  recovery; handle uncertain admission without resubmitting under a new ID.
- After confirmation, consider a bounded grace window for newer entries: the first notice may
  prompt a read covering those entries too. Recheck acknowledgement and current uncovered
  entries when the window expires; suppress a now-unneeded follow-up, otherwise send one notice
  covering the then-current cursor. Do not remind solely about already-covered, unacknowledged
  entries. Bound delay from the oldest _new_ uncovered entry even under continuous arrivals,
  missing acknowledgement or unavailable lifecycle signals; make timers durable.
- If the runner session becomes idle (harness running, no active turn), consider sending a
  follow-up for _new, uncovered_ entries promptly rather than waiting out the grace window:
  the previous turn no longer has an opportunity to pick them up in its next read. Recheck the
  inbox ack and runner state immediately before admission, including queued inputs and races
  with a new turn. Idle alone does not authorize a reminder for already-covered unacked entries;
  do not treat an offline, lost or suspended harness as idle. A submitted notice may itself start
  a new turn, so prevent an idle-to-notice feedback loop.

Measure the baseline and the proposed stages on both Claude and Codex: long operations,
compaction, resume, successive arrivals, ack during the grace window, stuck/unconfirmed notice,
restarts, idle transitions, and mixed/coalesced runner inputs. The Notification Service needs a
read-authorized, resumable view of runner session progress (e.g. attach state and turn events
through the existing Sandbox Service route), with a catch-up read before relying on idle events;
avoid per-inbox long-lived polling, cross-service DB access, and holding delivery leases while
waiting. Specify the waiting reason and next eligible time (or event, with a time fallback) in
durable/service-readable status so the operator notification drawer can explain why e.g. seven entries
are pending; distinguish pending inbox entries from notices
waiting for harness confirmation and entries awaiting agent acknowledgement. Do not claim a
precise delivery time if it depends on a harness event or the runner being offline.

Compare optional lifecycle/hook signals only if simple stage-aware timers leave a measurable
problem; hooks are not guaranteed pre-sample events. Native queued-input update/withdrawal
needs explicit too-late outcomes and must not cancel other coalesced inputs. Do not require LLM
proxy interception, which could cover subagents or unrelated requests. Keep this separate from notice wording and presentation.

### `HOME_ASSISTANT_NOTIFICATIONS` — entity and event subscriptions

**Unranked future source:** let agents follow authorized Home Assistant entity state changes and
events, using upstream entity IDs/event names and source-owned filters/payloads. Choose the connection,
credential and subscriber authorization model before implementation; do not expose every entity or
sensitive attribute merely because the service can read it. Prefer Home Assistant's event stream over
polling. Define reconnect/current-state reconciliation and missed-event limitations explicitly.
Acceptance covers a real state/event change through inbox and harness, filtering, reconnect and
revoked access. Monitoring does not grant control of devices or permission to run automations.

### `NOTIFICATION_SOURCE_WIRING` — extract shared wiring as sources accumulate

**Conditional future refactor, not a prerequisite for new sources:** as Actions, GitHub and further
sources expose concrete duplication, extract the wiring they genuinely share: source registration
and schema discovery, lifecycle ownership, stream reconnects, durable scheduling/checkpoints or
inbox handoff. Keep source-specific webhook verification, authorization, filter semantics, payloads
and upstream vocabulary with each source. Do not invent a universal filter DSL, unnecessary Protocols
or a new service before there is demonstrated shared behavior. Preserve meaningful differences
between webhook, watched-event and scheduled sources; add shared tests only for shared guarantees.
New sources beyond the candidates below should be driven by concrete agent use cases.

### `CRON_NOTIFICATIONS` — scheduled notifications for agents

**Unranked future capability:** let agents subscribe to recurring cron-style notifications through
the existing inbox/delivery machinery. Define schedule/timezone semantics (including DST), payloads,
ownership and cancellation in the source contract rather than adding a general-purpose job executor.

Use durable next-fire state and per-occurrence identity so restart or multiple replicas cannot lose
or duplicate scheduled entries. Choose missed-tick behavior explicitly (skip, bounded catch-up or
coalesce); bound frequency/backlog and avoid a burst after downtime. Reuse explicit acknowledgement
and existing destination-lifetime rules; waking suspended harnesses remains a separate decision.
Acceptance covers firing, cancellation, restart, replica races, missed ticks and DST transitions.
A scheduled notification must not itself grant authority to perform an Action or bypass approval.

### `MULTIAGENT_MODEL` — relationships and authority across agents and native subagents

**Unranked design prerequisite for Agentplane-level messaging and agent-requested Sandbox launch:**
choose stable identities for independently managed agents and their Sandboxes/Threads, and model
creator, owner/manager and parent/child relationships separately. Specify who may create, address,
observe, control, stop or delete a child; whether any authority is inherited (default to none),
how policy and quotas constrain depth/fanout, and what reassignment or parent termination does to
running work. An “X owned by Y” edge must not by itself confer access to X's Thread, credentials,
workspace or Actions. Distinguish operator-created agents from agent-launched ones, and explicit
delegation from mere provenance. Coordinate the policy vocabulary with `THREAD_CREATE_POLICY` and
`THREAD_READ_POLICY` without conflating a message grant, Thread creation and Sandbox creation.

Compare these Agentplane-level relationships with `NATIVE_SUBAGENT_THREADS`: Claude/Codex native
children necessarily live within their parent Agentplane Sandbox and are discovered as linked
Threads, not separately provisioned Agentplane agents. Preserve native parent/child provenance and
harness-owned lifecycle; do not implement agent spawning by relabeling a native Task, or make an
Agentplane-launched Sandbox masquerade as a harness-native child. Decide which shared UI/identity
terms and conversation/coordination concepts can be reused without sharing control or security
boundaries. The two tracks can progress independently; this is a comparison, not a new dependency
for native discovery.

### `AGENT_SANDBOX_LAUNCH` — allow agents to launch constrained Sandboxes

**Unranked future capability; depends on `MULTIAGENT_MODEL`:** specify and enforce a launch
policy for each caller: permitted templates/presets, image and harness/model selections, resource
limits, workspace mounts, network/egress, Kubernetes grants, Action policy bindings, secrets and
lifetime. A preset is a bundle of defaults, not permission to choose arbitrary overrides or grant
itself access. Resolve the effective spec server-side and record creator, owner/manager, chosen
policy, child identity, audit trail and explicit lifecycle/revocation semantics. Provide idempotent
create and bounded quota/depth to avoid duplicate or runaway launches after a lost response.
Prove both permitted and denied settings, concurrency, parent revocation/deletion and recipient
isolation. Launch authority alone grants neither Thread read/write nor messaging; those require
their own explicit policies. Reuse the existing Sandbox Service launch boundary where possible,
not a harness-native subagent creation API.

### `AGENT_MESSAGING_DESIGN` — choose agent-to-agent message authority and transport

**Unranked design, no transport selected:** define who may send to whom (agent, Sandbox,
Session/Thread and ServiceAccount identity), who administers allowlists or scoped policies, and
what happens on revocation or recipient deletion. Compare a Notification Service source (reusing
inbox, notice batching, runner delivery and acknowledgement), a purpose-built durable message
channel, and suitable existing messaging infrastructure. Separate a sender's accepted write from
recipient delivery, runner admission, agent handling and explicit acknowledgement. Apply the
identity/ownership choices from `MULTIAGENT_MODEL` without treating ownership as permission to
message. Decide how
receivers recover unread messages after downtime and how agents learn a recipient's stable identity;
do not assume a co-resident Sandbox or single replica. Treat message content and claimed sender
identity as untrusted, with source authentication and per-recipient authorization at admission.

### `AGENT_MESSAGING` — authorized agent-to-agent send and receive

**Unranked future capability; depends on `AGENT_MESSAGING_DESIGN`:** implement the selected
channel so agents can send and receive bounded messages without a human relaying them. Enforce
send/receive policy on the service side, including cross-Sandbox and cross-operator boundaries;
never rely on a prompt or frontend visibility filter as access control. Provide durable message IDs,
idempotent submit and replay/cursors, bounded retention and backpressure, clear delivery and
acknowledgement states, and useful sender/provenance metadata. If Notifications is selected, make
agent-originated messages a properly authorized source and reuse inbox batching without treating
notice admission as recipient acknowledgement; if not, document how the channel integrates with
runner turns and avoids duplicate or lost messages. Test permitted and forbidden pairs, revocation,
offline catch-up, concurrent senders/replicas, batching and retries without leaking content to
unauthorized recipients. This capability does not grant access to the other agent's Thread or
permission to execute its Actions.

### `AGENT_MESSAGE_CLASSIFICATION` — optional outbound content safeguards

**Lower-priority follow-up, not a prerequisite for messaging:** evaluate leakage classification
and policy-gated review for messages crossing trust boundaries. Define what content can be inspected,
who can review a held message, false-positive/appeal handling, and whether a block is surfaced to
both parties without disclosing the blocked payload. Do not silently discard accepted messages or
claim classifiers replace the sender/recipient authorization checks in `AGENT_MESSAGING`.

### `NOTIFICATION_PRESENTATION` — structured metadata and compact notification rendering

**Unranked future capability:** attach Agentplane notification metadata to generated runner input,
then preserve it through the runner journal, Sandbox Service event transport and app projection so
the frontend knows "this is a notification about this thing." The agent still receives the useful
retrieval instructions; the human-facing UI should default to a compact summary rather than the
whole machine-oriented message, with expansion/raw evidence available.

- Define versioned provenance and identity fields: notification origin, inbox/notice identity,
  covered cursor and safe subject/summary data as appropriate. Decide how a batched notice refers
  to multiple entries/sources without copying full provider payloads into runner metadata.
- Preserve metadata through retries, replay, archival and coalesced inputs. Explicitly represent
  mixed human/notification origins rather than relabeling an entire combined message. Reuse the
  existing command/Event authority; no parallel frontend notification log or app-owned ingress.
- Use explicit trusted origin metadata, never a text-prefix heuristic. Ordinary user text that
  resembles "Agentplane automated notification" must not be hidden or acquire system provenance.
  Missing/unknown metadata falls back to normal text rendering, and provider content remains untrusted.
- Acceptance: a real notification renders compactly by default and expands to full retained text;
  the agent sees unchanged actionable content. Verify older messages, replay/reconnect, mixed-origin
  coalescing and notification-looking human messages. Rendering must not acknowledge the inbox,
  hide human input or discard the authoritative message/evidence.

The owning backend/protocol carries metadata without depending on the integration app; the app is
its presentation client. This is independent of the shipped notice debounce and of Kubernetes
source selection.

### `THREAD_NOTIFICATION_INDICATOR` — show pending and upcoming notices in the Thread sidebar

**Unranked UI improvement:** put a small notification-state cue in the left-hand Thread list,
perhaps integrated into each Thread icon. In particular, an idle Thread with an inbox update
waiting for its next notice (for example, due in a few seconds) should look different from a
Thread with no pending notification. Explore a restrained ticking/countdown treatment, with an
accessible static label and reduced-motion fallback; distinguish queued/delayed notice delivery
from a notice already sent or acknowledged rather than treating "idle" as "nothing happening."

Use an authorized per-Thread view of the notification authority's state, including any next
eligible delivery time if available, without exposing another Thread's inbox or inferring state
from the rendered transcript or a browser-only timer. A deadline is an estimate: delivery may
be suppressed by acknowledgement, delayed by lifecycle/runner state, or fail. Reconcile after
reconnect and avoid claiming a guaranteed notice at an exact second. This is independent of
`NOTIFICATION_PRESENTATION`'s compact rendering of _delivered_ notifications and must not
acknowledge an inbox merely because its icon is displayed.

### `KUBERNETES_MONITORING` — agents observe rollout progress and outcomes

**Unranked future capability; design not selected:** let an Agentplane agent follow an explicitly
selected Kubernetes workload's rollout while doing other work, then inspect evidence of progress,
success, failure or stalled rollout. Start with a namespaced Deployment; broader resource kinds,
Flux reconciliation, Pod warnings and cluster-wide discovery can be evaluated later.

- Choose the read/watch API and its owning backend. A notification-service source is an option,
  not a prerequisite or a decision to add another service. Reuse existing inbox/notice/acknowledgement
  machinery if chosen; do not make a backend depend on the integration app or give runners a new
  notification connection.
- Define caller-authorized cluster/namespace/resource scope and revocation. A broad worker identity
  must not silently grant subscribers visibility they lack. Keep monitoring read-only; rollout
  mutation/remediation stays separately governed through Actions or existing workload authority.
- Follow Kubernetes list/watch semantics, including resource versions, reconnect and expired-version
  relist. Pin object UID and rollout generation/revision; distinguish replacement, deletion,
  supersession and revoked access from successful rollout. Do not promise complete historical replay
  from ephemeral Kubernetes Events.
- Derive rollout state from authoritative workload status/conditions (including observed generation
  and desired/updated/available replicas), not just a convenient Event message or an accepted change.
  Preserve diagnostic provenance and surface failure/timeout explicitly. Avoid exposing Secrets or
  unrelated Pod contents; bound retained evidence and coalesce noisy progress updates.
- Acceptance: an agent starts monitoring, continues other work, receives or retrieves progress and
  terminal evidence for both a successful and a stalled/failed rollout, and stops monitoring explicitly.
  Exercise reconnect/relist, duplicate signals, resource replacement and denied/revoked namespace
  access. If using notifications, prove the inbox-to-harness path and explicit acknowledgement too.

This is independent of the remaining GitHub recovery tests and Action-feed implementation; neither
is a technical prerequisite for designing Kubernetes monitoring. Prioritization remains open.

### `UISHELL_NEWTHREAD_SANDBOX` — pre-scoped "+ New thread" on a Sandbox's page

**Deferred combined UI:** a Sandbox's page opens the shared new-Thread composer with
that Sandbox selected and editable Thread fields prefilled. Accepting the first input
before a runner attachment is ready requires `NEWTHREAD_DURABLE`; explicit creation
and opening remain available independently.

### `UISHELL_NEWTHREAD_LANDING` — sidebar "+" unscoped new-thread composer

**Deferred combined UI:** the sidebar's "+" and a Sandbox page's own "+ New thread" open the same page an open
Thread already uses — same composer, same layout — just with no Sandbox/Thread bound yet. That state
lets the operator target an existing Sandbox or describe a new one (reusing `sandboxes.tsx`'s New
Sandbox fields and `sandbox_page.tsx`'s New Session fields, composed on one page); pressing Enter
persists the Thread command immediately, provisions whatever is missing, and rebinds the page to
the real Thread in place — the composer itself never moves or remounts. Pending input appears in
the command queue above the composer, not as a transcript bubble, until runner evidence confirms
its actual harness message. Depends on `NEWTHREAD_DURABLE` for the durable acceptance promise,
same as `UISHELL_NEWTHREAD_SANDBOX`.

**Existing chrome:** the sidebar's "+" currently opens the Sandbox list. Replacing it
with this combined composer waits for the durable acceptance dependency above. Whether this composer
eventually becomes the default landing page instead of requiring the sidebar click first is an open
question, not decided.

### `NEWTHREAD_DURABLE` — server-owned sandbox+thread provisioning

**Deferred, conditional on choosing app-first acceptance:** extend a proven,
authoritative existing-Thread outbox (`THREAD_OUTBOX_CUTOVER`) to atomically persist
Thread identity, resolved target/opening intent, and first command. Reconcile
Kubernetes prerequisites and native attachment without a live browser.

Acceptance follows [the optional app queue flow](../docs/thread_layering.md#optional-app-queue):
stable URL and pending input survive app/browser restart, while operational status
and runner effects remain distinct. Each preset field is individually editable.
Do not infer native resume from Sandbox readiness or silently transfer unsettled
commands into a successor scope.

### `THREAD_OUTBOX_CUTOVER` — one ingress if the app queue is chosen

**Deferred for this slice, conditional on `COMMAND_QUEUE_DECISION`:** when the app queue has its complete
delivery/recovery semantics, move every normal product `Command` through it in one
cutover. Retire the ordinary relay path and per-operation bypasses, including manual
UI controls that issue runner commands. Manual Sandbox lifecycle remains available.

Prove input, interrupt, model change, and stop through app commit, runner admission,
effect/non-effect, and reload. Keep “saved by app” distinct from “runner admitted.”
This is an extension of the chosen ingress, not a second concurrent normal path.

### `THREAD_SUCCESSOR_DELIVERY` — unsettled command across a successor runner session

**Deferred decision:** when a harness/session is replaced, decide whether an admitted-but-unsettled
Thread command is recoverable only in its predecessor session or may be delivered by a successor.
The required native continuation proof, command-provenance guarantee, and no-duplicate-effect test
gate are in [Thread, runner, and harness layering](../docs/thread_layering.md#deferred-commands-unsettled-across-successor-sessions).
Until then there is no automatic cross-session replay.

### `THREAD_SYNC_STOPPED_RECOVERY` — recover stopped Thread synchronization automatically

**Remaining exception to live views:** `frontend/threads/projected_session.tsx` offers a manual
“Refresh thread” button after a terminal synchronization error. Decide when the sync client may
retry automatically, with bounded backoff and a visible stale/error state rather than an infinite
silent loop. Reconnect from the durable cursor and reconcile the current Thread window; a retry
must not re-submit a command or discard locally pending input. Distinguish transient transport
failures from permanent access/scope errors, for which the operator needs an actionable error
instead of an automatic retry. Acceptance covers disconnect, archive/scope change, repeated failure
and eventual recovery without requiring a manual reload. Ordinary Settings, Actions and Sandbox
views already use live streams; their shipped contracts belong in the app and Action Service READMEs.

## Deferred work

Real work items, parked. Most are here because they had no edge of any kind -- not a hard
dependency, not a dotted soft one -- so the diagram carried their boxes without carrying any
relationship. The rest are parked by decision even though they had one. Where that happens the
edge leaves the diagram with the node and the relationship it carried is stated in the entry's
own text instead, so bringing one back means restoring an edge rather than inventing one.

- **`SSHDURABLE`** — durable SSH-backed processes
- **`PROFILES`** — cross-cutting capability profiles
- **`BB`** — BuildBuddy hosted-run credential boundary
- **`THREAD_BROWSE_PAGINATE`** — paginated/searchable all-threads page
- **`CONTROL_STATE`** — dynamic runtime control acceptance
- **`LIVE_CLEAN`** — executor heartbeat retention cleanup
- **`FORK`** — per-task identity fork (depends on `ELEVATE`, which stays on the board)

### `SSHDURABLE` — durable SSH-backed processes

**Deferred support:** the `ssh-mcp` server behind the MCP Executor runs one-shot commands; for
processes that must survive an SSH disconnect, add a small `agentplane-execd` host component for
`rugged` and `wyrm2`. SSH still authenticates as the configured target user; an
unprivileged stdio client forwards structured requests over a local Unix socket to a root-owned
daemon. The daemon derives the execution user from kernel Unix-socket peer credentials and does not
accept a requested-user field. It delegates process lifetime, cgroups, signals, and unit status to
the host systemd system manager, so user lingering is not required.

The daemon's durable handle is a systemd transient unit derived from the Agentplane Execution ID.
Future code-owned Actions may start, inspect, read bounded output from, signal, and terminate that
unit. Agentplane remains authoritative for Action schemas, approval, caller control rights, durable
Execution state, leases, and unknown-outcome reconciliation; the daemon is only a constrained
systemd adapter. See [the durable SSH process plan](ssh_durable_processes.md) for the protocol and
acceptance boundaries. Do not add this daemon, PTYs, or stdin streaming to the first one-shot implementation.

### `PROFILES` — cross-cutting capability profiles

**Deferred decision — Rai confirmation required:** define a durable authority for capabilities shared by egress, approvals, MCP
reachability, and other tool permissions. Do not widen the landed launch-preset slice merely to
reserve the concept; Kubernetes remains a storage candidate, and the profile owner, inheritance, and policy
read/verification boundary remain open. Do not start implementation before the design is confirmed.

**Acceptance evidence:** one profile can be resolved consistently by each participating authority,
with explicit precedence and negative tests for stale, cross-Agent, or caller-supplied profile names.

### `BB` — BuildBuddy hosted-run credential boundary

**Deferred decision:** accept the weaker hosted-runner boundary — a narrow `runner.RunRequest`
rewrite that keeps the real key out of the local Sandbox but hands it to agent-controlled code on
BuildBuddy's runner — or wait for a stronger seam (a per-run BuildBuddy credential or a run-scoped
gateway). The boundary, wire shape and required evidence are in
[`buildbuddy_remote_auth.md`](../docs/buildbuddy_remote_auth.md).

### `THREAD_BROWSE_PAGINATE` — paginated/searchable all-threads page

**Deferred, way later:** the sidebar's Threads list is fine for a working set, but finding one old
Thread once it runs past the dozens needs its own answer — probably a full page, the same shape as
the Action history page. Not designed here; flagged only so the with-sandboxes endpoint doesn't get
assumed to stay one unpaginated call forever.

**Depends on** the cross-sandbox Thread-listing endpoint (extends it with cursor pagination and,
eventually, search). Nothing above waits on this.

### `HARNESS_MANUAL_COMPACTION` — user-triggered compaction from the frontend

**Unranked future capability:** let the user request native harness context compaction from the
Thread UI, through the existing authorized app → Sandbox Service → runner command path. Determine
Claude and Codex support and busy-turn behavior separately; show unsupported/unavailable states
rather than pretending a generic summarization prompt is native compaction. Distinguish request
admission from actual start/completion/failure using runner evidence, including reconnect/replay.
Preserve Thread/session identity and the durable transcript/Event archive: compacting model context
is not deleting conversation history. Acceptance covers a real frontend request, native compaction,
continued conversation and retained shared instructions for each supported harness, plus failure and
reconnect behavior. Reuse the native triggers and post-compaction evidence from the harness
tests; the frontend control does not need a separate instruction-retention proof first.
Implementation and UI details remain open.

### `CONTROL_STATE` — dynamic runtime control acceptance

**Deferred native-capability work:** the admission/effect contract, display of an
admitted-but-not-effective model change, and any future time-local capability snapshot
are specified in [Thread, runner, and harness layering](../docs/thread_layering.md#command-protocol-intent-admission-then-outcome).
The harness-specific evidence still needed for model/effort capability reporting is in
[runtime control acceptance](../docs/runtime_control.md). Do not introduce an app-side
common active-turn gate or make the picker claim success before a causal effect.

### `LIVE_CLEAN` — executor heartbeat retention cleanup

**Deferred cleanup:** executor liveness currently creates one heartbeat identity row per coordinator
process lifetime. Once deployment scale makes that accumulation meaningful, choose a stable executor
identity or bounded expiry/compaction policy and add retention tests; do not change the exactly-one
claim or unknown-outcome semantics while doing so.

### `FORK` — per-task identity fork

**Deferred design:** a wide ServiceAccount-bound identity such as "Claude Code web via OIDC" may
serve several concurrent agent threads managed outside Agentplane. An agent forks its identity into
a per-task sub-identity, requests permission for that sub-identity through `ELEVATE`, and uses the
sub-identity's credential for the task. Scope then follows possession of that credential: a thread
that never receives it never gains the permission. Open questions: how a sub-identity is
represented (a derived ServiceAccount, or a child Connection under the parent's OAuth grant),
whether the parent's permissions flow down, and how the sub-identity ends. Low priority; nothing
else depends on it.

## Out of scope

Not deferred work with a node below, but scope this project is not pursuing:

- capability matrices or a broad Agent identity/privilege framework;
- cross-cutting capability profiles — see [`profiles.md`](profiles.md);
- delegated-versus-brokered external-access policy and grant/revocation semantics — see
  [`external_access.md`](external_access.md);
- MCP registry, dynamic action marketplace, standing grants, and a general cross-agent
  privilege-sharing framework beyond scoped messaging and constrained agent-requested launches;
- access to Agentplane services beyond Actions for hosted agents and external harnesses — the
  constraints are in
  [workload authentication § Access beyond Actions](../docs/workload_authentication.md#access-beyond-actions);
- per-destination workload audiences until recipient isolation is required;
- per-Action output schemas on direct tools, or other new generic-tool metadata;
- registration/enrollment retention cleanup, once actual growth is measured — bounded expiry that
  preserves historical attribution and replay tombstones, never a gate for client use;
- broad profiles beyond the landed launch-preset slice;
- separating the egress proxy's rule namespace from its Sandbox namespace — both deployments pass
  one namespace for both today, the reason separation mattered is not recorded, and a split has to
  replace the app's binding-to-Sandbox ownerReference cascade with a sweep
  (`agentplane/app/egress.py`);
- a runtime editing surface for `ActionPolicySet`s, and for bindings beyond the one the app
  writes at launch — Git and `kubectl` are the editors;
- creating a labeled caller ServiceAccount at OAuth enrollment instead of by a Git edit;
- a TokenReview admission path for an external client holding a ServiceAccount token with a
  dedicated audience, as Sandboxes authenticate, instead of OAuth; and
- cryptographic Decision signing until Decisions cross a boundary that requires it.
