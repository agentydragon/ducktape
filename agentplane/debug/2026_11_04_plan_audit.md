# Agentplane plan audit — 2026-11-04

Source snapshot: `origin/devel` at `e32e156adef1dbd07a581d0c179222cd21413a41`. The task DAG is the unfinished-work authority; this is an audit record, not a second dispatch map. Status below is as of that snapshot and does not assert a deployment or live-cluster result. Bare component paths are relative to `agentplane/`; `task_dag.md` is under `agentplane/plans/`. Line numbers refer to the snapshot. Recheck source before acting.

The audit examined each of the 127 task headings on the snapshot. “Decision” retains an unselected contract even where related functionality exists. A partially implemented node remains open until its own outcome is met. The notification-addressing node was subsequently removed: public inbox and message APIs, the CLI, and worker delivery now use Session ID; Action grant/address work and environment-independent central inboxes remain tracked by their separate nodes.

## Task DAG inventory

### `SCHEDULED_AGENT_WAKE_DESIGN` — choose the durable scheduled-wake contract

**Decision** — PR #13552 remained open when checked during this audit; no matching merge is present in the audited `origin/devel` history. No scheduler contract/API was found in Agentplane implementation. Keep the choice among scheduler-fed commands and Notification-owned scheduling, including identity, duplicate, overlap, and restart semantics.

### `SCHEDULED_AGENT_WAKE` — wake a standing agent on a durable schedule

**Still open** — Depends on the unresolved design. A search of Agentplane code for scheduled wake/schedule/CronJob paths found no durable agent wake implementation. The daily-agent capstone remains separate. Keep.

### `NOTIFICATION_COMMAND_OUTCOME_DESIGN` — notice delivery without reading conversation history

**Still open** — `notification_service/worker/deliver.go:15-18,63-72` reads History, and `:65-68` explicitly leaves command-scoped outcomes by ID as TODO; delivery scans receipt entries at `:103-138`. No command-scoped outcome API replaces this. Keep.

### `NOTIFICATION_SERVICE_SPLIT` — split, move or rename the Notification Service's responsibilities

**Decision** — The service still owns inbox/subscription APIs (`notification_service/api/service.go:82-101`), source subscriptions (`api/subscribe.go:16-62`), and notice delivery (`worker/deliver.go:63-138`). No split or reviewed ownership/naming proposal is evidenced. The stated later/unscheduled decision remains valid. Keep.

### `COMMAND_WATCH_SUBSCRIPTION_DESIGN` — a subscription kind that runs an agent's command on a schedule

**Decision** — The current subscribe implementation accepts only `ActionsSource` and `GitHubSource` (`notification_service/api/subscribe.go:45-62`); no command-watch subscription kind is present. Execution identity, bounds, trigger semantics, and authorization remain open as the node says. Keep.

### `SESSION_INPUT_METADATA` — later typed annotations on input commands

**Still open** — `runner_gateway/dispatcher/session_dispatcher.proto:112-120` has text and expected Sandbox UID, but no typed metadata/provenance field. The notification presentation plan confirms the missing dispatcher-to-runner journal path (`notification_presentation.md:3-7,15-20`). Keep.

### `SESSION_INPUT_METADATA_READ` — authorized annotation reads

**Still open** — It depends on the missing input metadata. Neither the Dispatcher request nor this event path carries it; presentation plan requires the fold to associate annotations through command IDs (`notification_presentation.md:52-65`). Keep.

### `NOTIFICATION_NOTICE_METADATA` — producer integration

**Still open** — The worker submits only expected Sandbox UID, Session ID, command ID, and text (`notification_service/worker/deliver.go:134-138`). There is no typed notice identity/range attached to the input. Keep.

### `NOTIFICATION_PRESENTATION` — compact notification rendering

**Still open** — The required metadata producer and read path are absent. Component plan status is open/unscheduled (`notification_presentation.md:3-7,75-90`). #14235 (“Use Session IDs for harness notification context”) changes notification context addressing; it does not add trusted notice provenance/metadata. Keep and preserve the no-prefix heuristic boundary.

### `SUBSCRIPTION_AUTHORIZATION_DESIGN` — auto-allow and operator-approved subscriptions

**Decision** — No reviewed choice between Action approval and source-side policy is present. Generic RPC admission allows authenticated accounts (`notification_service/api/service.go:82-101`), while `api/subscribe.go:16-62` checks destination ownership and the current Action/GitHub sources. Those checks do not settle who may approve broader sources, scopes, renewal, revocation, or retained entries. Keep.

### `SUBSCRIPTION_AUTHORIZATION` — creation gate and continuing enforcement

**Partially done** — Existing source and destination checks are enforced (`notification_service/api/subscribe.go:16-62`), and GitHub access is authorized at `:51-59`. The broader create gate plus continuing source-grant, renewal/update, expiry, and revocation contract has not been implemented. Keep, explicitly scoped to the missing reviewed authorization contract rather than redoing current checks.

### `GITHUB_MERGEABILITY_FRESHNESS` — obtain a current result after base-only changes

**Partially done** — The implementation preserves UNKNOWN for unproved/stale results and has bounded retries (`notification_service/debug/github_mergeability_freshness_probe.md:11-15`; `notification_service/docs/api.md:97-100`). The open GraphQL comparison is real: the 10/28 probe did not query GraphQL (`.../github_mergeability_freshness_probe.md:15`); the live acceptance's GraphQL read was followed by a head change and does not isolate base-only freshness (`github_mergeability_live_acceptance_20261028.md:51-56`). Keep only that controlled comparison/contract decision; the proof, retry, and outage/restart work already shipped should not be reopened.

### `MULTIAGENT_MODEL` — shared identity and authority vocabulary

**Decision** — Caller-privilege spawning is approved, but creator/manager/parent, cross-SA authority, addressing/ownership, and native-child boundary remain separate questions. `multiagent_communication.md:18-38,185-206` confirms that same-SA workflows landed while broader delegation and topology remain open. Keep; current functionality does not decide these semantics.

### `THREAD_READ_POLICY_DESIGN` — scoped history read policy

**Decision** — Explicitly held by the operator. The component plan distinguishes implemented same-principal reads from the held cross-SA policy (`scoped_session_reads.md:3-22`). Tag/all-Sandbox scope, replacement, unknown association, listing-vs-transcript access, and no-UID history behavior remain decision questions. Keep.

### `THREAD_READ_POLICY` — authorized retained-history access

**Partially done** — Same-principal retained reads are enforced: History scopes ordinary callers to the retained execution principal and configured Pod-bound readers to all Sessions (`history_service/api/service.go:50-65,99-124`; `storage/store.go:63-96`). Cross-SA PolicySet/history-tag reads, list/read separation, and continuing stream revocation are not implemented. Keep the remaining work; do not describe all ordinary History access as absent. Relevant same-principal delegated surfaces are in #13536/#13541.

### `HISTORY_SANDBOX_RECORD` — History's Sandbox record and history tags

**Partially done** — #11835 persisted Sandbox UID association and retained Session lookup (`history_service/README.md:131-142`), but there is no per-Sandbox History record/watch or `historyTags`. Sandbox spec currently exposes only `InboxTags` (`sandboxes/api/types.go:115-120`). `plans/sandbox_tags.md:3-5,36-51` still correctly marks the separate record/tags unimplemented. Keep; do not mistake the per-Session UID column for the planned record.

### `HISTORY_SANDBOX_ASSOCIATION_SOURCE` — an authoritative Sandbox UID source for History

**Decision** — Current association uses the Sandbox controller ownerReference on the writer ServiceAccount (`history_service/ingress/sandbox_uid.go:15-58`; README `:133-142`). The DAG explicitly says to keep that source until the alternative is reviewed. No alternative is evidenced. Keep as an unscheduled decision, not a migration requirement.

### `PLATFORM_GUIDANCE_OWNERSHIP_DESIGN` — decide platform-guidance ownership and composition

**Decision** — Shared guidance rendering and runtime-aware text now exist (`session_instructions/platform_instructions.py:12-37`; `platform_instructions.j2:1-14`; native and central deployment call sites in `cluster/cdk8s/agentplane/environment_sandbox.py:233-235,402-404` and `central_sampler_workload.py:67-73`). The open proposal still concerns configuration/distribution ownership, version/failure semantics, and per-environment composition. Managed central rollout was verified after #13410 (`central_sampler/docs/rollout_acceptance.md:55-70`), but that does not settle this later decision. Keep, narrowed to those remaining questions.

### `PLATFORM_GUIDANCE_SINGLE_OWNER` — consolidate platform-guidance ownership

**Partially done** — A shared renderer/template is used by both native and central paths (above), so “only shared template text exists” is no longer a complete implementation description. However, the ownership/composition proposal is unreviewed and the node's end condition removes redundant ownership only after that decision. Keep pending design; consider updating the description to acknowledge the common renderer and name only the still-open distribution/configuration ownership.

### `SESSION_TOOL_SURFACE_CONVERGENCE_DESIGN` — review a common Agentplane tool surface

**Decision** — Distinct `orchestration_v1` Actions (`orchestration_v1/README.md:10-13`), caller-delegation MCP/Action tools (`session_delegation/README.md:15-30`), and Session CLI exist. No reviewed capability/authority map or migration proposal is present. Keep; the new surfaces do not imply one has replaced the other.

### `ACTION_PROTO_CONTRACT_DESIGN` — protobuf contracts for Action inputs and outputs

**Decision** — Current Action inputs use typed Go records (`session_delegation/contract/inputs.go`) and published schemas are adapted through the Action catalog; outputs include serialized protobuf results. No universal protobuf request/response wrapper or generated-contract choice was found. The concrete inputs/outputs/approval metadata comparison remains unreviewed. Keep.

### `THREAD_CREATE_AUTHORIZATION` — caller-privilege delegation capstone

**Partially done** — Backend caller authorization, Discovery, managed guidance, ten tools configured in staging source, retained result reads, and scripted external-MCP/parent-CLI acceptance are in code and the stated PRs #13536/#13541; staging activation was #13514, guidance rollout #13410, and explicit environment resume #13918. The remaining operator follow-up is unaided agent discovery/use; the DAG correctly says no merge/rollout gate. Keep as the bounded usability capstone; do not repeat the shipped scripted implementation.

### `DAILY_CI_AGENT` — a daily CI-maintenance agent that can delegate fixes

**Still open** — No durable schedule, daily workflow configuration, retained daily run/outcome, or operator visibility implementation was found in Agentplane. It depends on scheduled wake and still requires a useful natural delegation run. Keep.

### `ENVIRONMENT_SHARED_ACCOUNT` — environments using a pinned existing SA

**Still open** — Current controller creates and owns a same-name ServiceAccount and PolicyBinding (`sandbox_controller/identity.go:22-33,36-66`), and Discovery requires the caller account to match that identity (`runner_gateway/environments/service.go:269-275`). #14273 records a fixed Sandbox-lifetime identity direction but explicitly does not implement it or prohibit sharing (`plans/session_environment_model.md:394-399`; commit `3494ea3b40`). The borrowed/shared-account provisioning mode remains unimplemented. Keep; reconcile language with the newer fixed-identity direction without claiming it supersedes borrowing.

### `ENVIRONMENT_TEMPLATE_CATALOGUE_DESIGN` — catalogue ownership and location

**Decision** — Current `ListEnvironmentTemplates` returns only sorted SandboxTemplate names (`runner_gateway/environments/internal/management/access.go:71-83`; `environment_service.proto:18-20,84-86`). It does not combine published runtime configuration and app/deployment defaults or pin approved revisions. Keep the catalogue owner/publication decision.

### `ENVIRONMENT_MANAGEMENT` — dedicated environment tools and creation

**Partially done** — EnvironmentService already exposes typed `CreateEnvironment` and basic template listing (`environment_service.proto:16-23,56-86`); the handler requires a direct Pod-bound manager/launcher grant and validates the created namespace/name/UID (`runner_gateway/environments/management.go:21-55,68-92`); internal create records caller/request intent and retries idempotently (`internal/management/access.go:85-124,431-445`). Missing are the planned `environments/create` caller-delegating Action using the caller's borrowed SA, full catalogue/pinned approved configuration, dedicated durable caller-UID creation UUID/recovery workflow, and CLI/human-approval flow. The DAG was updated by #14267 to acknowledge the RPC foundation. Keep as deferred future workflow.

### `AGENT_LAUNCH_POLICY_DESIGN` — Sandbox launch and delegation policy

**Decision** — The existing `orchestration_v1` launch takes concrete templates, PolicySets, tags and bootstrap but relies on operator Action decisions and shape-matched grants (`orchestration_v1/README.md:17-28,37-52`). It does not settle broader/different-privilege templates, resource limits, secrets, egress, quotas, revocation, or inherited authority. Keep.

### `AGENT_SANDBOX_LAUNCH` — constrained agent-requested launch

**Still open** — The operator-broker launch is not the caller-privilege constrained launch required by this node; its README calls that authority model a stopgap (`orchestration_v1/README.md:3-8,17-21`). Implementation remains blocked on launch policy. Keep.

### `ORCHESTRATION_V1_BACKEND_GRANTS` — `orchestration_v1` operations on backend grants

**Still open** — `orchestration_v1` continues to act using its trusted service identity after an Action Decision; its README records this exception (`:17-28`) and the ADR makes backend grants the target end state (`docs/adr_orchestration_v1.md:19-24,39-49`). Caller-privilege delegation for other tools has not migrated this group, and no migrated operation was found. Keep, and migrate operation by operation only after each backend grant lands.

### `RUNNER_HARNESS_OBJECTS_DESIGN` — decide process diagnostics and placement policy

**Decision** — Lifecycle ownership and current facts are implemented: the runner owns processes, Claude uses a process/store per Session, and Codex uses one app-server/store per Runner (`docs/runner_delivery_and_harness_design.md:300-350`). Native child characterization #10776 explicitly does not establish Agentplane child identity/control (`native/docs/child_sessions.md:8-19`). The comparative placement/store recommendation and API shape remain open; do not reopen approved lifecycle ownership or authorize child Session projection. Keep, narrowed to remaining placement, identity, concurrency and failure choices.

### `VM_INTRA_SANDBOX_HARDENING` — optional process and state hardening inside one Sandbox

Decision (explicitly deferred; no active acceptance). task_dag.md:795-805 accepts Sandbox as the isolation boundary and says to revisit Runner-state privacy/tool-child hardening only after a concrete product need or incident. vm_sandboxes.md:109-112 likewise states the trust domain is the Sandbox. The operator explicitly requested retaining the nonblocking reminder in the DAG.

### `VM_RAM_SUSPEND` — suspend a VM to disk with its RAM

Still open. task_dag.md:807-817 and vm_sandboxes.md:80-81 state KubeVirt has neither RAM suspension nor live migration. Current stop/start uses persisted disks but is not RAM resume.

### `VM_CONSOLE_DEFERRED_ATTACH` — attach after VM startup makes the console available

Still open. app/go/sandboxes/console.go:15-24 calls an unattached console error “unavailable”; :57-63 turns nonzero KubeVirt errors into that error. frontend/sandboxes/sandbox_console.tsx:27-30 displays unavailable and stops the stream. It does not distinguish startup from authorization/deletion/competing-client errors and does not retry attachability.

### `LOCAL_BAZEL` — bounded local client in a VM

Still open. env/images/README.md:48-58 says the image checks hook/tool availability, hosted builds use BuildBuddy, authenticated remote execution is not proven from the image, and local Bazel is deferred until VM-backed environments. docs/buildbuddy_remote_auth.md:27-31 also forbids inferring local Bazel authorization from package availability. No representative in-VM local client run is documented.

### `SAMPLER_NOTIFICATION_JOURNAL` — investigate executor notification journal deadline

retain incident investigation; central_sampler/executor.go:195 still provides a five-second notification journal context, and no cause has been established by that fact.

### `SESSION_LAUNCH_FRICTION` — fewer clicks to start useful work

retain product/UX outcome; frontend/sandboxes and threads/new_thread_form.tsx implement current form, but no evidence the selected common path was accepted as an improvement; #13938 remains open.

### `VM_STARTUP_LATENCY` — measure production time to a useful fresh VM Session

retain production measurement; the node names staging evidence absent from its cited record; test guests do not establish production KVM timings.

### `AGENT_PRINCIPAL_MODEL_DESIGN` — Agentplane principals and external identities

retain decision; principal/account lifetime and identity authority remain unsettled. See history_service/api/service.go:99 and workload_auth/audience.py for current SA-based authority.

### `SESSION_RUNTIME_MODEL_DESIGN` — Sessions, harness placement and execution attachments

retain broader design; native local and central executor paths exist but the user's zero-environment and multiple-attachment contracts are explicitly unimplemented; lifecycle authorization claim needed correction.

### `AGENT_AUTHORED_CONVERSATIONS_DESIGN` — conversations agents act on through tools

retain decision; current history_service/history_service.proto models observation-derived Thread entities, not the proposed authored message/edit/participant resource family.

### `STRUCTURED_TOOL_OUTPUT` — tool results as ordered text and image parts

retain: proto/event.proto:153 and :181 use string output, and env/runtime/claude/protocol.go:113-128 projects only text blocks.

### `CODEX_CODE_MODE_CELLS` — Codex code-mode cells as runner items

retain upstream/native capability investigation; code-mode cells are not a modeled runner Event, and no new upstream evidence was obtained here.

### `CENTRAL_SAMPLER_MODEL_CAPABILITIES` — live route check for standard-mode routes

retain bounded route check and compaction gap; source tests are deterministic and do not prove the named live provider's wire acceptance.

### `CENTRAL_SAMPLER_PROMPT_COMPACTION` — compaction by our own prompt

retain: central_sampler/responses/provider.go BuildCompactionRequest still rejects unsupported dialects; a provider-backed feature does not establish requested client-side compaction.

### `CENTRAL_SAMPLER_BACKGROUND_COMPACTION` — summarize while sampling continues

retain design: ordinary compaction intent and replacement exist, but that is not evidence of a separate concurrently fenced background operation.

### `CENTRAL_SAMPLER_DYNAMIC_TOOLS` — change tool definitions during a Session

retain decision/trigger; no verified mid-Session provider definition and runtime catalog migration.

### `CLAUDE_REMOTE_EXECUTION_SPIKE` — gated native Claude remote tools

retain; #12210 remains open and the requested native-gated behavior is not established on devel.

### `CLAUDE_CUSTOM_SAMPLER` — custom Claude loop and shared code-mode compatibility

retain; #12211 remains open; deployed native Claude or Anthropic adapter do not prove the distinct custom-CLI-shaped sampler experiment.

### `SANDBOX_FORM_TERMINOLOGY` — distinguish launch presets from runtime templates

retain: app/frontend/sandboxes/list.tsx labels the inputs `Preset` and `Sandbox template` (lines 225,248).

### `PLATFORM_SERVICE_NAMES_DESIGN` — stable sidecar names for platform services

retain review, distinct from existing exact mappings in cluster/cdk8s/agentplane/platform_routes.py.

### `EGRESS_CREDENTIAL_TRANSPORT_DESIGN` — credential upstream schemes and ports

retain decision; egress/plans/credential_transport.md and egress/proxy/gate.go describe current substitution boundary rather than a selected origin-pair contract.

### `PLATFORM_SERVICE_ROUTING_DESIGN` — direct platform access versus central egress

retain architectural decision; existing per-service routes do not select a future central/direct credential policy.

### `ACTION_SURFACES_DESIGN` — where and in what states Actions render

retain design; per-action offersCardDecisionButtons is a partial implementation, not a selected unified UI/product rule.

### `DOCKABLE_PANES` — dockable panes in the web app

retain unscheduled product decision; experimental #9721 is open, not a replacement selection.

### `CLAUDE_CODE_WEB_BRIDGE` — Claude Code web sessions in Agentplane

retain operator-deferred product idea; no selected implementation.

### `EGRESS_RELAY_RENAME` — rename the egress sidecar to relay

retain requested rename: cluster/cdk8s/agentplane/sandbox_pod.py:37 still publishes agentplane-egress-sidecar image.

### `RUNNER_HARNESS_DELIVERY_DESIGN` — choose how runners obtain harness binaries

retain decision; a harness-specific image split does not establish a measured independent delivery architecture.

### `K3S_BUNDLED_SERVICES_SPIKE` — measure a less isolated control plane in real k3s

retain unscheduled measurement; existing normal k3s fixture is not evidence for a credential-isolated bundled topology.

### `BUILD_BOX_BAZEL` — build Sandboxes can run Bazel

retain separately from LOCAL_BAZEL; agentplane/docs/buildbuddy_remote_auth.md states the VM/local-client policy and action-service Sandbox remains a different target.

### `PC_KUBERNETES_POLICY_SET` — public-coder's Kubernetes access as one PolicySet

Still open. task_dag.md:1385-1394 asks to convert public-coder’s whole grant set to one PolicySet/PolicyBinding and identifies the Group-subject mismatch. agent_access_profiles.py:40-72 still models Group and ServiceAccount subjects; public_coder/app.py:594-603, :622-631 and :649-689 still emits separate RoleBindings/ClusterRoleBindings.

### `CLAUDE_AI_SA` — review the external caller's actual authority

Decision. task_dag.md:1395-1400 leaves the actual external caller authority review open. agent_access_profiles.py:63-72 identifies the static claude-ai ServiceAccount; :252-261 still assigns five static grants. No accepted intended-authority review was found in the audited source.

### `AGENT_PERMISSION_REQUESTS` — an Action type and approval view for permission requests

Still open, blocked on ACTION_SURFACES_DESIGN. task_dag.md:1401-1410 states the required dedicated Action and approval view. Existing app/go/policyaccess/policyaccess.go:1, :97-107 handles app-created PolicyBindings; it is not a permission-request Action or request-review contract. Do not mark done from the existing grant editor.

### `MANAGED_SA_RBAC` — grants for accounts without a Sandbox

Still open, blocked on CLAUDE_AI_SA. task_dag.md:1411-1418 requires replacing claude-ai static grants with a Flux-owned binding after the review. agent_access_profiles.py:252-261 still contains the static grants and :267-275 materializes static profile subjects.

### `SANDBOX_RBAC` — bounded deployed grant checks

Still open (deferred, not dispatchable until selected). task_dag.md:1419-1430 explicitly leaves specific multi-Sandbox/shared-set/one-off/revocation/role-edit/failure-partway checks uncovered. Existing PolicySet/PolicyBinding implementation does not prove those named deployed cases. No live credentials or cluster checks were used in this audit.

### `NOTIFICATION_INGRESS_BOUNDARY` — bounded public-route security check

Still open (deferred). task_dag.md:1431-1437 says invalid-signature tests exist but the public-host unsigned webhook and private-API route checks remain. notification_service/webhook/handler.go:113-124 checks signatures, and handler_integration_test.go:308-338 covers bad signatures; these do not establish the two ingress-route boundaries or absence of created records through those routes.

### `NETWORK_WORKLOAD_IDENTITY` — network-level workload identity

Decision. task_dag.md:1438-1446 records “consider later” and the cost of a second auth scheme. workload_auth/auth/auth.go:142-150 performs audience-scoped Kubernetes TokenReview for current workload identity. No SPIFFE/SPIRE mutual-auth implementation was found in the audited Agentplane auth paths.

### `DECLARATIVE_SESSIONS_DESIGN` — long-running Sessions and managed subscriptions through Flux

Decision. task_dag.md:1490-1519 explicitly selects no CRD/database replacement and requires a reviewed desired-state/runtime ownership contract first. Current sandboxes/api/types.go:85-124 defines the Sandbox resource, while the Session model remains service-owned; no declarative Session CRD implementation was found in the audited Sandboxes/API sources.

### `STATE_VOLUME_BLOCK` — block storage for Pod and VM state and workspace

Partially done. VM workspace is block-backed and accepted, but state remains Filesystem/DirectoryTree and Pod workspace remains filesystem. sandbox_controller/state_volume.go:64-75 rejects any state PVC not Filesystem plus DirectoryTree. sandbox_controller/workspace.go:50-60 allows BlockFilesystem only when the template is a VM. docs/vm_sandboxes.md:67-73 confirms block workspace / virtiofs state for VM and DirectoryTree Pod workspace.

### `NODE_LOCAL_BLOCK_STORAGE` — node-local block volumes on every node

Still open. task_dag.md:1529-1538 points to nonexistent cluster/cdk8s/openebs_lvm/storage.py. Current piraeus.py:28-36 identifies node-local classes and selected prepared-node affinity; :627-649 defines piraeus-ssd-local and piraeus-hdd-local with placementCount=1 and no remote access. This proves selected-node strict-local classes, not provisioned capacity on every cluster node. Keep the coverage task and repair its link.

### `STATE_VOLUME_VIRTIOFS_RETIRE` — delete the superseded state attachment

Still open. docs/vm_sandboxes.md:70-72 says the VM state PVC remains a DirectoryTree Filesystem claim attached through virtiofs. The Sandbox Controller retains that state layout.

### `VOLUME_LAYOUT_CONVERSION` — explicit offline volume conversion

Decision (explicitly unscheduled/much later). task_dag.md:1546-1555 says no generic converter is authorized now. sandbox_controller/SPEC.md:120-124 blocks incompatible layouts with VolumeLayoutMismatch; no conversion path was found. Keep the rejection contract and separate future conversion from normal activation.

### `SANDBOX_SPEC_EDIT` — edit a suspended Sandbox's template or kind

Still open. task_dag.md:1556-1569 requires an operator app edit of template/kind only while suspended. environments/v1/environment_service.proto:17-52 exposes create/list/watch/get/resume/suspend/delete and policy setters, but no template/kind edit RPC. sandboxes/api/types.go:85-124 still has template and kind-selected resource configuration without an app editing method.

### `WORKSPACE_NODE_PORTABILITY` — move a workspace between nodes

Still open (deferred for remaining local claims). piraeus.py:627-649 makes local classes placementCount=1/no remote access; environment_sandbox.py:65 selects piraeus-hdd-local for Pod Sandboxes. Staging VM uses piraeus-ssd-2-autodiskful (staging.py:631; vm_sandboxes.md:70-72). Copy-based portability for other local claims is not established.

### `SANDBOX_AUTODISKFUL` — consider auto-diskful storage for Sandboxes

Decision. task_dag.md:1577-1593 asks to compare replication capacity/I/O with strict-local placement and preserves the fact that changing a template does not migrate existing PVCs. Current Pod class is piraeus-hdd-local (environment_sandbox.py:65); staging VM is set to piraeus-ssd-2-autodiskful (staging.py:631; piraeus.py:660-678). This is still a Pod storage decision, not an approved migration.

### `SANDBOX_ARCHIVE` — a suspended Sandbox archived to object storage

Decision/idea, no implementation. task_dag.md:1594-1618 labels it a thought rather than a plan. sandboxes/api/types.go:223-253 defines current SandboxStatus fields (volumes, incarnation, idle suspend/quiesce/credential/mode/conditions) with no archive state/rehydration field. Do not create an implementation node absent a selected product need.

### `SANDBOX_GENERIC_SPLIT` — separate the generic Sandbox from the runner's sealing

Decision. task_dag.md:1619-1631 leaves the split location and ownership open. sandbox_controller/controller.go:240-260 has Sandbox finalization seal Sessions and quiesce the Runner before workload deletion, so the runner-aware concern remains in the Sandbox reconciler.

### `RUNNER_IMAGE_LAYERING_DESIGN` — what carries the runner, the harnesses and the tools

Decision. task_dag.md:1632-1684 still says options are unchosen. docs/vm_sandboxes.md:60-66 says Runner/CLI are on a separate readonly disk but harnesses and tools remain in the guest image. Pod image.nix similarly keeps harnesses/tools in the environment image. Independent tool/runtime layers are not shipped.

### `RUNNER_LAYERING_SPIKE` — minimal guest, harness and tools-layer evidence

Partially done / paused. task_dag.md:1685-1721 documents merged RBE/Firecracker evidence and unresolved Claude child-context, image-volume, cold-preparation, compatibility, and startup-measurement limits; production layout and design decision remain open. This is evidence recorded in the DAG, not a production runtime.

### `RUNNER_LAYERING_VM` — a guest takes the runner, harnesses and tools from separate images

Partially done. task_dag.md:1722-1744 and vm_sandboxes.md:60-66 confirm the runner disk landed; tools/harnesses remain in the guest, so the split requested by the node is incomplete.

### `IDLE_SUSPEND_UI_DRIVE` — finish the VM staging UI drive

Partially done. task_dag.md:1745-1760 records an accepted five-minute Codex Pod path but says the staging VM UI drive is not identified/proven. Automated Pod/VM lifecycle coverage does not satisfy the specifically requested VM browser workflow.

### `AGENT_RESUME_NOTICE` — tell agents when their Sandbox was suspended and resumed

Partially done. task_dag.md:1761-1780 says only central sampler reports verified incarnation changes. central_sampler/SPEC.md:98-108 describes the durable notice, old/new incarnation fence, lost process state and unknown outcomes. The general Runtime/other-harness and pre-next-exec notice is still open.

### `ENVIRONMENT_READINESS_WAIT` — wait for environment readiness in client adapters

Still open. task_dag.md:1781-1788 and runner_gateway/environments/README.md:69-70 say the watch reports current state and resume acknowledges acceptance, but CLI and Action/MCP bounded wait adapters remain separate work.

### `AGENT_EXECUTOR_LOSS_NOTICE` — tell agents when executor restarts or kills lose their processes

Still open. task_dag.md:1789-1802 leaves notification owner/deduplication and controlled executor-loss notice open. env/TODO.md:126-132 still records shared cgroup/OOM/lost-work concerns. runtime/execution/ISOLATION.md:29-48 describes the current failure domains, not a delivered event.

### `RUNNER_BUSY_TRACKING` — the runner counts terminals and background processes as busy

Partially done. sandboxes/api/types.go:174-220 enumerates ActiveTurn, PendingCommand, Setup, Activation, Bootstrap, Admission, ExecutorRelay; it has no terminal/background-process or console blocker. The DAG’s missing busy-lifetime contract remains.

### `SESSION_ACTIVITY_STREAM` — the notification service delivers on a Session's idle time

Partially done. History folds idle_since from HarnessStarted/TurnCompleted and clears it at TurnStarted/exit (history_service/projection/summary.go:97-106); Dispatcher exposes WatchSessionSummaries (history_service/history_service.proto:46-49). Notification delivery still chooses from PendingEntryTimes plus NoticeDebounce (notification_service/worker/deliver.go:155-184), not Session idle_since.

### `DISPATCHER_HARNESS_CONFIRMATION` — settle admitted inputs on a canonical confirmation

Partially done. The wire enum includes HARNESS_CONFIRMED (session_dispatcher.proto:69-79) and snapshots.go can project that enum (:10-29), but dispatcher/README.md:126-128 explicitly says it has no writer and current inputs stay RUNNER_ADMITTED. Preserve the canonical confirmation source decision.

### `DISPATCHER_SEND_AUTHORIZATION_GENERALIZE` — replace v0 service trust with scoped sender grants

Decision (deferred). task_dag.md:1834-1847 keeps the narrow App/Notification v0 trust plus accepted orchestration exception and says destination/operation-scoped sender grants, delegation and queued-work revocation semantics are unchosen. Do not generalize trust implicitly.

### `RESUME_CREDENTIALS_VALID` — grants and tokens still valid after a long suspension

Decision. task_dag.md:1848-1858 and debug/resume_credentials_valid.md:3-25 show app PolicyBindings can expire; resume does not renew/recreate grants, token rotation is separate, and both “optional capability vs prerequisite” and strict expiry enforcement remain open. The audit explicitly says no cluster state was inspected; do not treat it as current live proof.

### `SANDBOX_ENVIRONMENT_RENAME_DESIGN` — review a scoped Sandbox-to-Environment rename

Decision. task_dag.md:1878-1892 says “probably rename” is exploratory and selects no rename. EnvironmentService exists, but the resource and API still include Sandbox vocabulary: sandboxes/api/types.go:85 defines SandboxSpec and EnvironmentRuntime proto still carries Sandbox identity.

### `ENVIRONMENT_RUNTIME_SERVICE_SPLIT` — separate environment and Harness/Session APIs

Partially done. environments/v1/environment_service.proto:16-53 already owns public environment management/discovery. env/runtime/control/environment_runtime.proto:15-40 remains a mixed EnvironmentRuntime service with WatchState/GetStatus/Quiesce plus ListSessions/OpenSession/SubmitInput/model/reasoning/interrupt operations.

### `RUNNER_GATEWAY_RENAME` — rename the Runner Gateway

Still open. cluster/cdk8s/agentplane/runner_gateway.py:1-9 still names the service and token audience runner-gateway; runner_gateway_workload.py:64-65 still publishes the agentplane-runner-gateway image. The node correctly limits this to the Gateway’s own package/deployment/security identity; EnvironmentService’s API rename is not proof it is done.

### `HISTORY_SERVICE_RENAME` — History as a transcript service

Decision. task_dag.md:1914-1922 leaves “transcript service” terminology as a consideration. history_service.proto continues to declare HistoryService methods (lines 1-14), and the package, token audiences and deployment still use history_service/History Service.

### `HISTORY_DATABASE_RENAME` — name the database for its owner

Still open (deferred/not dispatchable until selected). cluster/cdk8s/agentplane/database.py:30-35 retains sandbox_service in database/role names and comments that renaming moves data; history_service.py:1-8 and :158-159 connects History Service as owner. Keep the data-preservation constraint; do not substitute a database recreation.

### `ACTION_BOX_IDLE_REAPER` — delete Action boxes nobody uses

Still open. action_service/sandbox/inventory.go:264-266 retains TODO(sandbox-lifetime): forgotten boxes hold namespace quota until disposed; no last-exec stamp/reaper was found.

### `ACTION_BOX_RENAME` — Action boxes stop being called sandboxes

Still open. action_service/sandbox/README.md:142-147 continues to expose sandbox__exec and related direct tools; the action_service/sandbox package and command_sandbox template names remain. This is an external wire-name change, so preserve the task.

### `SERVICE_SETTINGS_RETHINK` — how Agentplane services are configured

Decision. task_dag.md:1949-1959 asks for one service-wide settings approach. egress/README.md:50-55 says the sidecar reads only its required YAML file; history_service/config.go:72-86 uses Viper environment and YAML config. No cross-service configuration decision is recorded.

### `REFACTOR_COHERENCE_AUDIT` — the refactor reads as one design

retain candidate; user-selected protocol audits were scoped migrations and do not prove a whole-repository coherence sweep.

### `RUNNER_JOURNAL_TRIM` — runners stop keeping what the ingress committed

retain unscheduled design; env/runtime/SPEC.md and runner_control.md distinguish runner journal and History acknowledgements, not a settled local-trim contract.

### `DISPATCHER_PERSISTENCE_MODEL_DESIGN` — choose delivery ownership and service topology

**decision, still open.** Mandatory Dispatcher PostgreSQL and queue admission are selected; queue-off is no longer viable. The binary/deployment boundary, delivery ownership/topology and immediate-or-fail option remain open (`task_dag.md:2119-2139`). Dispatcher requires the database in the current service path; the old merge proposal’s queue-off route is superseded. **Safe burn-down:** keep the decision node; replace references to queue-off implementation as active work with a short historical pointer, and decide topology/ownership and immediate-or-fail separately.

### `SESSION_DATA_OWNERSHIP_DESIGN` — review Session data, services and databases

**decision, still open.** The node was newly requested for 2026-11-04 and requires an authoritative-writer/read/lifetime/recovery inventory across Dispatcher, History, Notifications, central sampler and Runtime, then a service/database comparison (`task_dag.md:2141-2164`). #14269 is a plan-only request, not the review. **Safe burn-down:** keep. Do not infer that a shared Session UUID selects shared service or database ownership; the DAG expressly keeps History separate pending review.

### `SESSION_IDENTITY_CONTRACT` — Session identity and shared-Environment admission

**decision, partially decided.** The trust invariant and alternatives remain open: one verified identity for Sessions sharing an Environment, who may create/operate as that identity, identity lifetime/revocation, and central identity/delegation (`task_dag.md:2166-2194`; `session_environment_model.md:345-399`). #14273 chose only the initial fixed-per-Sandbox ServiceAccount direction. Current Dispatcher retains the execution account and checks exact account/Sandbox UIDs on native opening and command paths (`dispatcher/SPEC.md:50-70,77-100`); there is no independent Session-principal representation. **Safe burn-down:** preserve the decision; record the fixed-Sandbox-SA subdecision as settled, without marking the whole contract complete or broadening it to independent/central principals.

### `CENTRAL_SESSION_CREATION_CONTRACT` — opening a central Session without an Environment

**decision, still open.** The required zero-Environment create/retry/send/History contract and unknown-outcome semantics are not selected (`task_dag.md:2196-2209`). `Dispatcher.OpenSession` still requires Session ID, Sandbox name and Sandbox UID (`api.go:440-455`); central sampler admission requires executor identity and incarnation (`central_sampler/service.go:88-104`). **Safe burn-down:** keep and decide the minimal zero-Environment owner, immutable request/retry key, unavailable-owner behavior, setup/cwd/instructions and authorization contract before implementation.

### `SESSION_ATTACHMENT_CONTRACT` — Session-to-Environment relationships

**decision, still open.** Native required placement versus central zero-or-more attachments, attachment authority, revision/target identity, races and History transition source remain unsettled (`task_dag.md:2211-2223`; `session_environment_model.md:262-282`). Current opening is still Sandbox-bound and central sampler still requires an executor (`session_dispatcher.proto:212-220`; `central_sampler/service.go:89-104`). **Safe burn-down:** keep; do not treat current placement checks as attachment APIs or implicitly reattach after replacement.

### `SESSION_IDENTITY_ENFORCEMENT` — enforce one identity per shared Environment

**partially done on current native placement; broader node remains blocked.** Existing native openings bind the Sandbox execution ServiceAccount and commands check that principal and exact Sandbox UID (`dispatcher/SPEC.md:50-70,77-100`; #14208/#14211). The planned atomic invariant across every admission/identity-change path and future central attachments is not implemented; its DAG prerequisite is the still-open identity contract (`task_dag.md:2225-2236`). **Safe burn-down:** keep blocked for the generalized contract; optionally mark current native exact-account/UID checks as completed evidence, not as proof of central-attachment enforcement.

### `CENTRAL_SESSION_WITHOUT_ENVIRONMENT` — central Session without an Environment

**still open.** Both public Dispatcher opening and central sampler still require an environment/executor (above); no zero-Environment end-to-end path is implemented (`task_dag.md:2238-2256`). **Safe burn-down:** keep blocked on identity and creation decisions; preserve stable IDs, setup semantics and no-replay behavior as acceptance requirements.

### `SESSION_ENV_ATTACHMENTS` — attach and detach Environments

**still open.** The durable desired attachment state, attach/detach API, explicit targeting, status/revision and concurrent/deletion handling are absent (`task_dag.md:2258-2270`). Current requests have required native Sandbox placement; no general central attachment contract is present (`session_dispatcher.proto:212-220`). **Safe burn-down:** keep blocked on its stated three prerequisites. Do not count the Dispatcher’s immutable native placement lookup as attach/detach support.

### `SESSION_ATTACHMENT_SURFACES` — use attachments from agents, CLI and app

**still open.** Agent tools, CLI and app discovery/attach/detach/status surfaces depend on the absent attachment implementation (`task_dag.md:2272-2280`). **Safe burn-down:** keep behind `SESSION_ENV_ATTACHMENTS`; no surface-only phase should imply attachment authority.

### `SESSION_ATTACHMENT_HISTORY` — record attachment transitions in History

**still open.** There is no selected/implemented attachment transition source to deliver time-qualified attach/detach evidence to History (`task_dag.md:2282-2289`; `history_service.proto:250-284` retains Sandbox provenance, not an attachment history). **Safe burn-down:** keep as a separate later correctness outcome; do not make it a prerequisite for first zero-environment UI, as the DAG already distinguishes.

### `SESSION_LIFETIME_IMPLEMENTATION` — implement reviewed Session and inbox lifetimes

**still open.** The owning lifecycle and coordinated inbox/History/receipt behavior depend on the open joint lifetime decision; the DAG properly forbids silently deleting authored/unread data (`task_dag.md:2291-2301`). Dispatcher cleanup currently deletes opening and dispatch rows by Sandbox UID (`terminal.go:149-156`), while other stores reconcile independently. **Safe burn-down:** keep blocked on lifetime and identity decisions; allow a separately reviewed native-only safe slice only if it preserves the unknown-outcome fence.

### `CENTRAL_SESSION_NOTIFICATIONS` — inboxes for Sessions without Environments

**still open.** Notification subscription and worker still resolve a Dispatcher opening to a Sandbox and require exact placement (`notification_service/api/subscribe.go:19-43`; `worker/deliver.go:135-138`). No environment-independent inbox authorization/delivery exists (`task_dag.md:2303-2312`). **Safe burn-down:** keep blocked; do not fabricate an executor or retire a central Session’s live inbox merely because an attachment is absent.

### `CNPG_FREE_K3S_INTEGRATION_TIER` — add a narrow database-independent integration tier

**still open.** The current K3s fixture calls `fixture_database.add_database` and wires a `postgres-session-dispatcher` URI (`system_tests/k3s/fixture_chart.py:384-408`). Dispatcher admission requires PostgreSQL by selected contract; the node itself says the new tier must avoid Dispatcher admission and scope direct Runtime/other flows (`task_dag.md:2314-2331`). **Safe burn-down:** keep, but narrow acceptance to genuinely database-independent paths; do not label the existing K3s suite database-free or remove Dispatcher’s database.

### `SESSION_DIRECTORY_NATIVE` — Dispatcher authority for environment-hosted Sessions

**partially done.** Dispatcher’s `session_opening` is the live Session/placement authority, exact retries/read lookup are implemented, commands require a record, queued admission is placement-checked, and native opening outcomes hold uncertain dependent work (`task_dag.md:2333-2353`; `dispatcher/SPEC.md:40-60,73-102,169-195`). Still open are explicit resolution of held `UNKNOWN` openings and a durable private harness-store/native-ID mapping; deleted records are forgotten and callers must not reuse UUIDs. The opening API still names a Sandbox (`api.go:440-455`). **Safe burn-down:** retain node as partial. Remove any completed API/lookup/placement tasks from its remaining text, but keep explicit unknown-opening resolution, private mapping and no-replay risk.

### `DELIVERY_DESTINATION_AUTHORITY_DESIGN` — assign Session send authority and internal fences

**decision, partially decided.** First native cutover caller/wake rules, Session-record prerequisite, exact placement fences and Gateway proxy restriction have landed (`task_dag.md:2355-2386`; `dispatcher/SPEC.md:73-110`; `gateway/SPEC.md:3-26`). General sender grants/revocation and central Session authority remain separate/open. **Safe burn-down:** narrow remaining design questions to general grants/revocation and central authority; retain “UID is a fence, not authority” and distinct send/wake/read permissions.

### `SESSION_DELETION_FENCE` — prove and enforce the Session deletion fence

**partially done, high-confidence open gap.** Managed exact-incarnation Runtime quiescence and same-name replacement fencing are covered by #14134/#14135/#14146 and the system-test scenario (`task_dag.md:2388-2419`; `gateway/SPEC.md:28-40`). Sticky possible-forwarding evidence prevents classifying uncertain delivery as unsent (`dispatcher/SPEC.md:197-212`). The remaining issue is bounded durable outcome visibility after cleanup: terminal events are notifications to connected watchers, cleanup deletes receipts, and a watcher delayed behind prior invalidation can miss already-terminal evidence (`dispatcher/SPEC.md:214-220`). **Safe burn-down:** keep; do not remove the node on the strength of the deletion test or convert `UNKNOWN` into failure. Resolve a bounded reconnect/read contract.

### `DELIVERY_DESTINATION_CONTRACT` — send commands to a Session ID

**partially done.** The selected public command address is Session UUID; App/CLI commands no longer require Sandbox, and Dispatcher resolves placement. Session command proto fields 3 / `sandbox`, `sandbox_uid` are reserved; the optional `expected_sandbox_uid` remains an authorization precondition (`task_dag.md:2421-2437`; `session_dispatcher.proto:112-159`; `api.go:363-404`). Action schemas still contain `TargetRef` on open/submit for existing scoped grants (`session_delegation/contract/inputs.go:78-90`). General grants, accepted-work revocation, rollout and post-deletion outcome evidence remain open. **Safe burn-down:** remove the completed App/CLI command-address migration from remaining scope, retain Action/grant review, caller and wake authority, no-replay and deletion boundary.

### `SESSION_AND_INBOX_LIFETIME` — decide Session and inbox lifetimes

**decision, still open despite selected sub-decisions.** Central Session indefinite lifetime and Dispatcher forget-on-explicit-Session-delete are selected, but behavior when a required Environment disappears, Notification inbox/subscription retention, History handoff, operator-only access after account disappearance and cross-store reconciliation remain unresolved (`task_dag.md:2439-2469`; model discussion at `session_environment_model.md:594-604`). Current Dispatcher removes openings by Sandbox UID (`terminal.go:149-156`) and Notification retires stale destinations, so the selected independent lifetime is not yet implemented. **Safe burn-down:** keep the decision and its implementation child; do not use the central “indefinite” selection to imply inbox or native Session retention semantics are complete.

### `SESSION_NOTIFICATION_ADDRESS` — address inboxes and notices by Session ID

**partially done; one listed subtask is stale-complete.** Inbox operations and public messages use Session ID (#14218/#14227); the worker submits `SessionId` plus its recorded `ExpectedSandboxUid` (`notification_service/api/messages.go:45-61`; `worker/deliver.go:135-138`). Harness context is Session ID only (#14235). The DAG still says to migrate the “remaining native delivery worker,” which has already happened; Action-facing `TargetRef` inputs remain (`session_delegation/contract/inputs.go:78-90`) and require reviewed grant/address work (`task_dag.md:2471-2488`). **Disposition:** remove the completed node; the Action grant/address contract and central no-Environment inbox are tracked separately.

### `SESSION_HISTORY_PRESENTATION` — represent Sessions without a required Sandbox

**partially done, still open.** Session-keyed raw/history reads exist, but `FindSessions` only filters by Sandbox name/UID and the Thread view rejects a summary without Sandbox identity (`history_service.proto:266-284`; `reads/summaries.go:62-80`; `app/frontend/threads/client.ts:419-420,484-539`). The plan properly conditions zero-environment presentation on the central no-environment path and multiple-target UI on attachments (`task_dag.md:2490-2501`). **Safe burn-down:** keep; preserve provenance and filters while adding optional/multiple-environment projection; do not equate ID-keyed reads with environment-free presentation.

### `APP_API_GRPC_FACADE` — uniform app gRPC APIs with generated HTTP facades

**selected, unscheduled, still open.** The DAG enumerates remaining configuration/Sandbox creation, Action policy, system status, Thread and console streams, typed Session overrides, compatibility and non-JSON error behavior (`task_dag.md:2517-2553`). `AppService.OpenSession` still uses `Struct` / `Value` for `spec` and `setup_script` (`app_service.proto:163-174`); `grpc_http_facade.md:20-39` enumerates remaining items. #14264 only burned down completed facade slices. **Safe burn-down:** retain as unscheduled. Split only when a concrete API migration is dispatched; preserve omitted/null/empty and uncertain-opening semantics.

### `WEB_PUSH_SERVICE` — Web Push as its own sender

**conditional decision, not active implementation.** The recorded trigger is that non-Action-approval Web Push producers arrive; current Action ownership is clean while Notifications serves agent inboxes (`task_dag.md:2555-2564`). No evidence in this audit showed the trigger occurred. **Safe burn-down:** preserve as an explicitly deferred/conditional decision (or freezer item if no longer a dispatch candidate); do not build a service or delete the decision solely for age.

### `HARNESS_IDENTITY_DESIGN` — decide private Session-to-store and native mapping

**decision, still open.** Existing Runtime state keys a retained `SessionRecordSpec` by public Session ID and persists harness type, native ID and execution principal (`env/runtime/cosistate/persistent.proto:19-30`; `retained/resources.go:105-118,167-234`; `session/native.go:191-204`). It has no explicit runner-owned store ID or durable store-to-exact-Sandbox binding. Process/store count, shared-store safety and private mapping crash recovery remain questions (`task_dag.md:2566-2610`). **Safe burn-down:** keep the design node; treat current per-Session record/native ID as existing foundation, not proof of selected store identity.

### `RUNNER_STORE_SESSION_IDENTITY` — implement durable store and public Session mapping

**Still open; blocked on `HARNESS_IDENTITY_DESIGN`.** The planned durable runner-owned store ID, exact UID binding, restart/write fencing, migration and unknown native-create reconciliation are not present in the persisted spec (`task_dag.md:2611-2623`; `persistent.proto:19-30`). **Safe burn-down:** keep blocked on the reviewed mapping contract; preserve authored native data and use a new migration when selected.

### `RUNNER_IDENTITY_LEGACY_CLEANUP` — retire only obsolete identity assumptions

**Still open; blocked on `RUNNER_STORE_SESSION_IDENTITY` rollout.** The plan correctly requires auditing saved templates, pinned wheels and all callers only after the private mapping rolls out; it prohibits deleting stores, native conversations or History (`task_dag.md:2625-2632`). **Safe burn-down:** keep behind the mapping rollout and independent public-address gates; no cleanup is currently justified.

### `FAKE_CLIENTS_TO_ENVTEST` — the remaining fake API clients move to envtest

retain all four listed files; each currently imports/builds a fake Kubernetes client.

### `K3S_TEST_GUEST_RETIRE` — delete the k3s test guest

retain: agentplane/system_tests/k3s/guest and the separate production env/images/vm.nix guest remain present.

### `SAMPLER_JOURNAL_DIET` — what the sampler journals

retain decision; central_sampler/runtime.go:1008 stores a provider request intent and store.go:1390 begins the durable intent.

### `ACTION_SERVICE_PORT_SHARING` — a simpler single Action Service address

retain decision: action_service/main.go:529 uses graceful.NewShared.

## Deferred task freezer

The following are source-level assessments at the same baseline. Deferred items remain conditional, not dispatch commitments. The recovery protocol item was removed in this burn-down; the separate recovery and no-replay decisions remain open.

- **`AGENT_PLATFORM_INFORMATION_DESIGN`:** **Decision open; operator-deferred.** The item explicitly coordinates platform guidance ownership and service names (lines 21-24); no transport/service is selected.
- **`WORKLOAD_AUDIENCE_RENAME`:** **Open; explicitly deferred.** `agentplane/workload_auth/audience.py:1-4` still sets `agentplane-egress`; this is a cross-verifier token audience.
- **`EXTERNAL_AGENT_INBOXES`:** **Decision open; deferred.** V1 API says recipients are Sandbox-hosted Sessions and inbox ownership is the Sandbox ServiceAccount (`notification_service/notifications.proto:117-129`; `README.md:10-17`).
- **`DELEGATED_INBOX_ACCESS`:** **Decision open; no delegated read/ack.** `api/notifications.go:84-100` derives owner from caller account and Sandbox identity; `inboxaccess/policy.go:17-21,29-45` allows only `send_message`; proto documents account-owned inboxes (`notifications.proto:14-18`).
- **`NOTIFICATION_DISPATCHER_OWNERSHIP`:** **Decision open; current boundary implemented.** Notification is standalone and sends notices through Dispatcher (`notification_service/README.md:3-8`); it has its own database and independent dependencies (`README.md:42-50`).
- **`SANDBOX_EVENT_WAKE_POLICY`:** **Open; no per-event switches.** App documents operator control of Sandbox wake-on-message/idle-suspend and does not wake for diagnostics (`app/README.md:140-148`).
- **`NOTIFICATION_WORKER_ISOLATION`:** **Open; no operational trigger found.** `notification_service/worker/worker.go:53-88` runs one worker loop; replicas share database claims/leases (`worker.go:1-7`).
- **`NOTIFICATION_ACTION_FEED`:** **Partial prerequisite exists; integration still open and cost-triggered.** Action Service already provides `WatchActionRequests`: snapshot/upserts, coalescing, fresh snapshot on reconnect, and no replay cursor (`action_service/action_service.proto:37-44,98-105,239-253`). Empty state means all requests. Notification still has canonical reads from `GetActionRequest`/`ListActionRequestEvents` (`notification_service/actions/source.go:41-81`) and reconciles from stored sequence (`:84-99`); successful source reads schedule another Action read in five seconds (`notification_service/store/actions.go:19-30,35-44`), and the worker sleeps until durable due work (`worker.go:115-122`).
- **`NOTIFICATION_SOURCE_DISCOVERY`:** **Open; trigger not selected.** Subscription creation currently supports Action and GitHub source types (`notification_service/api/subscribe.go:45-61`); the freezer ties aggregate source discovery to a future GitHub UX improvement and issue #8981.
- **`NOTIFICATION_SUBSCRIPTION_LIFETIME`:** **Current expiry is implemented; lifecycle choice remains open.** Proto accepts 1–30 days, defaults absent input to seven (`notifications.proto:148-159`); persisted row has `expires_at` (`store/subscriptions.go:68-70`).
- **`NOTIFICATION_SOURCE_SCOPE`:** **Open; no additional source use case selected.** Current subscription dispatch accepts Actions or GitHub (`notification_service/api/subscribe.go:45-61`); broader subscription authority remains a separate DAG design prerequisite per freezer:104-105.
- **`NOTIFICATION_DELIVERY_SCOPE`:** **Open; central/no-Environment delivery remains separately active.** Current message API resolves a Dispatcher opening, exact Sandbox UID, and account grant (`notification_service/notifications.proto:120-129`; `api/messages.go:45-75`). The DAG tracks zero-Environment delivery separately as `CENTRAL_SESSION_NOTIFICATIONS`.
- **`NOTIFICATION_NOTICE_PACING`:** **Partial foundation implemented; stage-aware design remains conditional.** Worker has durable quiet/max debounce settings (2s/10s default; `worker/settings.go:22-43`); `PrepareNotice` batches through last cursor and retains an in-flight notice (`store/notices.go:72-109`).
- **`NOTIFICATION_SOURCE_WIRING`:** **Partial foundation already implemented.** Provider interface exists (`notification_service/worker/provider.go:12-22`); Actions implements it (`actions/source.go:33-39`) and GitHub has a separate Provider.
- **`HOME_ASSISTANT_NOTIFICATIONS`:** **Open; no selected source/use case.** This is an optional source family, not a current limitation in the selected Action/GitHub subscriptions (see `notification_service/api/subscribe.go:45-61`).
- **`KUBERNETES_MONITORING`:** **Open; no rollout-following product use case selected.** No current Notification source is identified for Kubernetes rollout subscriptions; freezer explicitly conditions it on a selected use case and subscription authorization.
- **`AGENT_MESSAGE_CLASSIFICATION`:** **Optional decision open; outside v1.** Freezer states it must follow an explicit leakage-review requirement and never replace RBAC or silently discard accepted messages.
- **`INBOX_VACUUM`:** **Open; ordinary retention exists, live-inbox removal is not selected.** Retention cleanup runs periodically (`notification_service/worker/worker.go:27-30,91-110`); message API says a purged message lookup is `NOT_FOUND`, which does not prove non-delivery (`notifications.proto:140-144`).
- **`LIVE_CLEAN`:** **Open; cost trigger not demonstrated.** Action heartbeats upsert by executor ID (`action_service/store/executions.go:313-321`); liveness notes that random per-process executor IDs leave stale rows and considers scale acceptable pending a cleanup need (`agentplane/docs/executor_liveness.md:113-115`).
- **`CLAUDE_PERMISSION_MODE_REVIEW`:** **Selected deployment setting implemented; reconsideration remains explicitly deferred.** Claude adapter launches `--permission-mode bypassPermissions` (`env/runtime/claude/adapter.go:81-98`); protocol roster says deployed runner uses it (`native/docs/protocol_roster.md:67`).
- **`INPUT_DELIVERY`:** **No specific remaining gap named; not a standing test task.** The freezer correctly limits this to a native queue/interrupt/recovery gap for an adopted operation. Runtime control docs distinguish durable admission from effect (`docs/runtime_control.md:3-11`).
- **`CLAUDE_RECOVERY`:** **Open only if automatic recovery is selected.** No new authorization to replay the native-effect-before-durable-receipt window is present in this item.
- **`CODEX_RECOVERY`:** **Open only if automatic recovery is selected.** Codex history can inform display reconciliation but does not establish that arbitrary external effects are safe to replay (`env/runtime/codex/history.go:18-20,69-76`).
- **`CODEX_RECOVERY_PROTOCOL`:** **Done at this baseline.** Codex adapter uses public paginated `thread/items/list` and explicitly forbids private-storage fallback (`env/runtime/codex/history.go:18-20,148-194`). The investigation records the selected pin, real-binary exercise and retained/absent/revised/unknown handling (`debug/codex_app_server_history_apis.md:146-183`); Go tests cover pagination and incomplete/unsupported results (`env/runtime/codex/history_test.go:78-149`).
- **`LLM_INGRESS_REQUEST_RECENCY`:** **Recency feature remains open; its “not Session identity” assertion is stale/incomplete.** Central sampler attaches `x-agentplane-session-id` on sampling and compaction requests (`central_sampler/runtime.go:1007-1014,1100-1106`). Ingress accepts canonical IDs only from configured trusted sampler accounts (`llm_ingress/server.go:211-227`) and constructs verified metadata while stripping caller fields (`:347-414`); test proves ordinary-runner denial and forged-field stripping (`ingress_test.go:902-955`). This does not establish native Runner attribution or Dispatcher ownership/existence.
- **`CLAUDE_FRESH_RESUME_CACHE_SPIKE`:** **Open; no measured fresh-process cost trigger.**
- **`CODEX_FRESH_RESUME_CACHE_SPIKE`:** **Open; no measured fresh-process cost trigger.**
- **`RUNNER_IMAGE_UPGRADE_PROOF`:** **Conditional; baseline explicitly says Codex manual pause/patch/resume is already proven.** No automatic image-upgrade product is selected (`task_freezer.md:196-202`).
- **`RUNNER_IMAGE_ROLLOUT`:** **Open; operator-assisted/automatic fleet rollout not selected.**
- **`HARNESS_CONFIG_ISOLATION`:** **Existing isolation is in place for Claude, but future features still need this gate.** Claude launch suppresses project settings (`claude/adapter.go:81-99`) and gets a Session-local config directory (`:112-128`).
- **`DT`:** **Open; no driver declaration/background-control product selected.**
- **`HARNESS_MCP_TASKS_EVAL`:** **Open; interoperability not selected.**
- **`HARNESS_SKILLS`:** **Open; project-scoped skills/commands not selected.**
- **`HARNESS_WEB_SEARCH`:** **Open; routed search not selected.**
- **`THREAD_UPLOADS`:** **Open; upload capability absent from the current composer.** `app/frontend/threads/composer.tsx:64-82` renders a text-only textarea and controls.
- **`HARNESS_VISUAL_INPUT`:** **Open; depends on upload contract.** Current composer has no attachment path (`composer.tsx:64-82`).
- **`CODEX_IMAGE_VIEW_ITEM`:** **Open; conditional on Agentplane needing workspace image-view history.** Current item text identifies the native `{id,path}` payload/status gap; it explicitly separates workspace view from composer uploads.
- **`HARNESS_MANUAL_COMPACTION`:** **Open; no manual compaction control selected.** Current composer exposes send/interrupt/resume controls only (`composer.tsx:84-108`).
- **`CLAUDE_REMOTE_IO_EVAL`:** **Open; stream-json remains selected.** Claude adapter launches `--output-format stream-json` and `--input-format stream-json` (`claude/adapter.go:81-98`).
- **`HARNESS_INTERACTIVE_CONTROLS`:** **Open; no question/permission UI selected.** Deployed Claude permission mode is bypass as above; no durable parked/answered/recovered control model is selected.
- **`HARNESS_PROJECT_HOOKS`:** **Open; trusted project-hook feature not selected.**
- **`HARNESS_PLUGINS`:** **Open; plugin feature not selected.**
- **`HARNESS_PROMPT_SUGGESTIONS`:** **Open; intentionally disabled in Claude launch** (`claude/adapter.go:81-87` includes `--prompt-suggestions=false`).
- **`NATIVE_SUBAGENT_THREADS`:** **Partial native evidence; linked Agentplane child Threads remain open.** Codex projects `subAgentActivity` identity/path as native items (`codex/adapter.go:599-611`), but freezer and native evidence say child authorization and independent control are unsettled.
- **`CONTROL_STATE`:** **Partial control capability exists; generic capability state remains open.** `ChangeModel` is a durable command and has harness-specific effect semantics; reasoning effort lacks a common live mutation (`docs/runtime_control.md:3-11,32-46`).
- **`CENTRAL_SAMPLER_RELIABILITY_FOLLOWUP`:** **Operator-deferred; no incident provided in this audit.** Existing tests/contracts and rollout evidence remain authoritative by item text.
- **`CENTRAL_SAMPLER_PREFIX_RECOVERY`:** **Decision open; selective prefix recovery not selected.** The freezer records provider-route incompatibility and opaque-content/order risks; existing whole-response abandonment does not authorize replay.
- **`RUNNER_STATE_BOUNDARY_RETHINK`:** **Central-sampling branch done; standalone legacy-runner redesign remains deferred.** Freezer records central sampler shipped and runner durability retained; runner contract still uses private mTLS gRPC and runner-owned journal/control (`docs/runner_control.md:1-11,29-45`).
- **`RUNNER_JOURNAL_SETTLEMENT`:** **Open; measured streamed-delta disk pressure not supplied.**
- **`CLAUDE_OFFLINE_CATCHUP`:** **Open; conditional on thin-runner design.**
- **`CODEX_OFFLINE_CATCHUP`:** **Open; conditional on thin-runner design.**
- **`RUNNER_OUTBOUND_CUTOVER`:** **Open; thin-adapter/central-authority design not selected.** Current runner uses private mTLS control (`docs/runner_control.md:1-11`).
- **`NEWTHREAD_DURABLE`:** **Open; combined submit-and-close-browser provisioning is not offered.** Current new-thread form creates a Session from setup/spec fields (`app/frontend/threads/new_thread_form.tsx:197-230`); the app documents durable opening reconciliation separately (`app/README.md:152-177`).
- **`UISHELL_NEWTHREAD_SANDBOX`:** **Open; depends on selected durable combined-start contract.** Current form is scoped to Sandbox preset/configuration (`new_thread_form.tsx:25-42,70-97`).
- **`UISHELL_NEWTHREAD_LANDING`:** **Open; same prerequisite as scoped composer.**
- **`THREAD_ON_DEMAND_RUNTIME`:** **Open; depends on Session revival and reviewed wake authority/budget.**
- **`AG`:** **Open product capstone; hosted Agent/Thread product not selected.**
- **`THREAD_TITLE_GENERATION`:** **Removed feature; operator-deferred pending #11817.** App README says Thread name/archive are operator-set (`app/README.md:59-62`); store explicitly calls rename operator-controlled (`app/go/threads/store.go:138-155`).
- **`T3`:** **Transcript search remains open; ordinary browsing exists.** History lookup has Session ID and Sandbox name/UID selectors (`history_service/history_service.proto:260-282`); this is not transcript full-text lookup.
- **`ELEVATE`:** **Authority decision open; no temporary permission workflow selected.**
- **`ACCESS`:** **Decision open.** External delegated-versus-brokered access is not decided by current egress wiring (freezer:369-371).
- **`EGRESS_CHANGE`:** **Authority decision open; no agent-requested policy expansion selected.**
- **`FORK`:** **Open; concrete concurrent external-agent identity need not selected.**
- **`SSHDURABLE`:** **Separate product need, not an Agentplane Sandbox prerequisite.** The freezer points to the separate SSH durable-process plan.
- **`ANTHROPIC_INCLUDED_API_ROUTING`:** **Open; no confirmed eligible subscription-linked credit use.**
- **`AIQUOTA_ANTHROPIC_API_CREDIT`:** **Open; no confirmed data source/usefulness.**
- **`HISTORY_SESSION_REKEY`:** **Open only if public Session UUID uniqueness changes.** History contract addresses reads by Session ID (`history_service/history_service.proto:13-28,208-214`) and separately includes Sandbox UID/provenance (`:230-240`).
- **`API_AGGREGATION`:** **Operator-held decision remains open; stated Go prerequisite is stale/satisfied.** Action Service is a Go implementation (`action_service/main.go:1-8`); exploration itself says it now runs in Go (`plans/api_aggregation_exploration.md:3-8`) but retains “once Go” in its recommendation (`:116-120`).
- **`RUNNER_IDLE_HARNESS_REAP`:** **Open; idle-RAM measurements and safe eligibility policy are absent.** App docs say idle Sessions may be archived while harness remains live (`app/README.md:149-151`); freezer correctly distinguishes Claude per-Session process from Codex shared Runner process and warns `idle_since` is insufficient.
- **`FRONTEND_QUERY_CACHE`:** **Deferred refactor; no correctness trigger.** Freezer describes fetch-in-effect/cancellation code and an existing Action cache as an idiom opportunity.
- **`FRONTEND_CONNECT_ES`:** **Decision open; current gateway choice remains active.** App uses generated Gateway ProtoJSON plus its retained-event SSE/NDJSON bridge (`app/README.md:20-31`).

The freezer also contains a broader verification policy rather than a task: retain its trigger-based approach. This audit did not perform a live operational check.

## Component TODOs and sampler plans

The following entries were audited against the same source baseline. They remain open or conditional unless identified as context; current deployment and external credentials were not checked.

## `agentplane/action_service/TODO.md`

1. **Dynamic Client Registration for MCP OAuth linkage** (`:5-8`) — **open**. Linkage configuration accepts a registered `client_id` or shared CIMD (`linkage/settings.go:12-30,41-75`); no link-time RFC 7591 registration path was found. Shared CIMD is not per-link DCR.
2. **`client_secret_basic`** (`:10-14`) — **open**. The token exchange explicitly uses `oauth2.AuthStyleInParams` (`linkage/flow.go:282-285`), so this code sends the client secret in the form body.
3. **Per-group Agent tool denylist** (`:16-19`) — **open**. MCP mirroring skips tools only when their schemas/catalog entries cannot be represented (`mcpexec/tools.go:28-81`); no configurable per-group denylist was found. `direct_tools` selects tools to expose directly; it is not an upstream tool denylist (`action_service.proto:567-571`).
4. **Expose half-broken group states and reasons** (`:21-29`) — **partial**. Existing health fields cover lifecycle and connection-level failure/retry detail (`action_service.proto:587-599`). Invalid/skipped tools are collected during mirroring and logged (`mcpexec/tools.go:28-81`; `mcpexec/executor.go:240-255`), not included in `McpHealth`. The detailed per-tool skip list and reason, workload-safe coarse count, and proposed stale/partial states remain open.
5. **Home Assistant entity control** (`:31-38`) — **open decision/implementation**. The policy kind constrains `ha_call_service` to configured entities and services (`policies/home_assistant_entity_control.py:1-8,22-45`). The CDK factory docstring says to add a factory when this repo builds one (`cluster/cdk8s/providers/agentplane/policy_set.py:88-95`); staging currently binds the separate reviewed read-only policy (`actions_staging_policies.py:791-805`). No entity-control factory/binding or reviewed write allowlist was found.
6. **Executors answer as MCP tools** (`:40-46`) — **open architecture proposal**. The MCP edge dispatches stored MCP `CallToolResult` separately from POD/orchestration/delegation object results (`agentplane/mcp_edge/mcpserver/results.go:47-50,112-133`). No unified stored result shape for all executors was found.
7. **Submit only if policy decides** (`:48-53`) — **partial**. The admission API honors `decided_only` and rejects `NoOpinion` before storage (`agentplane/action_service/api/submit.go:63-73`); direct tool calls set it (`agentplane/mcp_edge/mcpserver/direct.go:190-214`). Generic `request_action` does not expose/set the field (`agentplane/mcp_edge/mcpserver/tools.go:80-91,210-227`). Narrow the remaining work to the generic request tool.
8. **Refuse when caller’s own permissions already cover an Action** (`:55-63`) — **open proposal**. Action admission evaluates Action auto-approval policies (`action_service/api/submit.go:63-73`); no caller-identity Kubernetes SubjectAccessReview guard was found in Action Service. The historical Haku behavior does not itself prove this policy is implemented here.
9. **Direct tools per Connection** (`:65-69`) — **open**. `direct_tools` is configured on `ActionGroup` (`action_service.proto:567-571`; catalog validation at `catalog/catalog.go:375-392`). The MCP edge filters using caller policy, but no per-Connection/per-caller configured tool selection was found (`mcpserver/direct.go:74-103`).
10. **Consider shared GitHub observation service** (`:71-79`) — **decision, intentionally not a committed extraction**. Action visibility checks remain under Action Service (`action_service/policies/visibility.go`, `config.go:50-89`, `main.go:473`); notification webhook/repository/commit observations remain under Notification Service (`notification_service/github/`, `notification_service/store/github_commit_observations.go`, `notification_service/model/github_repository.go`). Keep the explicit possibility and preserve distinct public-visibility, installation, and operator authorization facts; do not mark done merely because both services have GitHub code.

## `agentplane/app/TODO.md`

1. **Load app config as protobuf and use one representation** (`:3`) — **partial**. Generated ProtoJSON routes and methods already expose model/preset views (`app/go/appconfigapi.go:14-25,28-74`), but actual config structs and parsing remain Go/mapstructure/Viper/YAML (`app/go/config.go:16-28`). The unified load-time representation remains open.
2. **Share and incrementally maintain PolicyService watches** (`:5-6`) — **open**. Each `WatchPolicyBindings` call opens its own watch and its read callback relists the namespaced objects (`policy_controller/service/service.go:75-115`). `WatchPolicySets` similarly opens binding/set watches and performs fresh list reads (`:131-180`). The separate consumer-side shared informer/readers do not change this RPC implementation.
3. **Decide Sandbox preset ownership** (`:7-9`) — **decision**. Current code has app-owned preset configuration (`app/go/config.go:24-28,73-80`) and emits Session defaults from preset views (`app/go/appconfigapi.go:55-74`); the Environment stores the label and the app resolves defaults (baseline app README `:563-593`). Current behavior preserves the explicit open ownership/replacement-flow decision; do not convert implementation into a resolved ownership decision.
4. **Retire legacy browser-retained command destination** (`:10-12`) — **partial migration / safety decision remains**. New saved commands record `sessionAddressed`, but still persist `target`; decode/encode preserve both (`app/frontend/threads/local_commands.ts:103-124`). The captured destination is intentionally not looked up on retry (`:142-157`), while original Sandbox UID fencing and uncertain-command non-replay remain required. Keep until old-record reconciliation and safe shape retirement are implemented.
5. **Generated HTTP clients and reusable protobuf-watch reducers** (`:13-15`) — **open**. Browser clients still spell HTTP routes and assemble requests/responses manually; for example Sandboxes use hand-built routes and ProtoJSON calls (`app/frontend/sandboxes/client.ts:121-206`). The TODO is an investigation, not evidence generated APIs/reducers exist for the named EnvironmentService client behavior.
6. **Environment-filtered Thread listing/query** (`:17-26`) — **open design/implementation**. Browser listing pages app metadata and calls History `BatchGetSummaries` per page (`app/frontend/threads/client.ts:475-552`). Server listing calls `FindSessions`, pages metadata, and batches summaries (`app/go/threadreadapi.go:208-264,335-380`); archived count scans metadata pages and batches again (`:229-263`). Keep the central Session/Environment relationship and exact UID attribution constraints; do not count the current query as a standalone optimization already completed.

## `agentplane/app/frontend/TODO.md`

1. **Generate thread tool schemas from their implementations** (`:5-9`) — **open**. `threads/shell_calls.ts` contains hand-maintained strict schemas for Claude `Bash` and Codex `commandExecution` (`:26-40`), with schema-based parsing (`:53-85`). Harness field additions can therefore still fail the special renderer and fall back to JSON.
2. **Thread view UX** (`:11-27`) — **decision/review, no feature burn-down**.
   - Observation terminology remains visible as “Observation N raw frames” (`threads/thread_evidence.tsx:107-119`) and the stored event field/API vocabulary is still observation-oriented. Whether to rename across layers remains a deliberate design review.
   - The user bubble and opened disclosure cards remain (`threads/conversation.css` around the `.transcript-user-bubble` rule; `threads/thread_cards.tsx:600-637,804-814`). The visual proposal is not already implemented simply because assistant text is unboxed in one state.
   - Badge de-duplication is only within the items passed to one `statusBadges` call (`threads/thread_cards.tsx:827-850`); history-wide grouping/softening of repeated interruption badges remains a UX decision.
3. **Hidden characters in ordinary Action text** (`:29-36`) — **open**. Action title/description still render as ordinary `<Text>` (`app/frontend/actions/call.tsx:118-128`). Ordinary one-line tool summaries also use `<Text>` (`threads/thread_cards.tsx:600-615`); code-shaped arguments/results use a separate viewer. Keep the decision on inline markers or approval warning.
4. **Durable local storage for thread windows** (`:38-45`) — **open**. `RetainedThreads` is an in-memory `Map` with a bound of eight left threads (`threads/electric_sync.tsx:1237-1262`); it does not persist a window across reloads or tabs. Local command recovery in `local_commands.ts` is a different store and does not satisfy this item.
5. **Bound read-ahead by payload size** (`:47-53`) — **open**. `ThreadPayloadReference` has `chunk_count` but no byte-size field (`history_service/history_service.proto:357-367`); frontend read-ahead queues by chunk count and limits only the batch to 20 bodies (`threads/electric_sync.tsx:525-546,649-658`). The persisted payload manifest’s `content_bytes` and a cross-window memory budget are not surfaced in this code path.
6. **Multi-operator Action outcome notifications** (`:55-57`) — **deferred decision**. The entry is explicitly conditional on becoming multi-operator/multi-user. No evidence in this scope establishes that condition or delivers this notification; preserve the trigger rather than treat unrelated Action approvals as completion.
7. **Kubernetes API probe on System page** (`:59-63`) — **open**. App probes cover Runner Gateway, History, Action, Connection, Notification, egress, and optional Electric (`app/go/runtime.go:408-450`); no Kubernetes API probe is appended.

## `agentplane/egress/TODO.md`

1. **Scoped Authentik inspection credentials** (`:3-8`) — **decision**. This first asks whether agents need scoped service accounts and an endpoint/permission scope. The generic proxy substitutes only credentials admitted by policy; no Authentik-specific scoped credential source was found. Keep the decision and secret-handling requirement.
2. **Evaluate upstream connection reuse** (`:10-16`) — **open measurement**. Proxy creates an `http.Transport` in each `relay` invocation and disables keep-alives (`egress/proxy/proxy.go:272-296`); the custom round tripper calls that transport (`:303-315`). The Iron spike is not a benchmark of the standard-library option. Preserve the stated per-request policy, pinned DNS, streaming, revocation, and WebSocket constraints.
3. **Review projected-token substitution location** (`:18-33`) — **unresolved architecture decision**. Sidecar-to-proxy carries `X-Agentplane-Workload-Token` (`egress/identity/identity.go:77-95`; relay handoff in `egress/sidecar/relay.go:301-315`), and the proxy verifies audience and Pod identity before allowing substitution (`identity/identity.go:98-125`; relay policy path `proxy/proxy.go:272-315`). Existing enforcement therefore implements the described central-proxy choice; it does not resolve the explicit sidecar-vs-proxy decision. Keep.

## `agentplane/native/TODO.md`

1. **Scripted automatic compaction test** (`:10-34`) — **open**. Current native test exercises `/compact` (`native/claude/test_compaction.py:16-60`), while the TODO asks for automatic compaction frames on pinned 2.1.252 and explicitly notes the old live capture was 2.1.233. Manual compaction is not the requested auto-compaction evidence.
2. **`--include-hook-events` for initialize hooks** (`:36-47`) — **open/unverified conflict**. The current hook tests assert `PreToolUse` callbacks (`native/claude/test_hooks.py:20-80`); no initialize-hook-frame assertion/positive-control test was found. Scenario docs describe harness hook reporting (`native/claude/scenarios.py:62-74`) but do not settle the recorded captures’ conflict.
3. **What triggers `command_lifecycle`** (`:49-58`) — **open/unverified conflict**. Wire docs say these frames are emitted per user UUID under `--replay-user-messages` (`native/claude/wire.py:94-104`), and scenario code adds that flag only when requested (`native/claude/scenarios.py:55-64,109-110`). No UUID-only positive / UUID-less negative control was found.
4. **Synthetic compaction summary in `UserFrame`** (`:60-66`) — **open**. Native `UserFrame` models replay echoes/tool results but no `isSynthetic` field (`native/claude/wire.py:236-247`); Go Claude wire frame has `IsReplay` but no synthetic flag (`env/runtime/claude/protocol.go:12-29`). Current manual compaction test does not establish auto-summary handling (`test_compaction.py:31-52`).
5. **Notification acknowledgement ID** (`:68-78`) — **partial**. Driver code always responds to `notifications/initialized` with ID `0` (`native/claude/testing/mcp.py:20-23,41-56`). The entry’s `null`/omitted alternatives on pinned version remain untested; keep the comparison, not the base implementation.
6. **Wait for unanswered `can_use_tool` / hook requests** (`:80-91`) — **open**. No current scripted test leaves these requests unanswered to a measured result/timeout. Hook documentation’s budget does not establish the separate `can_use_tool` behavior.
7. **Capability advertisement on pinned CLI** (`:93-98`) — **open**. The plan identifies old 2.1.220 capture; no assertion of the 2.1.252 `system/init.capabilities` fields was found in native tests.
8. **Harness-neutral compaction boundary** (`:100-107`) — **open design**. Claude history handles `compact_boundary` (`env/runtime/claude/history.go:86-90`); Codex parses `ContextCompaction` separately (`env/runtime/codex/history.go:178`). Provider evidence remains distinct; no shared native/runner event with the specified ordering/resume contract was found.
9. **Version context** (`:1-8`) — **context**, not a task: the document pins its claims to Claude Code 2.1.252 and points white-box-only questions to the separate reverse-engineering project. Preserve as evidence provenance; do not count it as a burn-down node.

## `agentplane/system_tests/TODO.md`

1. **Compare VM test and production/staging backing storage and guest behavior** (`:3-6`) — **open decision/measurement**. Test rendering asserts `BlockFilesystem`/ext4 (`agentplane/system_tests/k3s/test_render.py:449-459`) and system-test docs describe its local-path/test storage setup (`system_tests/README.md:226-236`). Staging VM generation also declares BlockFilesystem/ext4 but uses `piraeus-ssd-2-autodiskful` (`cluster/cdk8s/agentplane/environment_sandbox.py:134-149,220-232`; staging assertion `test_environment_vm.py:159-176`). Matching guest layout declarations do not prove backing storage/boot/format/mount equivalence or benchmark results.

## `agentplane/x/indexing/TODO.md`

1. **Ingest by tree diff** (`:3-5`) — **open, with a safety-sensitive narrowing**. New snapshots still call full recursive `read_tree` (`agentplane/x/indexing/source.py:100-114,26-57`); store persists path-to-blob membership for the full desired snapshot and reconciles removals (`agentplane/x/indexing/store.py:168-213`). A diff implementation would still need a trustworthy prior tree/manifest and complete path membership, including deletions/renames; “only needs the blobs `git diff` names” is too narrow if interpreted as enough information to construct a snapshot by itself. Preserve the optimization idea but specify how complete manifests are maintained.

## `agentplane/central_sampler/plans/anthropic.md`

- **Intro / implemented scope** (`:1-8`) — **context**. Native Anthropic adapter, conversation storage, and protocol code exist; README describes native storage and the acceptance record distinguishes automated evidence from staging rollout (`central_sampler/README.md:211-237`; `docs/anthropic_acceptance.md`). Treat the implementation as context. I did not independently inspect live deployment state in this source-only pass. Temporary instructions and task budgets are expressly excluded and should remain excluded unless scope changes.
- **BuildBuddy credential provisioning** (`:10-17`) — **deferred/open external provisioning**. Live test targets read `ANTHROPIC_API_KEY` (`central_sampler/BUILD.bazel:122`; `test_live_anthropic_provider.py:3,135`) and README says BuildBuddy must provide it securely (`README.md:141-145`). Staging’s runtime key wiring (`cluster/cdk8s/agentplane/staging.py:471-472,768-785`) is not proof that direct live-test credentials are provisioned in BuildBuddy. Preserve explicit authorization/spend/model limits.
- **Dynamic tool definitions** (`:19-34`) — **open**. Provider tool definitions are built from static `toolDefinitions()` (`central_sampler/provider_dispatch.go:12-18`); README states dynamic definitions remain (`README.md:223-235`). No within-Session provider-visible/runtime-catalog update and replay contract was found.
- **Background compaction** (`:36-65`) — **open; foreground compaction is done**. README identifies ordinary on-demand compaction as implemented and background compaction as deferred (`README.md:229-235`); the Anthropic adapter has foreground compaction handling (`anthropic/protocol_adapter.go:151-162`, `anthropic/compaction.go:18-103`). Do not remove the background concurrency/fencing/restart/retained-suffix requirements based on foreground support.
- **Optional Langfuse export** (`:67-74`) — **deferred decision**. README says native exchanges stay in sampler storage and Langfuse export remains an independent deferred follow-up (`README.md:236-237`); acceptance record says the same (`docs/anthropic_acceptance.md:129`). Existing Langfuse links/search are not export of canonical exchanges.

## `agentplane/central_sampler/plans/codex_parity.md`

- **Introduction and code-site summary** (`:1-6`) — **mixed: gaps remain; one locator claim unverified**. Current code has TODO evidence for timezone (`environment_context.go:13`), steering ordering/interrupt stream cuts (`runtime.go:949-960`), and cell limits (`codemode_host.go:237-239`). I did not find a corresponding TODO marker for base-instruction pruning. Base instructions are still sent in full (`responses/provider.go:82-85`, `anthropic/anthropic.go:128`, `protocol/instructions.go:5-8`), so do not classify that feature gap as done; correct the locator wording only after confirming intended scope. Purposeful exclusions remain documented in `central_sampler/README.md:277-290` and `SPEC.md:518`.
- **Selected workflow work** (`:8-19`) — **partial with explicit acceptance decision**. Delegation interfaces and scripted workflows exist and are described as exposed in staging; implementations include orchestration and session-delegation executors (`action_service/action_service.proto:580-584`, `action_service/orchestration_v1/`, `action_service/session_delegation/`). The plan explicitly leaves natural-agent discovery/use and domain contracts open, and defers dedicated disruption acceptance pending normal use; those decisions remain unresolved and should not be erased by the existence of scripts.
- **Later** (`:21-38`) — **all remain candidates/open design, not burn-downs**:
  1. Guardian-style approval review is still an Agentplane-wide proposal; Action Service’s existing policy decisions do not implement a Guardian review service.
  2. Agents reading sampler persistence remains open; native storage exists, but that does not establish agent-readable authorized access to retained conversation/evidence.
  3. Actions as nested tools remains open; `provider_dispatch.go:12-18` has a TODO for Action/MCP-backed tools, and dynamic availability is not implemented.
  4. Skills remain explicitly deferred (`SPEC.md:518`); no Codex `SKILL.md` discovery/injection path was found.
  5. Cross-Session memory extraction/consolidation remains a candidate; no such workflow was found.
  6. Persisted Session goals with automatic continuation remain a candidate; no goal persistence/continuation path was found.
  7. Session fork remains an open design across sampler/History/executor; no branch/fork API was found in this plan’s code scope.
  8. Hosted web search remains explicitly conditional; no provider-hosted search tool was found in `toolDefinitions()` (`provider_dispatch.go:12-18`).
- **Low priority** (`:40-51`) — **preserve as low-priority decisions/omissions**:
  1. Realtime/voice remains a low-priority omission; no sampler realtime voice surface was found.
  2. Browser/computer use and dictation remain low-priority omissions; no such tools appear in `toolDefinitions()` (`provider_dispatch.go:12-18`).
  3. `update_plan` is explicitly Agentplane-wide, not sampler-only; retain this ownership decision. Provider definitions do not show a current implementation (`provider_dispatch.go:12-18`).
  4. Hooks and `request_user_input` remain explicitly very-low-priority possibilities; no provider hook surface or asynchronous user-question tool appears in `toolDefinitions()` (`:12-18`).
  5. Root-to-cwd `AGENTS.md` discovery and nested discovery are explicitly deferred (`SPEC.md:518`); preserve this decision.
  6. Turn undo remains very-low-priority and no turn undo API was found.
  7. Context-awareness tools (`get_context_remaining`, `new_context_window`, budget/time reminders) remain optional/low-priority; current `environment_context.go:13-17` supplies cwd/shell/date context, while `toolDefinitions()` (`provider_dispatch.go:12-18`) does not define those tools.

## Explicitly uncertain or not independently verified

- Live deployment/rollout assertions in the Anthropic introduction were not independently checked against the running cluster; this pass audits pinned code and documents only.
- BuildBuddy’s secret store and organization-level direct Anthropic key provisioning are external state; source references establish the target/env contract, not the actual secret value or availability.
- Codex parity’s base-instruction-pruning TODO locator was not corroborated. The feature is still visibly absent, but confirm whether the plan intended a code TODO versus an explicitly pending design item before changing the claim.
- Native Claude protocol conflicts that the TODO calls out (automatic compaction on 2.1.252, initialize-hook frame behavior, UUID-only `command_lifecycle`, alternate notification IDs, unanswered-request timing, and 2.1.252 capability fields) remain unverified by the current tests; none is marked done here.
- The system-test storage entry explicitly has unknown performance and no equivalent-workload benchmark in the cited code; no external storage behavior was inferred from the class names.

## Research and architecture plans

The following are source-level assessments at the pinned baseline. Prior-art vendor claims and live deployments were not revalidated.

## `agentplane/plans/README.md`

**Decision/index; generally current.** Lines 3–12 say the DAG is authoritative and plans do not create a competing queue. The index identifies architecture as undecided (21–24), the Electric exception as accepted with optional HA design boundaries (33–34), sampling as custom central sampler shipped but upstream Codex/Claude still open (41–43), and user stories as north star rather than queue (69). These labels match the individually checked plans and current source. Thread sync is correctly called open on pending paging, body compaction and measurement (33), subject to the specific stale test-coverage wording under `thread_sync/README.md` below. No index entry here should be removed solely because it is an exploration.

## `agentplane/plans/api_aggregation_exploration.md`

**Decision / held exploration; a dependency-use statement is stale.** The file says it is not scheduled and describes the Action Service as Go behind the HTTP facade (3–9, 57–58, 117–120); this is consistent with the current Go service. Its cost claim that the repository uses `k8s.io/apiserver` only for `RequestInfo` parsing (52–58) is out of date: current workload authentication imports the apiserver `authenticator`, token cache and user APIs (`agentplane/workload_auth/auth/auth.go:17–21`). Refresh this cost paragraph if the design is revived. The extension-apiserver/SAR alternatives remain proposals; no `APIService` deployment was found in the Agentplane/cluster source at the baseline. The plan itself marks the exploration held, so do not add it to active dispatch based on its technical detail.

## `agentplane/plans/architecture_overview.md`

**Decision record; one present-state description conflicts with the rest of the document and implementation.** The opening dates it 2026-10-19 and says no direction is selected (3–13); keep it as a record of alternatives/operator decisions. The D2 “today’s workflow” passage says deleted Sandboxes leave History, collection membership and archive grants keyed by Sandbox UID (51–54). Only session-level Sandbox identity is stored: `session_history` has namespace/name fields (`agentplane/history_service/migrations/history/00001_baseline.sql:16–30`) and UID was added separately (`.../00002_sandbox_uid.sql:1–2`). The API has `InboxTags` (`agentplane/sandboxes/api/types.go:115–120`), but no HistoryTags or durable History Sandbox record/collection/archive-grant implementation. The document later correctly says those History features are not implemented (424–434); narrow the D2 present-state sentence to avoid contradicting it. Also split lines 302–304 (“right under every direction” still needs operator go) from the already-landed invariants at 306–314: Sandbox UID and InboxTags are implemented, while History tags/record and broader collection/grant direction remain open. The rest of the directions and explicit alternatives (333–347) are decisions, not completed product work.

## `agentplane/plans/claude_remote_io.md`

**Historical, unranked experiment; no runtime cutover.** The header explicitly says this is an evaluation only, with no runtime cutover (3–4). Current runner dependency is Claude 2.1.252 (`MODULE.bazel:1011`); the separate 2.1.292 spike fetch is at `MODULE.bazel:1020`. The production adapter still uses stream-json over the local process boundary (`agentplane/env/runtime/claude/adapter.go:91–96`). `agentplane/native/claude/x/remote_io/` contains the local proxy/server and tests, but it is experimental and not the runtime path. The plan correctly makes version-specific evidence and recovery gates prerequisites. Keep it as historical evaluation; do not mark the production transport changed or dispatch the cutover.

## `agentplane/plans/driver_tools_and_background.md`

**Decision / deferred seam.** Header says the seam is deferred until a named consumer exists (3–7); the contract requires reuse of existing Action behavior. No separate driver-tool/background API consumer is identified as implemented by this plan. This is properly frozen, and the acceptance boundary should remain attached if it is revived.

## `agentplane/plans/electric_ha.md`

**Decision; current implementation agrees with the recorded exception.** The 2026-10-30 audit explicitly accepts one replica and Recreate, with no deployment change scheduled and no claim to live cluster state (3–6). The baseline chart sets one replica and Recreate (`cluster/cdk8s/agentplane/electric.py:68–75`), uses the stated storage/shape limits (`:99–106`), and provides the Pod-local volume and Service (`:118–132`). Multi-active/rolling HA remains an unselected option. No stale current-code claim found; keep the date and source-audit framing.

## `agentplane/plans/external_access.md`

**Decision / open design.** This file distinguishes existing access mechanisms from delegated-versus-brokered choices (3–10, 59–116, 162–168). The per-Sandbox ServiceAccount/projected identity description is consistent with access-profile configuration (`cluster/cdk8s/agentplane/agent_access_profiles.py:63–72,252–275`); the broader revocation gate and compatibility evaluation are still proposals, not delivered guarantees. The Action Service redundancy TODO link at 51–55 resolves to the current TODO. No stale completion claim found. Keep the held decision and its separation from the independently scheduled multiagent implementation.

## `agentplane/plans/grpc_http_facade.md`

**Partially done; remaining app follow-ups and direct-browser option are accurately distinguished.** The plan says four Dispatcher command writes still use the generated facade (9–16); the current app proto still has untyped `Struct spec` and `Value setup_script` fields (`agentplane/app/app_service.proto:163–173`). The Connections UI’s three HTTP-status branches remain (`agentplane/app/frontend/connections/management.tsx:130,153,172`). Those are genuine open follow-ups, as are the typed overrides/attribution/compatibility items in 20–42. Direct browser access at 44–75 is an operator-held option, not a completed implementation. No stale completion claim found.

## `agentplane/plans/sampling_outside_sandboxes.md`

**Partially done; the custom central sampler shipped, upstream agent-loop relocation remains open.** This is a research/decision record dated 2026-10-27 and says there is no placement probe (3–12). The description of today’s Runtime-in-Sandbox placement and its separate Codex/Claude process boundaries (18–31) remains the current architecture. The “first runnable slice” is explicitly a retired fixture, not a deployment (68–77). The accepted custom code-mode sampler is shipped (332–336), with the remaining Anthropic follow-ups separately identified (340–345); running the upstream Codex or Claude agent loop outside the Sandbox remains unselected/unimplemented. Keep those distinctions and do not treat research evidence as migration completion.

## `agentplane/plans/standard_idioms.md`

**Three still-open/decision boundaries; the recheck warning is appropriate.** The file says to recheck accepted idioms before starting (3–11). `DISPATCHER_KEYED_DELIVERY` is partial: the queue is keyed by Session ID, but broad reconciliation remains (`agentplane/runner_gateway/dispatcher/delivery.go:124–159,161–220`; plan 22–27). `DISPATCHER_WATCH_VERSIONS` remains open: PostgreSQL listener exists (`.../dispatcher/watch.go:47–52`), while API watch still re-lists filtered scope (`.../dispatcher/api.go:91–145`) and has no sequence-based resume contract (plan 33–39). `SAMPLER_JOURNAL_DIET` remains an operator decision (plan 43–50): the intent persists full provider request bytes (`agentplane/central_sampler/store.go:1390–1427`; request fields at `types.go:178–202`) while current intent recovery reads only a subset (`store.go:3377–3397`), and evidence records have separate uses (`evidence.go:60–95`). Do not drop evidence or classify the storage decision as done without the operator’s choice.

## `agentplane/plans/user_stories.md`

**North-star stories remain open product goals; one summary sentence is internally stale.** The header explicitly says this is not an implementation queue and a story leaves only when delivered in the app (3–9). Story 1’s Missing section lists both a permission-request Action and reusable standing grants (52–58); the “order of work” summary says Story 1 lacks only standing grants (205–206). Update that summary to include the missing permission-request Action. Existing proxy, Action Service, PolicyBinding and approval-delivery capabilities do not deliver either product promise by themselves. Keep the story set and missing items rather than removing them as shipped infrastructure.

## `agentplane/plans/thread_sync/README.md`

**Partially done; open gaps are mostly accurate, but the restart/stalled-reader test claim needs narrowing.** One Electric shape per thread with subset snapshots is described as deployed (3–11); newest-200 pending subset and absence of older pending paging remain (15–19; current client cap at `agentplane/app/frontend/threads/electric_sync.tsx:273–277`, proxy subset bound at `agentplane/app/go/proxy/electric.go:16–22,284–297`). Server-memory/staging measurement and body compaction remain open (20–42; existing traffic test calls itself one finite workload, `agentplane/app/test_electric_window_traffic.py:1–5`). But “restart and stalled-reader probes … are not written” (22–25) is too broad: `agentplane/app/test_electric_wal_recovery.py:35–130` already covers Electric stop, lost WAL slot, restart, stale-handle 409 and resnapshot. It does not measure memory under long growth or a stalled reader; revise the gap to those missing probes. The `since` prerequisite at 57–59 is only partially covered: `test_electric_subset_window.py:136–195` pins a loaded text-body edit with snapshot transaction metadata/live feed ordering, not every mutation/body-change type. Preserve broader ordering coverage before implementing the candidate protocol.

## `agentplane/plans/thread_sync/option_app_push.md`

**Still-open alternative, not implemented.** The file presents SSE/app push as an option and lists per-reader state, replay and watch-semantics requirements (1–41). Current thread sync remains Electric; the plan itself does not claim an SSE endpoint is deployed. Keep as a candidate, not as a completed task.

## `agentplane/plans/thread_sync/option_moving_window.md`

**Still-open protocol alternative, not implemented.** The proposed moving window and revision-cursor contract are design only. The README’s prerequisite at `thread_sync/README.md:57–59` is not fully covered: `agentplane/app/test_electric_subset_window.py:136–195` demonstrates one text-body edit and ordering behavior, not the full all-mutations contract. Do not say the candidate is ready based on that one case.

## `agentplane/plans/thread_sync/option_window_poll.md`

**Still-open protocol alternative, not implemented.** This is the proposed long-poll/range-and-fetch-body route. Current source has immutable payload manifests/chunks (`agentplane/history_service/projection/payloads.go:64–165`) and range reads (`agentplane/history_service/reads/threads.go:33,68–69`), but no corresponding thread-sync long-poll endpoint/ETag implementation was found. The option is correctly presented as a candidate.

## `agentplane/plans/thread_sync/seams.md`

**Partially implemented seam; second implementation/deployment switch is only proposed.** Current app mounts Electric conditionally (`agentplane/app/go/runtime.go:348–355`), and the proxy serves Electric entity/chunk routes (`agentplane/app/go/proxy/electric.go:80–87`). The browser-facing `ThreadSync` interface and Electric implementation exist (`agentplane/app/frontend/threads/thread_sync.ts:11–65`). The proposed alternate implementation module/deployment selector and migration seam are not present; no separate `agentplane/app/go/threadsync` implementation exists in the baseline. The document correctly frames that boundary as future work.

## `agentplane/plans/thread_sync/prior_art/README.md`

**Historical research index; preserve as historical.** The source survey is explicitly dated 2026-09-23 and relies on then-current vendor documentation/source (3–10). Its “Electric 1.8.1” configured-engine entry still matches the baseline chart pin (`cluster/cdk8s/agentplane/electric.py:30`). The other vendors’ current versions, licenses and capabilities were not rechecked; do not present those as current recommendations without refreshing the citations.

## `agentplane/plans/thread_sync/prior_art/electric_powersync.md`

**Historical vendor snapshot; no Agentplane implementation item.** It is explicitly pinned to a 2026-09-23 snapshot and source commits (3–7); Electric 1.8.1 remains the configured chart version (`cluster/cdk8s/agentplane/electric.py:30`). Other vendor/version/license claims are date-bound and not revalidated here. The line 214 assertion about exact-key dedupe points to a pinned upstream commit, so retain the commit context rather than treating it as a claim about current Agentplane code.

## `agentplane/plans/thread_sync/prior_art/others.md`

**Historical vendor comparison; no Agentplane implementation item.** The comparison identifies itself as a 2026-09-23 external-source snapshot (3–5). Convex, InstantDB, LiveStore, Triplit and Phoenix remain research references rather than Agentplane modules; their present-day product/version claims were not refreshed. Keep the research provenance and date.

## `agentplane/plans/thread_sync/prior_art/replicache.md`

**Historical vendor/protocol snapshot; no Agentplane implementation item.** The document dates its source review to 2026-09-23 and describes an option, not a deployed Replicache integration. Current vendor status, license and version were not checked. Keep only as historical prior art unless a candidate is reopened and revalidated.

## `agentplane/plans/thread_sync/prior_art/zero.md`

**Historical vendor snapshot; no Agentplane implementation item.** The document pins its review to 2026-09-23/source commit and presents Zero as a candidate only. No Zero integration was found in the Agentplane source; external current capabilities/license/version were not revalidated. Preserve as dated prior art, not a current adoption decision.

## `agentplane/egress/plans/credential_transport.md`

**Decision; enforcement is still open and the stated boundary matches current code. The source inventory is stale.** The plan explicitly records no selected enforcement change (3–5). `policy.Request` lacks scheme and port (`agentplane/egress/policy/evaluate.go:16–25`); the gate separately handles destination admission (`proxy/gate.go:70–107`); proxy transport can substitute credentials over cleartext HTTP (`proxy/proxy.go:272–315`). No origin-pair field exists in the rule schema (`agentplane/egress/resources/resources.go:221–236`), so scheme/port enforcement remains open.

The table calls its source audit a snapshot at `b3ec6c38b6` and explicitly says it is desired configuration, not live traffic (21–26), which is sound, but it is not a complete current grant inventory. It omits `quest-read`, defined at `cluster/cdk8s/agentplane/egress_staging_credentials.py:63–74,598–650`, and the `agentplane-workload` projected Session Dispatcher/EnvironmentService credential and gRPC grants at `cluster/cdk8s/agentplane/egress.py:257–270,441–454`. If the inventory is reused for a decision or implementation, refresh it against current cdk8s grant authors. The origin-pair alternatives, internal plaintext exceptions and rollout conditions (44–90) remain operator decision/implementation acceptance, not completed enforcement.

## Examined inventory and limits

All 22 assigned documents were individually read: the 11 named files directly under `agentplane/plans`; all 10 Markdown files under `agentplane/plans/thread_sync/` including `prior_art/`; and `agentplane/egress/plans/credential_transport.md`. Source verification was against the immutable baseline above. No cluster/live-state observations were made. External vendor prior-art claims were intentionally treated as historical and not refreshed. No builds or tests were run because this was a read-only documentation/source audit.

## Environment component TODOs

## env/TODO.md inventory

All listed items remain open, partial or explicitly decision/deferred; the old reservation-write entry is no longer present.

- Prove VM guest boot and console autologin — Still open: TODO.md:3-9 says image/render checks do not boot production guest or prove Runtime readiness.
- Simplify Runtime reconciliation/coordination — Partially done: :11-21 says systemd observations are typed, while revision wakeups/result slots and fan-out/contention measurement remain. Do not replace the worker slot based on the abandoned start-fact prototype.
- Simplify ownership/process fencing with COSI primitives — Partially done/decision: :23-44 lists current PID namespace, Runtime ownership and COSI Process finalizer/native fence, but external effect fencing across Runtime death and simpler systemd marker replacement need analysis and acceptance.
- Reconsider COSI persistence for Session journal — Decision/deferred: :46-57 keeps SQLite authoritative and describes the comparison/contract required before considering a replacement.
- Reconsider Session state model — Decision: :59-72 says retained Session records are COSI/bbolt while journal databases remain SQLite; realistic memory/index/transaction evaluation and ownership contract are not done.
- Use COSI primitives where they fit — Decision/open audit: :74-107 records where cosi.Write/qtransform/mappers differ from current wake, retry, ownership and one-shot-effect semantics. No safe blanket replacement is supported.
- Prove central execution across Pod and VM lifetimes — Partially done: :109-115 records Gateway PID1 integration and Pod suspend/resume coverage; production PID1 Pod and VM systemd loss/replacement/descendant cleanup still need coverage.
- Investigate VM executor health loss during tool OOM — Decision/deferred-on-recurrence: :117-124 states the handshake cause is not established and asks for more timing/socket/process evidence only if it recurs. Do not call it root-caused or adjust the deadline from this report.
- Protect resident harnesses from tool resource exhaustion — Still open: :126-132 and runtime/execution/ISOLATION.md:29-48 state current shared cgroup/failure domain and request a feasible boundary plus OOM-survival/unknown-effect coverage.
- Exercise the shipped harness binaries — Still open: :134-140 says image binaries differ from separately pinned e2e binaries and current smoke checks only versions.
- Decide whether Agentplane should own a base prompt — Decision: :142-146 leaves this product choice open; Runner currently adds platform/Session instructions while retaining harness default prompt.
- Verify Claude interrupted-turn reconciliation across transcript flush — Still open: :148-153 says the native test waits for interrupt marker persistence and does not prove no-flush reconciliation; retain unknown when transcript evidence is insufficient.
- Reconcile compacted conversations — Still open: :155-161 keeps UNKNOWN for Codex compaction/Claude compact_boundary until listed real-binary cases are covered.
- Codex reconcile edge cases — Still open: :163-169 keeps other tools, failed/incomplete command results and encrypted reasoning payload unknown.
- Claude reconcile edge cases — Still open: :171-182 lists untested non-streaming retry, pre-TurnStarted native line, second-read cost, redacted_thinking and interrupted tool-call uncertainty.
- Claude synthetic messages — Decision: :184-190 says UI/context treatment is unchecked and product choice remains open.
- No TODO entry for uncertain Session reservation writes — Correctly absent after #14026; see the Runtime contract and reservation-failure tests.

## runner_gateway/environments/TODO.md

- Python-compatible create-intent serialization — Decision/open compatibility question, not a broken current behavior. TODO.md:3-5 says preserve existing bytes if persisted/uncertain outcomes still depend on them. management/access.go:85-124 records and reconciles immutable intent; :431-480 constructs sorted, compact Python-compatible JSON; access_test.go:115-124 pins exact annotation/idempotent retry and :321-335 tests Python JSON escaping. The tests cover examples, not whether any stored intent can safely be reformatted/migrated. Keep the TODO until persisted annotation compatibility is reviewed.
