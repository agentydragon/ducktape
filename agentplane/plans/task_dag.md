# Agentplane task DAG

This is the dispatch map for **unfinished** work. Detailed contracts belong in component plans;
completed work belongs in component docs, not done nodes here. Maintenance rules are in
[AGENTS.md](../AGENTS.md#task-dag-maintenance). Deliberately deferred candidates and conditional
hardening live in the [freezer](task_freezer.md), not the execution graph.

## Current state and scheduling

Audit metadata consumers (`sandbox`, `harness`, `model`, `cwd`, summary/activity fields) before
deleting useful UI projections. Squash Alembic only once the app schema settles.

States: **in flight** means reported work is underway; **decision** needs a reviewed outcome;
**blocked** names prerequisites; **candidate** is dispatchable when selected, not a priority claim.
A **capstone** closes an integrated contract, not another implementation. Solid arrows below are
prerequisites; dashed arrows explicitly label scheduling holds or conditional choices. All service
contracts remain multi-replica unless a reviewed temporary restriction says otherwise.

Dispatch assignments (Rai authorized, Codex reported, 2026-10-14):

| Worker            | Assigned sequence; each successor retains its node's gates                                                                                                           |
| ----------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `dispatcher`      | Wake limits, selective revival and failure feedback; state-volume layout; Dispatcher creation/resume and app/wake/UI activation as gates clear.                      |
| `dag_burndown`    | Isolated k3s CI; typed workspace templates and legacy DataVolume retirement; then declarative VM guest storage. Cutoff evidence in #10493.                           |
| `sandbox_service` | Complete app Session defaults and Dispatcher routing; notification activation and direct sender removal; Sandbox Service config cleanup and eligible legacy cleanup. |
| Coordinator       | Replica acceptance, integration review and DAG burn-down.                                                                                                            |

These are assignments, not claims that gated work has started. Independent config changes can
proceed while behavior PRs are under review. Publish drafts as soon as a branch has reviewable work,
with remaining gates in the body; Rai merges each PR by hand.

## 1. Session schema and identity follow-up

```mermaid
flowchart TD
    APP_ALEMBIC_SQUASH[Candidate: baseline final app schema]
```

### `APP_ALEMBIC_SQUASH` — consolidate the final app schema

**Candidate.** Baseline only the settled schema,
verify fresh and migrated databases and their deployed stamps before pruning old revisions.
Retain the data-preserving rollback procedure. No Action Service or other database squash implied.

## 2. Service-owned command admission and later notification presentation

```mermaid
flowchart LR
    NOTIFICATION_COMMAND_OUTCOME_DESIGN[Unscheduled: notice delivery without reading history]
    SESSION_INPUT_METADATA[Typed input annotations and provenance]
    SESSION_INPUT_METADATA_READ[Blocked: metadata reads and message correlation]
    NOTIFICATION_NOTICE_METADATA[Blocked: attach notice metadata to submissions]
    NOTIFICATION_PRESENTATION[Blocked: compact frontend presentation]
    SESSION_INPUT_METADATA --> SESSION_INPUT_METADATA_READ
    SESSION_INPUT_METADATA --> NOTIFICATION_NOTICE_METADATA
    SESSION_INPUT_METADATA_READ --> NOTIFICATION_PRESENTATION
    NOTIFICATION_NOTICE_METADATA --> NOTIFICATION_PRESENTATION
```

Details: [command admission](../runner_gateway/API.md#sessions-and-commands); later
[notification presentation and input metadata](notification_presentation.md).

### `NOTIFICATION_COMMAND_OUTCOME_DESIGN` — notice delivery without reading conversation history

**Unscheduled; operator, 2026-10-11 PDT: not a priority, needs more thought.** The Notification
Service scans a Session's raw history from the History Service for its own command's admission and
`harness_user_message_confirmed`, and stores one `EventEntry` per notice (`Notice.runner_entry`):
the one service besides the History Service fold that derives state from Session events. A command's
`CommandAdmitted` entry says the runner admitted it, but not that the harness executed it or that
the message reached the model's context; today that needs the history. Decide where that
outcome is recorded before the Notification Service stops reading history.

### `SESSION_INPUT_METADATA` — later typed annotations on input commands

**Unscheduled; operator's lean, 2026-10-12 PDT.**
Typed metadata and restricted trusted producer provenance on input commands, so a notice the
Notification Service sends is not rendered as a user message. The lean: the Runner Gateway stamps
verified provenance on the runner Command, and the runner echoes it into the Event that journals
the input, so it reaches history with the input itself; the gateway keeps no command records.
The field rides `SubmitCommand` as it addresses a Session's Sandbox through `SessionDestination`
(#10129) and is delivered through the replica holding the runner stream. The harness never sees it. Notification-specific authorization
belongs here, not in the generic command-admission path; attachments are invalid on controls. No
untyped extension dictionary.

### `SESSION_INPUT_METADATA_READ` — authorized annotation reads

**Blocked on typed input metadata.** Carry the echoed metadata into the Thread fold, joined to
the message by `origin_command_ids`, including pending and failed inputs and replay, without
requiring a live notification inbox.

### `NOTIFICATION_NOTICE_METADATA` — producer integration

**Blocked on typed input metadata.** Attach notice identity/range metadata using existing delivery
command IDs. Preserve receipt reconciliation and explicit inbox acknowledgement. Verify the backend
path without the app; use automated integration coverage rather than requiring a provider outage.

### `NOTIFICATION_PRESENTATION` — compact notification rendering

**Blocked on producer and metadata read integration.** Compact authenticated notification-only
inputs, expand full text and render mixed human/notice messages normally with annotations. Missing
metadata falls back to text. Include visual coverage and one bounded real-notice check; no inbox
acknowledgement on render and no text-prefix provenance heuristic.

### Subscription authorization before broader sources

```mermaid
flowchart LR
    SUBSCRIPTION_AUTHORIZATION_DESIGN[Decision: Action approval vs direct subscription policy]
    SUBSCRIPTION_AUTHORIZATION[Blocked: enforce reviewed creation and continuing source grants]
    SUBSCRIPTION_AUTHORIZATION_DESIGN --> SUBSCRIPTION_AUTHORIZATION
```

### `SUBSCRIPTION_AUTHORIZATION_DESIGN` — auto-allow and operator-approved subscriptions

**Decision.** Compare an Action-backed subscribe operation using existing auto-allow/operator
approval with authorization inside Notification Service; do not preselect another policy engine.
Review which callers may subscribe to which sources, targets, fields/filters, destinations and
lifetimes, and who can approve/delegate that access. Subscription creation approval is distinct
from continuing source access and from ownership of the destination inbox. Define renewal, scope
expansion, policy changes/revocation and what happens to already-retained entries.

Use Kubernetes as a concrete design case: namespace/resource/UID scope, object/status/event/log
content, name reuse and whether grants delegate the caller's existing read authority or explicitly
allow additional observation. A privileged watcher must not expose arbitrary cluster data merely
because the agent owns an inbox. Review argument examples that auto-allow narrow approved scopes,
require operator approval for additional scopes, and reject requests nobody can delegate. See the
[subscription authorization design](notifications.md#subscription-authorization-and-action-approval).
This is independent of selecting a messaging transport or fixing existing GitHub delivery gaps.

### `SUBSCRIPTION_AUTHORIZATION` — creation gate and continuing enforcement

**Blocked on the authorization decision.** If Actions is selected, implement its subscribe operation
and reviewed policy bindings; otherwise implement the chosen direct policy path. Keep Notification
Service the subscription/delivery authority in either case. Carry verified caller/decision scope
across the service boundary rather than substituting the executor's broad identity. Enforce the
same access contract on direct APIs, renewals and updates; no approval bypass by another endpoint.

Test auto-allow, pending approval, denial, restricted destinations, scope escalation and expiry/
revocation with controlled identities. No entry may be delivered beyond the reviewed source scope;
approval is not permission to wake a Sandbox or execute source content. Future Kubernetes sources
must depend on this contract/enforcement when promoted from the freezer. Ordinary authorized
subscription reads/cancellation and existing GitHub gap recovery need not wait for a broad redesign.

### GitHub notice reliability after missed updates

The operator reports that GitHub updates may sometimes be missed. This is a concrete reliability
concern, not proof that GitHub webhook delivery itself is at fault. Diagnose webhook receipt,
matching, inbox persistence and notice dispatch separately. Do not reopen waived broad provider
outage/refresh exercises; test the specific recovery contract with controlled missing webhooks.

```mermaid
flowchart LR
    GITHUB_NOTICE_RELIABILITY_DESIGN[Decision: eventual-state recovery vs agent fallback]
    GITHUB_NOTIFICATION_RECOVERY[Blocked: selected reconciliation or reminder mechanism]
    GITHUB_SHEPHERD_GUIDANCE[Blocked: after CRON_NOTIFICATIONS; fallback-check wording]
    GITHUB_NOTICE_RELIABILITY_DESIGN --> GITHUB_NOTIFICATION_RECOVERY
    GITHUB_NOTICE_RELIABILITY_DESIGN -. chosen guarantee wording .-> GITHUB_SHEPHERD_GUIDANCE
```

### `GITHUB_NOTICE_RELIABILITY_DESIGN` — delivery guarantee and fallback

**Decision.** Review the gap and choose the guarantee needed for PR shepherding: eventual awareness
of current head/check/review/merge state, or historical delivery of each event. Compare bounded
service-side reconciliation using existing durable shared GitHub refresh state with explicit agent
fallback checks, optionally scheduled reminders. Recommend the smallest reliable path with a stated
staleness bound/cost. A webhook subscription is not proof no changes occurred when it stays silent.
Snapshot polling cannot reconstruct all intermediate events, and a cron reminder alone is not
recovery if no active agent checks state. Detailed alternatives and questions are in the
[notification plan](notifications.md#missed-github-updates-and-shepherding-reliability).

### `GITHUB_NOTIFICATION_RECOVERY` — implement the reviewed recovery contract

**Blocked on the reliability decision.** If service reconciliation is selected, periodically compare
eligible shared subjects with authoritative GitHub state and durably emit deduplicated observations
for uncovered relevant changes. Reuse refresh leases, backoff and access checks; bound API load.
Distinguish observed state from a received webhook; do not fabricate delivery IDs or promise replay
of events the API cannot recover. If agent fallback is selected instead, provide its actual bounded
check/reminder mechanism and explicit limitations rather than just advising agents to remember.
Add `CRON_NOTIFICATIONS` as a prerequisite only if that reviewed implementation actually needs it;
a service refresh deadline need not introduce a general scheduler. Backend workers stay in-process.

Test a suppressed webhook, duplicate webhook/reconciliation races, changed head/check state and
restart/checkpoint recovery with a controlled GitHub peer. Observe one eventual update for the
promised scope, honest access/backoff state and no false claim of complete history. No live GitHub
outage or exhaustive historical event matrix is required.

### `GITHUB_SHEPHERD_GUIDANCE` — explain monitoring limits and completion checks

**Sequenced after `CRON_NOTIFICATIONS`; final wording also follows the decision.** Agents are
asked not just to subscribe to GitHub while waiting (e.g. for CI) but to back the subscription with
a fallback check after N minutes, in case the webhook never delivers. Treat notifications as prompts
to inspect authoritative current PR state, not evidence that the latest head passed or that silence
means no progress. Document the chosen fallback deadline/ownership and how an agent notices
unreliable monitoring. State who runs reminders and that notices do not start a stopped harness.
Guidance accompanies the selected reliability behavior; it must not advertise a polling/reminder
guarantee before that mechanism exists.

## 3. Multiagent decisions before implementations

These are operator-reviewable decisions, not permission to implement every possibility. Drafts can
be developed together, but publish a coherent shared identity/authority vocabulary before downstream
APIs. Do not equate creating a resource, reading history, sending a message or receiving/acking it.

```mermaid
flowchart TD
    MULTIAGENT_MODEL[Decision: identities, relationships and native-child boundary]
    THREAD_READ_POLICY_DESIGN[Decision: history read grants]
    SANDBOX_COMPARTMENT_BOUNDARY[Blocked: enforce Sandbox placement boundary]
    THREAD_READ_POLICY[Blocked: scoped archive reads]
    AGENT_MESSAGING_DESIGN[Decision: send/receive RBAC and inbox vs direct delivery]
    AGENT_MESSAGE_INGRESS[Blocked: authorized send API and durable receipts]
    AGENT_MESSAGE_RECEPTION[Blocked: receiving, recovery and acknowledgement]
    AGENT_MESSAGING[Capstone: end-to-end agent messaging]
    THREAD_CREATE_POLICY[Decision: opening a session in an existing Sandbox]
    THREAD_CREATE_AUTHORIZATION[Blocked: implement session-create grants]
    AGENT_LAUNCH_POLICY_DESIGN[Decision: constrained Sandbox launch and delegation]
    AGENT_SANDBOX_LAUNCH[Blocked: enforce agent Sandbox-launch policy]
    MULTIAGENT_MODEL --> THREAD_READ_POLICY_DESIGN
    MULTIAGENT_MODEL --> AGENT_MESSAGING_DESIGN
    MULTIAGENT_MODEL --> THREAD_CREATE_POLICY
    MULTIAGENT_MODEL --> AGENT_LAUNCH_POLICY_DESIGN
    THREAD_READ_POLICY_DESIGN --> SANDBOX_COMPARTMENT_BOUNDARY
    THREAD_READ_POLICY_DESIGN --> THREAD_READ_POLICY
    SANDBOX_COMPARTMENT_BOUNDARY --> THREAD_READ_POLICY
    AGENT_MESSAGING_DESIGN --> AGENT_MESSAGE_INGRESS
    SESSION_INPUT_METADATA_READ[Input provenance reads] -. if direct-input delivery selected .-> AGENT_MESSAGE_RECEPTION
    AGENT_MESSAGE_INGRESS --> AGENT_MESSAGE_RECEPTION
    AGENT_MESSAGE_RECEPTION --> AGENT_MESSAGING
    THREAD_CREATE_POLICY --> THREAD_CREATE_AUTHORIZATION
    AGENT_LAUNCH_POLICY_DESIGN --> AGENT_SANDBOX_LAUNCH
```

### `MULTIAGENT_MODEL` — shared identity and authority vocabulary

**Decision; no runtime work implied.** Present concrete API examples for independent agents,
Sandboxes and Sessions, with creator, manager and parent/provenance represented separately. Ask
the operator to choose addressing/ownership and delegation semantics. No permission inheritance
from an organizational edge or preset. Review how native harness children could later be ingested:
linked execution in the same Sandbox trust boundary versus independently provisioned Sandboxes,
addressability, observability and which controls must remain unavailable. Native integration itself
stays frozen; this decision must allow explicit exclusion rather than require its implementation.

### `THREAD_READ_POLICY_DESIGN` — scoped history read policy

**Decision; draft in parallel with the shared model.** Review compartments versus exact-session
grants, who classifies/reclassifies histories and grants/revokes access, and caller replacement
semantics. Neither grant model isolates Sessions sharing a Sandbox (see the
[isolation boundary](../docs/thread_layering.md#sandbox-isolation-boundary)). Defaults expose
no existing histories to workloads. Select raw read/list/follow scope; read does not imply
send/create, and no folded-read API is required for v1. Decide how revocation
applies to live feeds, discovery and linked evidence. Do not block this on a messaging transport.

### `SANDBOX_COMPARTMENT_BOUNDARY` — enforce Sandbox placement boundary

**Blocked on the reviewed scoped-read policy.**
The Sandbox is the security isolation boundary: co-resident Sessions share filesystem, credentials,
and ServiceAccount authority, regardless of archive-read classifications. Enforce compatible
placement for any proposed scoped-read policy at Open, adoption and replacement, rejecting
placements whose claimed isolation depends on separating co-resident Sessions. Test shared
filesystem/SA cases and denied placements. Keep VM isolation a separate capability, not a
fictional fix for shared credentials inside one VM. See the
[Sandbox boundary](../docs/thread_layering.md#sandbox-isolation-boundary).

### `THREAD_READ_POLICY` — authorized retained-history access

**Blocked on reviewed read policy and compartment enforcement.** Enforce at
the owning backend across list, raw/native evidence, direct reads and feeds, including reconnect
and revocation. Keep app-only UI folds separate. Test allowed/denied histories, two principals,
deleted Sandboxes and revocation with deterministic service tests plus a bounded deployed auth check.

### `AGENT_MESSAGING_DESIGN` — send and receive contract

**Decision.** Present examples of sender authentication, recipient discovery/opt-in, send grants,
recipient read/ack grants and administration. Compare Notification Service inbox delivery with
direct session input and an existing messaging channel; recommend the smallest fit. Define
accepted, delivered, handled and acknowledged separately; decide offline behavior, retention,
batching, abuse limits and mailbox ownership after Sandbox deletion. Reading a transcript does not
authorize sending or acking.

Output includes the selected owner/API and explicit conditional prerequisites: a direct-input
implementation reuses durable `SubmitCommand` and separately reviewed provenance reads; a notification source reuses
inboxes without treating admission as acknowledgement. Neither branch is selected in this DAG.
Before dispatch, add the chosen branch's edges; do not require implementing both. New Sandbox
Service or app tables require their task-specific contract review. Pure Notification Service work need not
wait for unrelated archive schema once its contract is reviewed.

### `AGENT_MESSAGE_INGRESS` — authorized send and acceptance

**Blocked on messaging decision and its selected prerequisites.** Implement bounded payloads,
server-authenticated sender provenance, per-recipient send policy, idempotent message IDs and honest
acceptance receipts in the chosen authority. Test denied pairs, spoofing and retry/conflict behavior.

### `AGENT_MESSAGE_RECEPTION` — delivery, recovery and acknowledgement

**Blocked on ingress contract/implementation.** Implement selected recipient read/delivery/ack
operations, batching and recovery of unread work. Test revoked/deleted recipients, ordinary offline
catch-up and duplicate delivery without leaking payloads. Do not start/resume a harness implicitly
unless `DISPATCHER_WAKE_ACTIVATION` lets that kind of sender wake a Sandbox.

### `AGENT_MESSAGING` — integrated messaging capstone

**Blocked on ingress and reception.** Demonstrate an authorized pair exchanging a message through
the selected API, with correct provenance and distinct receipt/ack states. Security-negative and
multi-replica retry cases belong in automated tests; no real provider outage requirement.

Control design: [ADR](../docs/adr_sandbox_session_control.md).

### `THREAD_CREATE_POLICY` — authorize session creation separately

**Decision.** Review caller scope to open in an existing Sandbox, accepted defaults, quotas,
creator visibility and revocation. Use the service-reserved Session identity, not a caller-invented
public UUID. Opening in an existing Sandbox grants no isolation from its other Sessions; require a
separate Sandbox when incompatible trust or credentials are needed. Decide whether launch-and-open
is a composition of separate grants, never an implicit read/send/launch permission. This contract
reuse the existing Session Dispatcher durable command queue.

### `THREAD_CREATE_AUTHORIZATION` — implement session-create grants

**Blocked on create policy.** Enforce scope/defaults and idempotent Open
lookup at the service boundary; test forbidden targets/overrides and a lost creation reply.

### `AGENT_LAUNCH_POLICY_DESIGN` — Sandbox launch and delegation policy

**Decision.** Review which callers can use which templates and resource budgets, mounts, images,
secrets, egress and Kubernetes/Action grants; presets are defaults, not authority. A new Sandbox is
required for an independent isolation boundary; adding a Session within one is not. Define manager,
lifetime, parent termination, quota/fanout and revocation without automatic privilege inheritance.
Choose policy ownership and audit records before adding tables. State separately whether the
operation also opens a session; if so, depend on `THREAD_CREATE_AUTHORIZATION` for that composition.

### `AGENT_SANDBOX_LAUNCH` — constrained agent-requested launch

**Blocked on launch policy and idempotent create.** Enforce the reviewed
effective spec and delegation server-side. Test allowed and forbidden launch parameters and
concurrent retries. Reuse the app's create path (the shared Sandbox inventory), not harness-native agent tools. Launch alone
does not grant history read, messaging, credentials or execution of another principal's Actions.

## 4. VM environment phases

The [VM Sandboxes design](../docs/vm_sandboxes.md) holds the provider constraints, and the
[KubeVirt plan](kubevirt_environments.md) the live gates no node below names. Split implementation rather than making “VM support” one indivisible task. This is
an unranked candidate lane, not authorization for local Bazel in current containers. The
[runner control stream](../docs/runner_control.md) settles the dial-out contract VM control networking
builds on; changing connection direction does not move command durability or remove the runner
journal.

Stateful VM contract: our own Sandbox resource
(`kind: pod | vm`) carries both kinds, and both keep their
state on one filesystem PVC per Sandbox that Agentplane owns (section 7). Pods mount it as a volume,
KubeVirt guests through virtiofs. Workspace portability is a separate constraint: retained layouts
must be compatible with the next workload. `STATE_VOLUME_BLOCK` may change state attachment later;
a block StorageClass does not itself make Pod and VM layouts interchangeable. See
[storage layouts](../docs/sandbox_storage.md).

```mermaid
flowchart LR
    VM_CONTROL_NETWORKING[Blocked: integrate selected VM control path]
    VM_PROVIDER[Candidate: production provider and API]
    VM_EGRESS[Blocked: live launcher relay checks]
    VM_RAM_SUSPEND[Unscheduled: suspend a VM to disk with its RAM]
    VM_PROCESS_ISOLATION[Blocked: harness/process resource boundary]
    VM_LIFECYCLE[Blocked: integrated lifecycle]
    VM_PROVIDER --> VM_CONTROL_NETWORKING
    VM_CONTROL_NETWORKING --> VM_LIFECYCLE
    VM_PROVIDER --> VM_PROCESS_ISOLATION
    VM_PROVIDER --> VM_LIFECYCLE
    VM_PROVIDER --> VM_EGRESS
    VM_PROVIDER --> VM_RAM_SUSPEND
    STATE_VOLUME_BLOCK[Blocked: the state volume moves to block storage, section 7] -. related .-> VM_PROCESS_ISOLATION
    STATE_VOLUME_BLOCK -. related .-> VM_RAM_SUSPEND
    VM_EGRESS --> VM_LIFECYCLE
    VM_PROCESS_ISOLATION --> VM_LIFECYCLE
    VM_LIFECYCLE --> SANDBOX_VM_ISOLATION[Capstone: selectable VM-backed Sandbox]
    SANDBOX_VM_ISOLATION --> LOCAL_BAZEL[Blocked: bounded local Bazel client in VM]
    SANDBOX_VM_ISOLATION --> KUBEVIRT_EXPERIMENT_RETIRE[Blocked: delete the KubeVirt experiment]
```

### `VM_CONTROL_NETWORKING` — integrate the reviewed connection direction

**Blocked on the VM provider.** Wire
VM control reachability/authentication to the chosen path. An outbound control stream may remove guest
control-port exposure and endpoint discovery; retain the independently needed outbound API/credential
proxy path. State which guest ports, endpoint discovery and network policies the chosen path
replaces. Image packaging and process-isolation work need not wait for this decision. Verify the
selected path on the actual VM network, not just a host-loopback transport test.
The experiment's guest already dials out, but `runtime_acceptance` no longer runs Sessions, setup probes
or recovery on it: those phases called the runner over a port-forward. They return driven through a
Runner Gateway.

### `VM_PROVIDER` — production provider API and lifecycle intent

**Candidate.** The Sandbox Controller makes a `VirtualMachine` from a `SandboxTemplate`'s `vm`
(`agentplane/sandbox_controller/SPEC.md` § VMs), the Runner Gateway accepts its virt-launcher Pod, and
`agentplane-testing` has the `runner-vm` template (`cluster/cdk8s/agentplane/runner_vm.py`). Left:
boot a Sandbox on `runner-vm` and open a Session on it. Re-type what is
worth keeping from draft #8843 (`vm_view` status mapping, DataVolume validation, halted image
replacement), then close it unmerged; it cannot be rebased.

### `VM_EGRESS` — check the launcher relay live

**Blocked on the VM provider.** `agentplane-testing` deploys the launcher relay policy
(`cluster/cdk8s/agentplane/launcher_relay.py`), which copies a runner Pod's egress sidecar, routes and
tokens into the virt-launcher of a VM controlled by one of our Sandboxes. Once the provider creates
such a VM, check on it: no token reaches the guest; the proxy and each platform route answer the
guest at `10.0.2.1`; the relay is ready, which needs the VM's masquerade interface to list ports so
kubelet's probe on 3129 is not forwarded to the guest; the runner fence admits the launcher and
another Sandbox's guest cannot reach it; a forged launcher is refused. Do not repeat the entire
completed prototype matrix or require simultaneous cluster-wide disasters.

### `VM_PROCESS_ISOLATION` — protect control from harness processes

**Blocked on the provider.** Enforce aggregate memory/process/disk budgets and process-control
boundaries. Test representative tool/harness termination and bounded exhaustion to establish which
runner/control state survives and what needs durable recovery. This directly addresses agents
accidentally killing their own harness; guest or container packaging alone is not that guarantee.

Direction (operator, 2026-10-12 PDT): separate the runner and the harness by what each can see,
not by uid, since every file on the virtiofs state PVC is `107:107` anyway (`STATE_VOLUME_BLOCK` would
end that constraint; revisit the uid choice then). In VMs the runner runs
as guest root under systemd and starts the harness with `systemd-run` as uid 107, in a scope with
`InaccessiblePaths=/state/runner`, write access only to `/state/harness` and the workspace,
`ProtectProc=invisible`, `NoNewPrivileges`, no capabilities, `MemoryMax` and `TasksMax`; the
runner's slice reserves memory. Pods keep today's single container; the strong version there
(runner and harness in separate containers of one pod with no shared process namespace, the harness
mounting only its subpath, started through a launcher on a unix socket in an `emptyDir`) waits
until pods hit the problem. Disk exhaustion is not covered and waits on #4717.

### `VM_LIFECYCLE` — integrated lifecycle verification

**Blocked on provider, selected control networking, egress and isolation.** Boot the published
guest (`agentplane-runner-vm-image.yml`) and exercise both harnesses through real service
routes with retained-state stop/start and a representative replacement; confirm identity, replay and
cleanup. Automated tests cover restart/concurrency branches. Additional live faults need a concrete
unresolved platform risk, not a Cartesian product of every component crash and lifecycle operation.

### `SANDBOX_VM_ISOLATION` — VM integration capstone

**Blocked on integrated lifecycle.** Publish the selectable VM environment and measured isolation
limits, with no implied existing-Sandbox conversion or live migration. Component evidence, not
absence of every imaginable failure, establishes the delivered contract.

### `KUBEVIRT_EXPERIMENT_RETIRE` — delete the KubeVirt experiment

**Blocked on the VM capstone.** Once the production provider covers it, move anything still needed
(the acceptance driver, if it becomes the VM lifecycle test) into production code, then delete
`cluster/cdk8s/agentplane/kubevirt_experiment/`, its launcher policy (production's is
`launcher_relay.py`) and the manifests it generates.

### `VM_RAM_SUSPEND` — suspend a VM to disk with its RAM

**Unscheduled; operator, 2026-10-13 PDT: nice to have.** A stopped VM Sandbox resumes with the memory
it had, not a cold boot, so a quick resume loses no in-memory state. Find what KubeVirt offers (a
memory dump or a snapshot with memory) and whether it works with the state volume (virtiofs today, block under `STATE_VOLUME_BLOCK`). The
[VM Sandboxes design](../docs/vm_sandboxes.md#guest-and-storage) records that a VM supports neither
RAM suspension nor live migration today. Check what a resumed guest must redo: the runner's control stream and
projected tokens (the relay reopens them per request, so rotation alone needs no restart), and the
clock. Settle first what "seal before suspend" means when the harness keeps running across the suspend
(`QUIESCE_WITHOUT_POLLING` assumes the Pod sequence). Where storage or the guest cannot carry it, stop/start from the state PVC stays the
lifecycle.

Control design: [ADR](../docs/adr_sandbox_session_control.md).

### `LOCAL_BAZEL` — bounded local client in a VM

**Blocked on VM capstone.** Wire Bazelisk, storage and authenticated RBE/cache/BES; set CPU/memory/disk
budgets and cleanup. Run a representative build/test with the client inside the VM and actions
remote; check cancellation/cleanup while the harness stays responsive. Installed tools and working
hosted builds do not authorize local Bazel in agent containers.

## 5. Independent UI and narrowly scoped service work

These candidates need no multiagent or VM decision. Any implementation that turns out to require
app/Sandbox Service database changes requires its own migration safety review.

```mermaid
flowchart LR
    CLAUDE_CODE_WEB_BRIDGE[Unscheduled: a frontend for Claude Code web sessions]
    ACTION_SURFACES_DESIGN[Decision: where and in what states Actions render]
    ISOLATED_ACCEPTANCE_CI[Unscheduled: acceptance tests in their own CI workflow]
    DOCKABLE_PANES[Unscheduled: dockable panes in the web app]
    ACTION_SURFACES_DESIGN -. related .-> DOCKABLE_PANES
    NOTIFICATION_PRESENTATION[Blocked: compact notification rendering, section 2]
    ACTION_SURFACES_DESIGN -. related .-> NOTIFICATION_PRESENTATION
```

### `ACTION_SURFACES_DESIGN` — where and in what states Actions render

**Decision; operator, 2026-10-13 PDT: rethink it, since it is a patchwork and possibly redundant.**
Decide every place an Action is shown and in which states, then reduce the code to that. Surfaces
found in `app/frontend/actions/` and the service worker: the sidebar rows (collapsed and expanded,
with Approve/Deny only where `canApproveInline` allows), the top "Actions waiting" strip (a count),
the Actions page queue and the detail view (`PendingActionCard`, the full `ActionCall`, with a raw
and a pretty toggle), history, the policy view, and the Web Push notification (formatters in a
registry separate from the React one). Several of them choose independently what to render, whether
to offer a decision, and how tall to let the card grow. Settle which surfaces exist, what each is
for (a glance, a quick decision, full review, a record), and which states each has, so that one
rule decides inline approval and height and each Action type is described once.
Direction (operator, 2026-10-13 PDT): whether a card offers its decision buttons is not a per-kind
allowlist (`QUICK_APPROVALS`); the Action type's own TypeScript implementation decides for each
invocation from its content (for example a read-only call against a listed resource). Also give
each representation and visual element one fixed name and use it everywhere (code, UI copy, docs),
since "quick approve" could mean the buttons on a card or Approve/Deny on a desktop notification.
Proposed vocabulary, to settle with the design: **compact card** (the glance form in the sidebar
and the strip), **full card** (the detail view with the whole `ActionCall`), **card decision
buttons** (Approve/Deny on a compact card), **notification buttons** (Approve/Deny on a Web Push
notification), **attention strip** (the top "Actions waiting" count). `NOTIFICATION_PRESENTATION` (section 2) renders notices on the same surfaces and uses the same card
names. The compact-card
fixes under way (decision buttons on collapsed cards, a height cap, more Actions allowed inline)
land first and need not wait for this.

### `DOCKABLE_PANES` — dockable panes in the web app

**Unscheduled; operator, 2026-10-13 PDT.** Let the operator arrange the app's panes (thread browser,
Actions queue, several threads at once, Action detail) by dragging them into a layout of their own,
with split resizing, a layout that persists, and one selected pane at a time on a phone. Open spike
[#9721](https://github.com/agentydragon/ducktape/pull/9721) builds this as an experimental
`/#/mosaic` shell on `react-mosaic-component`, split-only without tabs, beside the classic routes;
it is stale and is the starting point. Settle whether docking replaces the classic routes or stays
an alternative, and fold the Action tiles into the surfaces `ACTION_SURFACES_DESIGN` settles.

### `CLAUDE_CODE_WEB_BRIDGE` — Claude Code web sessions in Agentplane

**Unscheduled; operator, 2026-10-12 PDT: low priority, not to build now.** Make Agentplane's
frontend a custom frontend for Claude Code web sessions: show their conversations and send them
messages, by shimming the web interface's control protocol. Builds on the viewer and syncer under
`devinfra/` and the reverse-engineering in `agentydragon/gaffer-private`.

### `ISOLATED_ACCEPTANCE_CI` — acceptance tests in their own CI workflow

**In flight (Codex, 2026-10-14), current-stack refresh #10467 and measured OCI import work.**
Pod/VM storage acceptance remains the first priority. After measuring setup and improving the
bottleneck, enable the model-free Pod/Dispatcher suite in CI. Real KubeVirt, Cilium and OIDC coverage
remain explicit gaps. Today the deployed vertical tests under
`agentplane/acceptance/` run by hand against the stateful `agentplane-testing` environment, and CI
never runs them. Move them into their own CI workflow that runs in isolation, so the cluster no
longer carries a stateful environment for them. Options named: an isolated part of the operator's
cluster, GitHub's runners, or BuildBuddy's; and the cheap-experiments LiteLLM key or a new key
provisioned for CI. Constraint: an agent can still run, inspect and step through the tests itself.
The current candidate is k3s on RBE; measure setup/image import and lifecycle costs, then improve
the measured bottleneck without weakening assertions or increasing timeouts. The k3s-on-RBE spike (`agentplane/x/k3s_rbe_spike`) shows a real kubelet runs in a
BuildBuddy action, but with no ClusterIP Services, no KubeVirt and no Cilium, so tests that need
those stay elsewhere. envtest covers only API-server behavior, not Pods, harnesses or LLM calls, so
it does not replace these tests.

### `ISOLATED_TEST_SPIKES_RETIRE` — remove superseded isolated-test scaffolding

**Blocked on `ISOLATED_ACCEPTANCE_CI` providing the maintained fixtures and entry points.** Move the
retained k3s fixture/tests into their owning test package, then delete superseded `x/k3s_rbe_spike`
and `x/k3s_env` entry points, image-loader paths, BUILD targets and old recipes. Keep any still-needed
platform-specific fixture until its replacement covers the same contract. End with one documented
local/CI invocation path per supported test tier.

### `TESTING_ENVIRONMENT_RETIRE` — retire the stateful testing deployment once coverage replaces it

**Blocked on `ISOLATED_ACCEPTANCE_CI` and an explicit gap review.** Pod lifecycle/Dispatcher tests on
bare k3s do not cover KubeVirt, Cilium, external identity or real service routing. Assign those checks
to an isolated tier or retain their bounded live environment before removing `agentplane-testing`.
Prepare a separate operator-reviewed retirement/data-disposition PR: remove obsolete Flux/deployments,
databases, credentials, RBAC, DNS/network rules and stateful acceptance recipes only after the new
paths are usable by agents and CI. Preserve staging and user-authored data; this node does not authorize
immediate deletion of the testing namespace or its volumes.

```mermaid
flowchart LR
    ISOLATED_ACCEPTANCE_CI --> ISOLATED_TEST_SPIKES_RETIRE
    ISOLATED_ACCEPTANCE_CI --> TESTING_ENVIRONMENT_RETIRE
```

## 6. Existing access and lifecycle follow-ups

These are bounded existing work, not dependencies on the broader multiagent model. Confirm current
source/deployment state with the relevant owner when dispatching; old acceptance notes are not live
observations. New database work follows the task-specific review and rollout requirements.

```mermaid
flowchart LR
    PC_EGRESS_CREDENTIALS[Candidate: caller admission configuration] --> PC_EGRESS[Blocked: controlled egress cutover]
    CLAUDE_AI_SA[Decision: review caller authority] --> MANAGED_SA_RBAC[Blocked: account-owned grants]
    AGENT_PERMISSION_REQUESTS[Blocked: an Action type and approval view for permission requests]
    ACTION_SURFACES_DESIGN[Decision: where and in what states Actions render, section 5]
    ACTION_SURFACES_DESIGN --> AGENT_PERMISSION_REQUESTS
    BOOTSTRAP_UNKNOWN_HOLDS_READY[Blocked: remove old-runner readiness tolerance]
    SANDBOX_RBAC[Candidate: bounded deployed grant checks]
    NOTIFICATION_INGRESS_BOUNDARY[Candidate: public-route security check]
    NETWORK_WORKLOAD_IDENTITY[Unscheduled: network-level workload identity]
    PC_KUBERNETES_POLICY_SET[Unscheduled: public-coder's Kubernetes access as one PolicySet]
```

### `PC_EGRESS_CREDENTIALS` — label public-coder's Action Service caller

**Candidate; verify remaining configuration.** Admit the intended OpenClaw account to the dedicated
Action Service using its configured namespace and policy bindings. A label alone is not an execution
grant. Coordinate with the owner of the currently disabled/controlled workload before rollout.

### `PC_EGRESS` — public-coder-agent egress migration

**Blocked on caller admission and a controlled deployment.** Compare effective allowed/denied routes,
credential substitution and Action policy against the existing proxy, then do a reversible cutover.
Remove only unused OpenClaw-specific wiring; retain shared Iron/Haku consumers. Do not turn this
into an outage matrix. Source: [public-coder wiring](../../cluster/cdk8s/public_coder/proxy.py).

### `PC_KUBERNETES_POLICY_SET` — public-coder's Kubernetes access as one PolicySet

**Unscheduled; operator, 2026-10-12 PDT: wants it eventually.** Move public-coder-agent's whole set
of RoleBindings and ClusterRoleBindings (logs, pods, node and cluster-metadata reads, VolSync status
and the rest, in `cluster/cdk8s/public_coder/app.py` and its entries in
`cluster/cdk8s/agent_access_profiles.py`) under one `PolicySet` of Kubernetes rules with a Flux-owned
`PolicyBinding`. Open: `PolicyBinding` subjects are ServiceAccounts only, while some of these
grants name the `haku:access-profile:public-coder` Group, so either the binding learns Group
subjects or those grants move to the agent's ServiceAccount.

### `CLAUDE_AI_SA` — review the external caller's actual authority

**Decision.** Review the claude.ai account's Connection and arbitrary-shell Sandbox privileges,
Kubernetes scope and egress/Action grants. Record intended authority in generated configuration,
not ad hoc grants. Test an allowed and denied operation; do not add privileges as part of this review.

### `AGENT_PERMISSION_REQUESTS` — an Action type and approval view for permission requests

**Blocked on `ACTION_SURFACES_DESIGN`; operator, 2026-10-13 PDT:
eventually.** A specific Action type for the request instead of the Kubernetes MCP escape hatch, and
an approval view that shows the operator the account, the sets and the duration rather than a blob of
Kubernetes YAML. It shares with the app's sandbox-page grants the one path that writes a `PolicyBinding` for
the operator (`EgressAccess.grant`), so an approved request creates the same kind of binding. How
the request renders is the Action surfaces' decision: the approval view is one Action type described
once there.

### `MANAGED_SA_RBAC` — grants for accounts without a Sandbox

**Blocked on the caller review.** The claude.ai caller gets a
Flux-owned `PolicyBinding` of the sets `CLAUDE_AI_SA` approves, of any rule type, replacing its
static per-namespace grants in `agent_access_profiles.py`. Static Group subjects (Haku OIDC;
public-coder until `PC_KUBERNETES_POLICY_SET`) stay Flux bindings. Test an allowed and a denied request. Static caller accounts' bindings, this one among them, are Flux-owned, never Sandbox-owned.

### `SANDBOX_RBAC` — bounded deployed grant checks

**Candidate; security-relevant.** Inspect the managed-Sandbox grant path as `PolicySet`s leave it
and verify intended versus unrelated scope, including Role edits and credential-use boundaries,
without recording credentials. Use [agent RBAC](../../cluster/docs/agent_rbac.md) and existing tests;
only unresolved deployed wiring needs a live check, not a repeat of every already-covered operation.

### `NOTIFICATION_INGRESS_BOUNDARY` — bounded public-route security check

**Candidate; security-relevant deployment check, not a provider failure drill.** Existing tests
cover invalid signatures. Close the remaining route-wiring uncertainty with an unsigned webhook
request and a private-API request through the public hostname: both must be refused, without
creating subscriptions or inbox entries. Record the result once; no exhaustive provider matrix.

### `NETWORK_WORKLOAD_IDENTITY` — network-level workload identity

**Unscheduled; operator, 2026-10-12 PDT: consider later.** Sandboxes and other workloads prove
their identity to Agentplane services at the network layer, with Cilium mutual authentication
(SPIFFE/SPIRE) or a service mesh, instead of a projected token plus an egress proxy hop; services
authorize by SPIFFE ID or workload label. Costs: SPIRE or a mesh to install, Cilium mutual
authentication still in beta, and a second authentication scheme in every service beside projected
tokens.

### `BOOTSTRAP_UNKNOWN_HOLDS_READY` — an unreported bootstrap holds `Ready`

**Blocked on every live Sandbox running a runner that reports the bootstrap fact.** The controller lets an `Unknown` `Bootstrapped` condition (reason
`BootstrapNotReported`: the runner reported no bootstrap fact) leave `Ready` alone, because runners older
than the Lease facts change never report it. Once none runs, `Unknown` holds `Ready` too (`Ready` False,
reason `BootstrapNotReported`) and the gating comment in `agentplane/sandbox_controller/controller.go` goes.
Gate: every live Sandbox's Lease carries the bootstrap annotation, or those Sandboxes were restarted on a
current image. A `CLEANUP` tombstone naming this gate sits next to the code.

Control design: [ADR](../docs/adr_sandbox_session_control.md).

## 7. Our own Sandbox resource: Sessions that outlive their Pod

A Session and its history are durable. The operator's direction (2026-10-12 PDT):
the way a Session outlives its runner is our own Sandbox resource, which holds the state PVC and
stays in place with no Pod or VM scheduled. A Sandbox is its state, running or not; an
**incarnation** is one Pod or VM. Stopping, replacing the image and starting again keeps the
Sandbox, so keeping the state means keeping the Sandbox, and deleting the Sandbox is the only thing
that deletes it. The same resource carries the VM kind, so section 4's stateful VMs build on these
nodes instead of a parallel track. Today a Session lives as long as its `Sandbox`
(suspension removes the Pod and keeps the state PVC, which goes with the `Sandbox`). The History Service keys by Session, so it can record each incarnation, and a seal closes
an incarnation, not the Session.

A related direction (operator, 2026-10-11 PDT; later, not scheduled): Sessions living in a
long-running, often-suspended Sandbox whose runner image upgrades between incarnations.

```mermaid
flowchart TD
    STATE_VOLUME_BLOCK[Blocked: the state volume moves to block storage]
    NODE_LOCAL_BLOCK_STORAGE[Unscheduled: node-local block volumes on every node]
    WORKSPACE_VOLUME[In flight: activate typed Sandbox-owned workspaces]
    WORKSPACE_DATAVOLUME_RETIRE[Blocked: retire the old DataVolume workspace path]
    STATE_VOLUME_LAYOUT[Candidate: declare the state volume layout explicitly]
    VM_GUEST_DECLARATIVE_STORAGE[Candidate: declarative guest disk initialization]
    VOLUME_LAYOUT_CONVERSION[Unscheduled: explicit offline volume conversion, much later]
    SANDBOX_SPEC_EDIT[Unscheduled: edit a suspended Sandbox's template or kind]
    WORKSPACE_NODE_PORTABILITY[Unscheduled: move a workspace between nodes]
    SANDBOX_ARCHIVE[Unscheduled: a suspended Sandbox archived to object storage]
    SESSION_REVIVE[Blocked: start a stopped Sandbox again]
    AGENT_RESUME_NOTICE[Unscheduled: tell agents when their Sandbox was suspended and resumed]
    SESSION_REVIVE -. related .-> AGENT_RESUME_NOTICE
    SANDBOX_IDLE_EXPIRY -. related .-> AGENT_RESUME_NOTICE
    RUNNER_BUSY_TRACKING -. related .-> AGENT_RESUME_NOTICE
    INCARNATION_UID_REQUIRED[Blocked: the runner rejects a Welcome without the incarnation UID]
    RUNNER_BUSY_TRACKING[Unscheduled: the runner counts terminals and background processes as busy]
    QUIESCE_LEGACY_SHAPE_REMOVAL[Blocked: the controller stops reading the blocking Quiesce shape]
    QUIESCE_WITHOUT_POLLING[Blocked: the controller is told when a seal finishes]
    SESSION_ACTIVITY_STREAM[Blocked: the notification service delivers on a Session's idle time]
    SANDBOX_SERVICE_REMOVE[Blocked: remove the Sandbox Service]
    PROXY_START_METHODS_DISPATCHER_ONLY[Blocked: only the Dispatcher may start or continue work through the proxy]
    NOTIFICATIONS_VIA_DISPATCHER[Blocked: notifications send through the Session Dispatcher]
    NOTIFICATION_GATEWAY_SEND_REMOVE[Blocked: remove the Notification Service's pre-activation gateway command path]
    RESUME_CREDENTIALS_VALID[Blocked: grants and tokens still valid after a long suspension]
    SUSPENDED_COMPOSER[Blocked: the composer stays enabled while a Session is suspended]
    THREAD_SUCCESSOR_DELIVERY[Blocked: commands unsettled across incarnations]
    HOSTED_THREAD_SURFACES[Blocked: show and revive stopped Sessions]
    VM_PROVIDER[VM provider, section 4]
    NODE_LOCAL_BLOCK_STORAGE --> STATE_VOLUME_BLOCK
    WORKSPACE_VOLUME -- old consumers gone and images pinned --> WORKSPACE_DATAVOLUME_RETIRE
    STATE_VOLUME_LAYOUT --> STATE_VOLUME_BLOCK
    VOLUME_LAYOUT_CONVERSION -. future incompatible edits only .-> SANDBOX_SPEC_EDIT
    WORKSPACE_VOLUME --> WORKSPACE_NODE_PORTABILITY
    WORKSPACE_VOLUME --> SANDBOX_ARCHIVE
    WORKSPACE_NODE_PORTABILITY -. related .-> SANDBOX_ARCHIVE
    SANDBOX_SPEC_EDIT -. related .-> WORKSPACE_VOLUME
    VM_PROVIDER -. related .-> SANDBOX_SPEC_EDIT
    APP_SESSION_DEFAULTS[Candidate: the app sends complete Session specs, section 12] -. keeps the preset label true .-> SANDBOX_SPEC_EDIT
    STATE_VOLUME_BLOCK -. related .-> WORKSPACE_VOLUME
    SANDBOX_IDLE_EXPIRY -. resumed by .-> SESSION_REVIVE
    HOSTED_THREAD_SURFACES --> SUSPENDED_COMPOSER
    QUIESCE_WITHOUT_POLLING --> SANDBOX_IDLE_EXPIRY
    GATEWAY_WRAPPER_RETIRE[Blocked: callers use the proxy; RunnerGateway goes, section 12] --> PROXY_START_METHODS_DISPATCHER_ONLY
    NOTIFICATIONS_VIA_DISPATCHER --> SESSION_ACTIVITY_STREAM
    DISPATCHER_WAKE_ACTIVATION --> SUSPENDED_COMPOSER
    APP_DISPATCHER_ACTIVATION --> SUSPENDED_COMPOSER
    SESSION_REVIVE --> RESUME_CREDENTIALS_VALID
    QUIESCE_WITHOUT_POLLING -. seal before a RAM suspend is its own question .-> VM_RAM_SUSPEND[Unscheduled: suspend a VM to disk with its RAM, section 4]
    SESSION_REVIVE --> THREAD_SUCCESSOR_DELIVERY
    SESSION_REVIVE --> HOSTED_THREAD_SURFACES
    EFFECTIVE_MODE_FALLBACK_DROP[Blocked: remove spec.running fallback after controller rollout]
    DISPATCHER_REPLICA_ACTIVATION[Candidate: remove singleton deployment restriction]
    DISPATCHER_HARNESS_CONFIRMATION[Decision: settle harness-confirmed inputs]
    DISPATCHER_SEND_AUTHORIZATION[Decision: scoped sender admission]
    APP_SESSION_DEFAULTS --> APP_SESSION_OPEN_VIA_DISPATCHER
    APP_DISPATCHER_ACTIVATION --> APP_GATEWAY_SEND_REMOVE
    APP_SESSION_OPEN_VIA_DISPATCHER --> APP_GATEWAY_SEND_REMOVE
    NOTIFICATIONS_VIA_DISPATCHER --> NOTIFICATION_GATEWAY_SEND_REMOVE
    APP_GATEWAY_SEND_REMOVE --> PROXY_START_METHODS_DISPATCHER_ONLY
    NOTIFICATION_GATEWAY_SEND_REMOVE --> PROXY_START_METHODS_DISPATCHER_ONLY
    SANDBOX_SERVICE_CONFIG_REMOVE --> SANDBOX_SERVICE_REMOVE
    STATE_VOLUME_LAYOUT --> STATE_VOLUME_LAYOUT_FALLBACK_REMOVE
    STATE_VOLUME_BLOCK --> STATE_VOLUME_VIRTIOFS_RETIRE
    RUNNER_HEARTBEAT_SPLIT[Candidate: runner-only heartbeat type]
    DISPATCHER_WAKE_ABUSE_LIMITS --> DISPATCHER_WAKE_ACTIVATION
    SELECTIVE_SESSION_REVIVAL --> DISPATCHER_WAKE_ACTIVATION
    DISPATCHER_WAKE_FAILURE_FEEDBACK --> DISPATCHER_WAKE_ACTIVATION
    INCARNATION_UID_REQUIRED --> JOURNAL_LEGACY_SEAL_SCHEMA_REMOVE
    K3S_LIFECYCLE_SUITE[Candidate: k3s lifecycle integration suite]
    QUIESCE_WITHOUT_POLLING -. quiesce assertions .-> K3S_LIFECYCLE_SUITE
```

### `K3S_LIFECYCLE_SUITE` — a k3s lifecycle integration suite

**Candidate; operator, 2026-10-13 PDT: smaller k3s-based tests of the Sandbox lifecycle.** One
shared k3s environment fixture on a BuildBuddy action, one test per
flow, no LLM: create, bootstrap, suspend, resume, quiesce and delete, about 15 minutes on RBE. Create,
bootstrap, suspend, resume and delete have landed (`agentplane/x/k3s_env`). Left: the quiesce
assertions, which wait for `QUIESCE_WITHOUT_POLLING`, and the assertion that `Ready` implies a
finished bootstrap. The controller contract is implemented; this node adds integration assertions. The disposable-environment lane owns the
fixture. Tests that need ClusterIP Services, KubeVirt or Cilium stay elsewhere
(`ISOLATED_ACCEPTANCE_CI`).

### `STATE_VOLUME_BLOCK` — the state volume moves to block storage

**Blocked on `NODE_LOCAL_BLOCK_STORAGE` and `STATE_VOLUME_LAYOUT`.** Replace the VM's virtiofs
state attachment with a declared disk layout once suitable node-local storage exists. The state
PVC holds journals and native harness persistence under `/state/sessions`, separately from the
workspace. A disk would remove virtiofs's `107:107` ownership constraint; check all callers before
changing the guest and Pod UIDs. A Pod filesystem mount and a VM disk are not interchangeable just
because the provisioner supports block storage. Specify supported attachments and reject
incompatible kind changes; conversion is deferred, not a prerequisite for fresh provisioning.
Use an explicit incompatible cutoff for existing Sandboxes, in a separate data-drop PR; the
operator accepted dropping them rather than requiring migration. Gate guest behavior on the VM image and its embedded runner wheel, then activate templates.

### `NODE_LOCAL_BLOCK_STORAGE` — node-local block volumes on every node

**Unscheduled; operator, 2026-10-13 PDT.** Extend capacity-enforcing node-local provisioning to
all cluster nodes, tracked by [#4717](https://github.com/agentydragon/ducktape/issues/4717).
`cluster/cdk8s/openebs_lvm/storage.py` already declares Proxmox filesystem and raw-block classes;
that is not coverage of every node. `local-path` does not enforce PVC capacity. Verify capacity,
node affinity and both required volume modes. A node-local volume remains pinned to its node;
moving it is `WORKSPACE_NODE_PORTABILITY`. This provisioner work does not gate Sandbox ownership
or the explicit layout contract for workspace PVCs.

### `WORKSPACE_VOLUME` — roll out explicitly typed Sandbox-owned workspaces

**In flight (Codex, 2026-10-14); template activation is in draft #10507, based on devel after merged #10493.**
The operator-directed existing Sandbox cutoff is complete; [cutoff evidence](workspace_cutoff.md)
records the empty inventory verified on 2026-10-14. The implementation merged in #10440 adds
explicit workspace layouts, retained PVC identity and attachment compatibility checks;
[storage layouts](../docs/sandbox_storage.md) owns the contract. This PR activates the selected
layouts in the Pod and VM templates, not another implementation of ownership.

Fresh testing Pod DirectoryTree and VM RawDiskImage fixtures passed retained file/PVC checks,
including a changed-claim refusal and halted VM-object replacement. The old DataVolume kept files
through halt/start but lost its workspace on VM-object replacement. The selected paths and exact
evidence are in [storage acceptance](../acceptance/debug/storage_acceptance.md); no staging run
has been run. The existing-data cutoff is recorded separately below.

The accepted controller pin `devel-20261014082718-1ceacb7` reads `workspaceVolume`; the selected
VM image `devel-20261014055155-9642b3d` and runner wheel `a13d9a7d58ff` accept the explicit
RawDiskImage/ext4 guest path. The Pod uses DirectoryTree at `/state/workspaces`; the VM uses the
Sandbox-owned RawDiskImage/ext4 claim at that same guest path. The test and staging image pin files
already carry these controller and guest references. The operator-directed cutoff is recorded in
[#10493](https://github.com/agentydragon/ducktape/pull/10493). Refresh both namespace inventories
immediately before the template change is applied; do not create, delete, or replace Sandboxes as
part of this PR. Do not require migration, a converter or the future block provisioner. The losing
workspace path is removed by `WORKSPACE_DATAVOLUME_RETIRE` after its callers are gone.

### `WORKSPACE_DATAVOLUME_RETIRE` — retire the old DataVolume workspace path

**Blocked on `WORKSPACE_VOLUME` and old template/workload consumers being gone.** Remove the
superseded blank-DataVolume workspace template, controller support used only by that path and its
obsolete tests/docs. Search all callers before removing generic DataVolume support. Gate removals
on pinned caller images where applicable. Do not delete retained data as an incidental cleanup;
completed existing-data disposition is recorded in [cutoff evidence](workspace_cutoff.md). Testing selected the retained
Sandbox-owned RawDiskImage path; its evidence is linked above.

### `STATE_VOLUME_LAYOUT` — declare the state volume layout explicitly

**Candidate; assigned to `dispatcher` alongside the wake-feedback lane.**
Extend the workspace layout vocabulary and durable identity contract to
`stateVolume`, whose existing contract is fixed `DirectoryTree`. Keep this separate from choosing
block storage. Reader code must land and be pinned before manifests declare new fields; old
manifests must remain accepted during that rolling deployment. `STATE_VOLUME_LAYOUT_FALLBACK_REMOVE` removes any temporary optional-field fallback after all
declarations and images have moved. Do not infer an existing disk's format from a mutable template. Test mismatches before
workload replacement. If existing declarations cannot be retained, use the approved incompatible
cutoff in a separate data-drop PR rather than adding old-data compatibility fields.

### `STATE_VOLUME_LAYOUT_FALLBACK_REMOVE` — require explicit state layout declarations

**Blocked on `STATE_VOLUME_LAYOUT` and all declaring templates and accepting images rolled out.**
Remove temporary omitted-layout defaults and their tests if the code-first rollout introduces them.
Validate retained identity/layout before attachment. No old-data tolerant adoption or conversion;
any incompatible data disposition is a separate operator-reviewed cutoff.

### `STATE_VOLUME_VIRTIOFS_RETIRE` — delete the superseded state attachment

**Blocked on `STATE_VOLUME_BLOCK` acceptance/activation and old workloads gone.** Remove VM state
virtiofs mounts, guest ownership/UID workarounds used only by them, old template paths and tests/docs.
Keep unrelated virtiofs uses where live callers still need them. Verify Pod/VM state persistence on
the selected explicit layouts before retiring the path; data drops stay in a separate cutoff PR.

### `VM_GUEST_DECLARATIVE_STORAGE` — declarative guest disk initialization

**Candidate; assigned to `dag_burndown` after `WORKSPACE_VOLUME`; operator: the VM is running too much bash.**
Inventory guest shell scripts and the
NixOS module replacing each, then use declarative filesystem/disk initialization where supported.
Preserve the explicit layout contract and refuse incompatible existing data. `autoFormat`'s
recognizable-filesystem check is not equivalent to the existing zero-content scan; specify and
test first-use formatting and remount behavior before removing the old scripts. This can proceed
independently of workspace ownership. New guest configuration waits on the accepting VM image
and, for runner changes, its embedded wheel pin. Finish by deleting the replaced shell scripts,
units and packaging references; do not leave a second formatting path.

### `VOLUME_LAYOUT_CONVERSION` — explicit offline volume conversion

**Unscheduled, much later; operator, 2026-10-13 PDT.** Conversion is an accepted future option,
not work for #10440 or a gate on its rollout. Until implemented, incompatible layout changes are
rejected. When scheduled, design a stopped-Sandbox operation with an explicit destination layout,
verified data/metadata preservation, crash-safe binding switch and separately authorized source
cleanup. See [storage layouts](../docs/sandbox_storage.md#formatting-and-conversion). Do not build
a generic conversion framework now.

### `SANDBOX_SPEC_EDIT` — edit a suspended Sandbox's template or kind

**Unscheduled; operator, 2026-10-13 PDT.** Let the operator change a Sandbox's template, including a
switch between VM and Pod, from the app, only while the Sandbox is suspended. Each incarnation
already reads the template as it is then (`SANDBOX_IMAGE_REPLACE`), and no spec is replaced
automatically; this is the explicit, operator-driven edit. Needs `WORKSPACE_VOLUME`,
so a compatible kind switch keeps the workspace, and `VM_PROVIDER` for the VM side. Validate the
retained state and workspace layouts before replacing a workload. Reject incompatible edits;
`VOLUME_LAYOUT_CONVERSION` is a separate, much later task and does not block compatible edits. The
preset the app records on a Sandbox as a label (`APP_SESSION_DEFAULTS`) must stay true after the edit.

Control design: [ADR](../docs/adr_sandbox_session_control.md).

### `WORKSPACE_NODE_PORTABILITY` — move a workspace between nodes

**Unscheduled; operator, 2026-10-13 PDT: even later.** A reasonably fast, node-portable workspace, or
node portability by copying: volsync from node to node, through S3 on SeaweedFS or directly, while the
Sandbox is suspended. Node-local volumes stay the default; this only moves them.

### `SANDBOX_ARCHIVE` — a suspended Sandbox archived to object storage

**Unscheduled; operator, 2026-10-13 PDT: a thought that volsync suggests, not a plan.** Resource
affordances for a Sandbox that still exists but whose volumes sit in restic or S3, so it must be
rehydrated before it can start, freeing the node's disk meanwhile. Sandbox status would say that it
is archived and where, and resuming one rehydrates first. Related to `WORKSPACE_NODE_PORTABILITY`,
which shares the copy mechanism. Needs `WORKSPACE_VOLUME`; the runner's state PVC would archive the
same way.

Control design: [ADR](../docs/adr_sandbox_session_control.md).

```mermaid
flowchart TD
    VM_PROVIDER[VM provider, section 4] --> SANDBOX_IMAGE_REPLACE[Blocked: replace a VM's runner image between incarnations]
    SANDBOX_GENERIC_SPLIT[Unscheduled: split the generic Sandbox from the runner's sealing]
    SANDBOX_RUNNER_LAYERING[Unscheduled: add the runner and harnesses to a platform user's image]
    SANDBOX_GENERIC_SPLIT -. related .-> SANDBOX_RUNNER_LAYERING
```

### `SANDBOX_IMAGE_REPLACE` — replace a VM's runner image between incarnations

**Blocked on `VM_PROVIDER`; the operator's "pause, replace the image, resume" (2026-10-12 PDT).**
Pods already behave so: each incarnation reads its `SandboxTemplate` as it starts, and a running Pod
never changes (`agentplane/sandbox_controller/SPEC.md` § Incarnations), so a new image in the
template, or a Sandbox switched to another template, applies at the next resume on the same state
PVC. Left is the same for VMs once our Sandbox has them: the controller rewrites a VM's containerDisk
only while `runStrategy: Halted`, keeping its state PVC. The state PVC's contents belong to the
runner (operator, 2026-10-12 08:41 PDT): it versions and migrates them, or reports that it cannot,
and the controller has no version gate. So of #8843's `ReplaceVmImage` (replaced live in
the 2026-10-03 run) the Halted-only rewrite carries over,
and the exact `VmTemplate.state_schema_version` match does not.

Control design: [ADR](../docs/adr_sandbox_session_control.md).

### `SANDBOX_GENERIC_SPLIT` — separate the generic Sandbox from the runner's sealing

**Unscheduled; operator, 2026-10-13 PDT: a later TODO.** Our `Sandbox` resource binds two concerns
together. One is what Agent Sandbox did, plus VM support: a template, a Pod or VM per incarnation,
and a state volume that stays. The other is specific to our runner running inside: sealing a
Session's journal before the incarnation goes. Separated,
the first could be reused without the runner. Decide where the line falls (resource, controller or package) and what each side owns. A
layered pair, a Sandbox and a runner that runs inside one, is the direction the control design
(`docs/adr_sandbox_session_control.md`) points to: seal, runner connectivity and idle suspension
are the runner-aware layer, and the Session Dispatcher sits outside both.

Control design: [ADR](../docs/adr_sandbox_session_control.md).

### `SANDBOX_RUNNER_LAYERING` — add the runner and harnesses to a platform user's image

**Unscheduled; operator, 2026-10-13 PDT: very low priority unless easy.** Separate the runner's
concerns (its version and binary, and the harness binaries) from the tools in the image, so a
platform user supplies a Pod image or VM image or definition with no runner or harnesses and the
runner side layers them in. Then they need not bump build versions or layer the runner themselves.
Related to `SANDBOX_GENERIC_SPLIT`: that node separates the resource, this one separates the image.
Options the operator named, none chosen: mount a separate volume that carries the runner, Claude and
Codex into a Pod image that has none of them; or take a user root filesystem and run it under
Firecracker with the runner as init.

### `EFFECTIVE_MODE_FALLBACK_DROP` — the Python readers stop falling back to `spec.running`

**Blocked on the operating-mode controller image being pinned in both environments and existing CRs having status populated.** `Sandbox.operating_mode` in
`sandbox_resource.py` reads `status.operatingMode` and falls back to `spec.running` while the controller
has not published it. Once the controller that publishes it is pinned everywhere, make the status field
required and delete the fallback (its `CLEANUP` marker names the same gate).

### `SANDBOX_IDLE_EXPIRY` — the controller suspends an idle Sandbox

**Blocked on the app image that reads `status.operatingMode` being pinned, `QUIESCE_WITHOUT_POLLING`; operator, 2026-10-11 and 2026-10-13 PDT.** An idle runner
Sandbox suspends: it is sealed, then the Pod or VM goes, freeing memory and compute, and the Sandbox, its
ServiceAccount, bindings and state PVC stay, so `SESSION_REVIVE` resumes it on the same PVC. Nothing deletes a
Sandbox on idleness or any timer (operator, 2026-10-13 PDT): only the operator deletes one, through
[teardown](../sandbox_service/API.md#teardown) and the seal. What a resume gets back of the
workspace depends on `WORKSPACE_VOLUME`. Idle is a Sandbox-level fact: every Session in the Sandbox
is idle. The control design (`docs/adr_sandbox_session_control.md`) puts the policy in the Sandbox
controller: `spec.idleSuspendAfter`, a timer the controller measures from the `idleSince`
fact the runner publishes (the connection Lease facts), so it needs no History Service and
survives a controller restart. Busy covers a delivery under way, the bootstrap, a turn, a command and a setup script. A background or
durable process or an attached terminal does not count (operator, 2026-10-14), so a suspend ends them (`RUNNER_BUSY_TRACKING`). The controller's idle timer does not edit the spec: it
writes `status.suspendedBy: Idle` with the `spec.wakeGeneration` it suspended at. Automatic suspension is
`Quiesce` with `if_idle_since`: the runner refuses it on a token mismatch or when busy (`ABORTED`, as
for a Sandbox already in lameduck; `FAILED_PRECONDITION` is terminal), and there is
no ingress-health gate. Suspension waits for the Sandbox bootstrap and every
Session's setup (running setups are a runner fact; the Sandbox is `Ready` False with reason `SuspendPending`); deletion does not wait. A Sandbox that is quiescing refuses resume. Action Service boxes are `ACTION_BOX_IDLE_REAPER`'s, which
only ever deletes.

Control design: [ADR](../docs/adr_sandbox_session_control.md).

### `SESSION_REVIVE` — start a stopped Sandbox again

**Acceptance audit remaining.** Same-state-PVC VM Codex resume with changed workload UID, unchanged
Session identity, journal continuation and native recall has passed; do not repeat that implementation
or scenario as an open gate. Audit deterministic coverage for concurrent resume, interrupted setup
and incompatible retained storage; add only missing contract coverage. Current setup handling refuses
a failed/interrupted setup rather than restarting it. Long-stop credential validity belongs to
`RESUME_CREDENTIALS_VALID`, accepted workspace/replacement evidence to the storage acceptance note, and dormant
harness restoration to `SELECTIVE_SESSION_REVIVAL`. Remove this node once its bounded coverage audit
is complete; a missing live version of an adequately automated case is not another gate.

Control design: [ADR](../docs/adr_sandbox_session_control.md).

### `AGENT_RESUME_NOTICE` — tell agents when their Sandbox was suspended and resumed

**Unscheduled; operator, 2026-10-13 PDT: it would be good.** An agent whose Sandbox was suspended and
resumed learns nothing of it: its tool processes are gone, a terminal or background process it started
no longer runs, and whether its workspace survived depends on where it lived (the state PVC is kept; a
VM-local disk is not, until `VM_RAM_SUSPEND`). Tell the agent when a new incarnation of its Sandbox
started, how long it was down, what was kept (state PVC, workspace) and what was lost (processes,
terminals). The runner knows the incarnation change; what survives is a fact of the Sandbox's kind and
volumes, and a suspend ends background processes and terminals, which do not count as busy
(`RUNNER_BUSY_TRACKING`). Open: whether the notice is a runner
platform instruction added at harness start, an event the History Service folds into the Session, or a
message through the Dispatcher; and the wording agents act on. Test a suspend and resume with a
background process, a terminal and workspace files, and a crash restart (no suspend) that reads
differently.

Related: `SESSION_REVIVE`, `SANDBOX_IDLE_EXPIRY`.

### `INCARNATION_UID_REQUIRED` — the runner rejects a `Welcome` without the incarnation UID

**Blocked on the gateway image that sends the UID pinned in
every environment, and in the VM guest's runner wheel pin.** Removes the missing-UID tolerance: a
`Welcome` without a UID ends the stream, and the `None` path in the runner store's incarnation
adoption (marked by its `CLEANUP` tombstone) goes. Pin ordering is not relied on before this; afterwards a
runner too old to know the field cannot meet a gateway that no longer sends it.

### `JOURNAL_LEGACY_SEAL_SCHEMA_REMOVE` — delete the pre-incarnation seal compatibility path

**Blocked on `INCARNATION_UID_REQUIRED` and a verified inventory/cutoff of old retained journals.**
`runner/journal.py` still probes `PRAGMA table_info(seal)` and adds a missing `incarnation_uid`
column, accepting UID-less seals. Once the separately approved storage cutoff covers those journals
and old Pod images/VM wheels can no longer write them, remove this old-schema upgrade and missing-UID
seal tolerance, with obsolete fixtures. Do not erase journals incidentally; if the workspace cutoff
leaves any old state PVC, name it in a separate operator-reviewed data-disposition step first.

### `RUNNER_HEARTBEAT_SPLIT` — remove runner-only facts from service heartbeats

**Candidate.** Facts already travel from the runner to the gateway's incarnation-stamped connection
Lease and are read by the controller. Only the wire-type cleanup remains: use a runner-only
`RunnerHeartbeat` on `RunnerFrame.heartbeat`, retaining field numbers, while service heartbeats
carry no runner facts. Update all users and generated outputs atomically; delete the shared-message
facts field once no live caller requires that shape. This wire-compatible cleanup is not a new
prerequisite for idle suspension or bootstrap readiness.

### `RUNNER_BUSY_TRACKING` — the runner counts terminals and background processes as busy

**Unscheduled; operator, 2026-10-14: not now, an open TODO that may be looked at later.** The runner
publishes `idle_since` (`RunnerFacts`), busy while a delivery is under way, the bootstrap runs, or any
Session has a turn, an unfinished command or a setup script. A background or durable process or an
attached terminal or console does not make the Sandbox busy, so an idle suspend ends them. The runner
does not supervise these, so who would report them (the harness adapters, the VM console) is open.
Test an attached terminal that detaches, a background process that outlives a turn, and a runner
restart. `AGENT_RESUME_NOTICE` tells the agent what a suspend ended.

### `QUIESCE_LEGACY_SHAPE_REMOVAL` — the controller stops reading the blocking `Quiesce` shape

**Blocked on the runner's non-blocking `Quiesce` pinned in every environment, the VM guest's wheel
included.** Deletes the controller's legacy-shape read and the tombstone marking it.

### `QUIESCE_WITHOUT_POLLING` — a runner that never progresses is backed off, not hammered

**Candidate; the Lease-facts images are pinned in testing and staging.** The
controller already calls once and reacts to the Lease stage events, re-issuing the idempotent call
only at a one-minute deadline. What is left: while the ingress is down the controller backs off
instead of retrying per Sandbox, and a live test (a Sandbox with a Session whose flush is held
back) covers a gateway rollout mid-seal, a runner restart mid-seal and a lost event. Neither could
be exercised on `agentplane-testing` without a held-back flush.

### `SESSION_ACTIVITY_STREAM` — the notification service delivers on a Session's idle time

**Blocked on `NOTIFICATIONS_VIA_DISPATCHER`.** The History Service folds `idle_since` into
`SessionSummary` (the turn and harness events that start and end idleness), and the app shows it.
What is left is the second listener: the notification service reads the Session's summary after each
`WatchSessionSummaries` notice and picks its own moment, so it delivers after a minute of idleness,
at most ten minutes after the first pending entry, through `NOTIFICATIONS_VIA_DISPATCHER`. Its
`NoticeDebounceSettings` quiet window stays about inbox entry times until then. `idle_since` is
display and delivery timing: suspension reads the runner's busy facts (the connection Lease facts) and
never this field. Per-Session idle times stay out of Kubernetes.

Control design: [ADR](../docs/adr_sandbox_session_control.md).

### `SANDBOX_SERVICE_CONFIG_REMOVE` — stop configuring the retired Sandbox Service clients

**Assigned to the Sandbox Service worker; recheck the #10466 app image pin before config removal.**
The accepting image was verified pinned in testing and staging on 2026-10-14. Direct app lifecycle config
has rolled out; the optional-target receiver and conditional channel/health probe have merged.
Remove the app's `sandbox_service_target`, token projection and obsolete network edge, and the
Notification Service's unused `sandbox_service` config, token and egress rule. Check all remaining
readers before removing a projected token. Generate manifests canonically and verify both rollouts
before deleting strict settings fields. Update the deployed traffic diagram with the config change.

### `SANDBOX_SERVICE_REMOVE` — delete the Sandbox Service and its legacy clients

**Blocked on `SANDBOX_SERVICE_CONFIG_REMOVE` rolled out and all caller images using direct inventory.**
Delete the app's gRPC lifecycle fallback, obsolete settings/client/channel/probe code, and Notification's
unused `SandboxServiceSettings`. Remove the Service, Deployment, ServiceAccount, Role/Binding,
TokenReview grants, network rules, image publication/Flux registration and migration-free service wiring.
Delete its proto/service client, console/grant RPCs, server and tests, after moving any still-used shared
inventory code into `agentplane/sandboxes/`. Search the whole repo, including acceptance and isolated
fixtures, before deleting a shared helper or generated target. Regenerate derived files and update
service docs and the README diagram. Preserve History Service's independently owned database; database
renaming is deferred. No data deletion is bundled with this retirement.

### `DISPATCHER_REPLICA_ACTIVATION` — remove the singleton deployment restriction

**Candidate; #10469 (per-Sandbox ownership) and #10478 (cross-replica terminal broadcast) are merged.**
The Dispatcher image and schema migration 0004 are deployed in testing and staging; their health and
pin gates are clear. Run two replicas on testing; verify ownership failover, queued delivery and
deletion events across replica boundaries without the app. Then remove the one-replica limit and
`Recreate` strategy in generated manifests, roll out testing before staging, and delete the temporary
singleton warnings from docs. This node finishes with the restriction gone, not merely a successful
experiment; preserve the normal Lease renewal/fencing machinery.

### `DISPATCHER_WAKE_ABUSE_LIMITS` — enforce the initial wake budget

**Candidate; assigned to the Dispatcher worker. Operator, 2026-10-14: use an initial limit of
10 wakes per minute and leave tuning for later.** Enforce the budget per authenticated caller,
shared across Dispatcher replicas and restarts. Deduplicate retries of the same wake and test
concurrent requests, limit exhaustion and recovery. Surface exhaustion explicitly; do not drop
queued work or poll for budget availability. Preserve the existing wake allowlist and per-Sandbox
policy; this decision does not authorize agent callers or change destination authorization.
Keep wake activation off until enforcement and its accepting image are deployed.

TODO: tune the rate and algorithm from observed use; initial numerical tuning is not an activation
gate. Document the accounting window and which attempted wakes consume the budget in the implementation.

### `SELECTIVE_SESSION_REVIVAL` — resume only native Sessions with pending work

**Candidate.** Audit runner startup and Dispatcher delivery together: selecting queued Dispatch rows
alone does not prove dormant harnesses remain stopped. Preserve native identity/history and start only
Sessions with pending work after a wake; cover multiple retained Sessions and explicit manual resume.

### `DISPATCHER_WAKE_FAILURE_FEEDBACK` — bound a failed revival with a visible cause

**Candidate.** Surface unavailable/full volume nodes and other terminal revival failures in delivery
state and the app, with a bounded wait and no automatic data deletion. Use condition/events and an
explicit deadline; test failure and recovery without arbitrary sleep or increased timeouts.

### `DISPATCHER_WAKE_ACTIVATION` — enable send-triggered revival

**Blocked on reviewed/enforced `DISPATCHER_WAKE_ABUSE_LIMITS`,
`SELECTIVE_SESSION_REVIVAL`, `DISPATCHER_WAKE_FAILURE_FEEDBACK` and accepting controller/Dispatcher images.**
The defaults-off wake implementation from #10463 is merged. Verify already-granted Sandbox patch RBAC and schema/image pins, then enable the approved caller
allowlist and `wakeOnSend` configuration. Test message-during-seal, concurrent sends and deletion
precedence on testing before staging. Keep `wakeOnSend: false` as the intentional per-Sandbox policy;
it is not a compatibility shim to delete.

### `DISPATCHER_HARNESS_CONFIRMATION` — settle admitted inputs on a canonical confirmation

**Decision, then bounded implementation.** `HARNESS_CONFIRMED` currently has no writer. Review the canonical runner/history
fact that proves an input reached the harness, consume it idempotently and settle the matching
Dispatch without reading unrelated conversation content. Preserve the distinction between durable
queue acceptance, runner admission and harness confirmation. Test duplicate/replayed facts and
restart. Notification presentation/outcome design remains separate.

### `DISPATCHER_SEND_AUTHORIZATION` — scope which callers may submit work

**Decision.** Current `caller_accounts` admits every configured caller to every Sandbox. Review
destination/operation authorization for the current app and notification callers;
agent senders wait for the reviewed multiagent authority contract. Do not confuse send permission
with wake permission or silently expand the caller allowlist during a cutover. Add the bounded
implementation node once policy is reviewed; existing caller cutovers do not silently select policy.

### `APP_DISPATCHER_ACTIVATION` — activate app command sends through Dispatcher

**Candidate; #10475 is merged, and the accepting app image is healthy in both environments.** The
app configuration still has `noDispatcher` and no Dispatcher arguments. Pin an image that accepts
the complete setting before adding endpoint/token/network configuration, then verify queued,
admitted and confirmed UI states in testing before staging. No fallback to direct sends on a
Dispatcher error. Keep session creation and explicit native resume accounted for separately until
all start-work callers have migrated.

### `APP_SESSION_OPEN_VIA_DISPATCHER` — route app session creation and native resume

**Blocked on `APP_SESSION_DEFAULTS`; can be prepared alongside command sends.** Route complete
Session specs through Dispatcher `OpenSession`. Explicit resume of a retained Session must also go
through an idempotent Dispatcher operation; add its receiver before activating the caller if needed.
Pin accepting Dispatcher and app images before config activation. Remove app-side gateway readiness
and in-process waiting as queue authorities. Verify retained Session identity and suspended opening.

### `APP_GATEWAY_SEND_REMOVE` — delete app direct command and session-start branches

**Blocked on `APP_DISPATCHER_ACTIVATION` and `APP_SESSION_OPEN_VIA_DISPATCHER` rolled out in both
environments.** Remove optional Dispatcher selection and the legacy direct command/CreateSession/
ResumeSession branches; make Dispatcher configuration required. Delete obsolete client methods,
queue/progress adaptations and tests. Preserve independently used read-only Gateway credentials and
routes until their callers migrate under `GATEWAY_WRAPPER_RETIRE`; do not delete all gateway access
merely because command sends moved.

### `NOTIFICATIONS_VIA_DISPATCHER` — notifications send through the Session Dispatcher

**Assigned to the Sandbox Service worker; recheck the #10465 image pin before activation.**
The accepting image was verified pinned in testing and staging on 2026-10-14.
The optional sender implementation has merged; this node is the manifest activation and acceptance. The Notification Service sends its
notices through the Session Dispatcher and keeps choosing when to deliver from its existing quiet window.
It submits the already persisted notice ID and text, treats queued as
acceptance rather than runner admission, and keeps reading admission/confirmation from History Service.
The optional endpoint/token setting is code-first: pin an image that accepts the complete setting before
adding it to the manifest. Once configured, a Dispatcher error never falls back to Runner Gateway.
The app's separate send activation is tracked by `APP_DISPATCHER_ACTIVATION`; it is not a prerequisite
for this Notification Service route.

### `NOTIFICATION_GATEWAY_SEND_REMOVE` — remove the pre-activation gateway command path

**Blocked on `NOTIFICATIONS_VIA_DISPATCHER` and healthy rollout of its Dispatcher config.** After the
manifest routes sends to a pinned, accepting image, remove the old direct `SubmitCommand` branch and make
the Dispatcher endpoint required. Keep Runner Gateway only for the Notification Service's read-only
`ListSessions` validation. This cleanup must land before `PROXY_START_METHODS_DISPATCHER_ONLY` closes command
RPC authorization to the Notification Service.

### `RESUME_CREDENTIALS_VALID` — grants and tokens still valid after a long suspension

**Blocked on `SESSION_REVIVE`.** A Sandbox that resumes hours or days later must come up with egress
grants, `PolicyBinding`s and projected tokens still valid, or it looks broken. Check each
expiring thing the Sandbox holds, and make a resume refresh or refuse with a reason, never come up
half-granted. Grants made from the app can carry an `expiresAt`.

### `SUSPENDED_COMPOSER` — the composer stays enabled while a Session is suspended

**Blocked on `DISPATCHER_WAKE_ACTIVATION`, `APP_DISPATCHER_ACTIVATION` and `HOSTED_THREAD_SURFACES`.** A suspended
Session in the app has an enabled composer; sending queues the message and shows that the Sandbox
is starting and the message waits.

Control design: [ADR](../docs/adr_sandbox_session_control.md).

### `THREAD_SUCCESSOR_DELIVERY` — commands unsettled across incarnations

**Blocked on revival.** A command whose admission was ambiguous when an incarnation ended is
redelivered to the next one, which resolves the ambiguity (the runner deduplicates on the command
ID). It is `unknown` only when the Sandbox was deleted. Moved from the freezer.

Control design: [ADR](../docs/adr_sandbox_session_control.md).

### `HOSTED_THREAD_SURFACES` — show and revive stopped Sessions

**Blocked on revival.** The app lists Sessions whose Sandbox has no
running incarnation, reads their history from the History Service, and offers an explicit revive,.
Moved from the freezer.

## 8. Ownership and naming cleanup

Cleanups that leave one database per service and names that say what things are. Each node is one
deployment; edges are the known order. Staging data follows
[staging data preservation](../docs/staging_data_preservation.md): Session identities and history
are irreplaceable, and a node that deletes a column or row stops for the operator.

```mermaid
flowchart TD
    HISTORY_DATABASE_RENAME[Candidate: rename the sandbox_service database and role]
    SANDBOX_DATABASE_RENAME[Unscheduled: name the sandbox_commands database for the Runner Gateway]
    ACTION_BOX_RENAME[Unscheduled: Action boxes stop being called sandboxes]
    ACTION_BOX_IDLE_REAPER[Unscheduled: delete Action boxes nobody uses]
    SANDBOX_IDLE_EXPIRY[Unscheduled: idle runner Sandboxes suspend, section 7] -. same idleness question .-> ACTION_BOX_IDLE_REAPER
    GATEWAY_WRAPPER_RETIRE[Blocked: callers use the proxy; RunnerGateway goes, section 12] -. makes moot .-> SANDBOX_DATABASE_RENAME
```

### `HISTORY_DATABASE_RENAME` — name the database for its owner

**Candidate.** Rename the `sandbox_service` database and role
(and the `postgres-sandbox-service` Secret) for the History Service. Rename in place (`ALTER DATABASE`/`ALTER ROLE ... RENAME`)
and have the CNPG `Database` and managed role adopt the new names; recreating the database under a
new name would copy the log. Verify the History Service and Electric reconnect with no data movement.
The `storage/migrate.py` and `cluster/cdk8s/agentplane/history_service.py` docstrings take the
new name.

### `SANDBOX_DATABASE_RENAME` — name the Runner Gateway's database for it

**Unscheduled; operator, 2026-10-11 PDT: keep the name for now, not urgent.** `sandbox_commands` holds the Runner Gateway's Session registry and command
admission; the Sandbox Service has no database. Rename it and its role and Secret for the gateway
in place, as `HISTORY_DATABASE_RENAME` does, and point the Runner Gateway, its migration init
container and `commands_database_url` at the new names. `database.py`'s comment on
`sandbox_commands` follows.

### `ACTION_BOX_IDLE_REAPER` — delete Action boxes nobody uses

**Unscheduled; operator, 2026-10-11 PDT.** A small controller deletes Action box Pods with no
`exec` for a configured time. The Action Service stamps a last-exec time on the box at each `exec`;
a plain Pod delete is enough, since boxes have no seal. It takes over `TODO(sandbox-lifetime)` in
`action_service/sandbox/inventory.py`; draft [#7859](https://github.com/agentydragon/ducktape/pull/7859)
expired agent-sandbox boxes the same way.

### `ACTION_BOX_RENAME` — Action boxes stop being called sandboxes

**Unscheduled; operator, 2026-10-11 PDT: a later TODO.** Rename what calls an Action Service box a
sandbox, so it no longer reads as a Session Sandbox: the `sandbox__*` Actions,
`action_service/sandbox/`, the `agentplane-sandbox` templates and labels, and
`cluster/cdk8s/agentplane/command_sandbox.py`. The `sandbox__*` tool names are what agents and
external Connections call, so renaming them is a wire change.

## 9. Legacy burn-down

Dead code, finished gates, compatibility shims and stale names found by the 2026-10-10 PDT audit of
devel. A gate counts as legacy once its rollout is finished on both testing and staging or nothing
reads it; the delta settlement flag stays (operator, 2026-10-13 PDT). Lane named per
node; leftovers of in-flight work belong to the lane already changing that code. Upstream-gated
tombstones with a verifiable condition in code (`keepingOffset` in
`thread_store.tsx`, the jsdom pragma in `markdown.test.tsx`) need no node.

```mermaid
flowchart TD
    ACTION_EXECUTION_AUDIT_TRIM[Decision: execution reconciliation columns]
    REFACTOR_COHERENCE_AUDIT[Blocked: whole-refactor coherence audit]
```

### `ACTION_EXECUTION_AUDIT_TRIM` — execution reconciliation columns

**Decision; drops columns, so the operator decides.** `ExecutionRow.reconciled_by` and
`reconciliation_source` in `action_service/db.py` are written but read only by a test, and
`ReconciliationSource` has one member. Either expose `reconciled_by` in the execution view, or
drop both columns and the enum.

### `REFACTOR_COHERENCE_AUDIT` — the refactor reads as one design

**Blocked on the cutover's retirements and the split**, which remove the
transitional pieces the other nodes name. Sweep devel for unused modules, functions, settings, proto
fields, tables and columns; one name per concept across code, settings, manifests, protos and docs
(Runner Gateway, History Service, ingress, control; Session for the raw stream,
Thread for its projection; no "hub", "follow" or "shadow" left); READMEs, `SPEC.md` files and
`docs/` describing the code as it is, without cutover narration; no compatibility branch,
optional-with-fallback setting or `CLEANUP` tombstone whose condition is met; the system diagram in
`agentplane/README.md` showing exactly the deployed services and the traffic between them in
`cluster/cdk8s/agentplane`. Known now: the test-only `PostgresListener.wait_until_disconnected`
(`connected` turns false before the termination callback runs, so its tests cannot wait on
`connected` instead); egress and app test support each build a throwaway CA;
`cluster/cdk8s/agentplane/kubevirt_experiment` is an experiment outside `x/`. Findings are fixed in
PRs of their own; this node leaves with the last of them.

## 10. Runner ingress and control split

Target and decisions: [History Service plan](history_service.md#target). The runner keeps dialing
out, but to two endpoints its Sandbox names: an **ingress** it pushes its journal's Events to, and
a **control endpoint** (the Runner Gateway) whose stream it holds for commands, receipts and lifecycle
requests. The History Service is the ingress when history is kept. The contract is
[runner control stream § Event ingress](../docs/runner_control.md#event-ingress); it replaces following the
runner through the Sandbox Service.

```mermaid
flowchart TD
    RUNNER_JOURNAL_TRIM[Unscheduled: runners stop keeping what the ingress committed]
```

### `RUNNER_JOURNAL_TRIM` — runners stop keeping what the ingress committed

**Unscheduled; the operator's direction, 2026-10-12 PDT: later.** A runner could stop persisting
journal entries its ingress has already acknowledged, behind a flag, with full persistence kept
selectable; the freezer's
[`RUNNER_JOURNAL_SETTLEMENT`](task_freezer.md#runner_journal_settlement--settle-deltas-in-the-runner-journal-too) is the same idea at the delta level. Not designed: what the runner still needs locally (cursors, seal state, entries a
resumed harness or a lost push reads back) once acknowledged entries are gone, including what a revived incarnation reads back
(the retained incarnation history).

## 11. Coherence refactors

No refactor is open here.

## 12. The Runner Gateway serves the runner's own service

Target and decisions: [History Service plan](history_service.md#decided-operator-2026-10-10-pdt)
(Runner Gateway shape, Session specs). Today `RunnerGateway` wraps every `Runner` call with a
Session registry, spec assembly, readiness checks and seals; these nodes move each job to its owner
until the gateway is the `Connect` listener plus a proxy of `Runner` with per-method authorization.

```mermaid
flowchart TD
    APP_SESSION_DEFAULTS[Candidate: the app sends complete Session specs]
    RUNNER_COMMAND_RPCS[Blocked: DeliverCommand goes once nothing calls it]
    GATEWAY_WRAPPER_RETIRE --> RUNNER_COMMAND_RPCS
    GATEWAY_WRAPPER_RETIRE[Blocked: callers use the proxy; RunnerGateway goes]
    GATEWAY_DATABASE_DROP[Blocked: drop the sandbox_commands database]
    GATEWAY_STATUS_RBAC_REMOVE[Blocked: the gateway loses its Sandbox status write]
    APP_SESSION_DEFAULTS --> GATEWAY_WRAPPER_RETIRE
    APP_GATEWAY_SEND_REMOVE --> GATEWAY_WRAPPER_RETIRE
    NOTIFICATION_GATEWAY_SEND_REMOVE --> GATEWAY_WRAPPER_RETIRE
    PROXY_START_METHODS_DISPATCHER_ONLY[Blocked: narrow proxy command authorization]
    GATEWAY_WRAPPER_RETIRE --> PROXY_START_METHODS_DISPATCHER_ONLY
    GATEWAY_WRAPPER_RETIRE --> GATEWAY_DATABASE_DROP
```

### `APP_SESSION_DEFAULTS` — the app sends complete Session specs

**Candidate; a for-now choice.** The app records which preset a
Sandbox was made from on the Sandbox (a label) and fills a complete `SessionSpec` and setup script
from it. `session_defaults` leaves `SandboxBinding` and our Sandbox's spec (`CreateSandboxRequest` goes with `SANDBOX_SERVICE_REMOVE`),
and `CreateSessionRequest` loses its field mask.

### `PROXY_START_METHODS_DISPATCHER_ONLY` — only the Dispatcher starts or continues work through the proxy

**Blocked on `APP_GATEWAY_SEND_REMOVE`, `NOTIFICATION_GATEWAY_SEND_REMOVE` and `GATEWAY_WRAPPER_RETIRE`,
with their caller images pinned in both environments.** Today
the gateway's runner proxy (`runner_gateway/runner_proxy.py`) lets every configured caller account call
every runner method except `Quiesce` and `Seal`, and the gateway wrapper calls `OpenSession` too. The
bound on a suspend waiting for setups (`SuspendPending`, `SANDBOX_IDLE_EXPIRY`) holds only if nothing
but the Dispatcher can open a Session or send work, so the proxy's per-method policy authorizes
`OpenSession` and the other start-or-continue-work methods (`SubmitInput`, `ResumeSession`,
`ChangeModel` and `ChangeReasoningEffort`, which start a stopped harness; operator, 2026-10-13 PDT) for
the Dispatcher's account alone. `InterruptTurn`, `StopRunnerSession` and the reads stay open to the callers
that have them. Order: every other caller has moved to the Dispatcher, then the policy narrows.

### `GATEWAY_STATUS_RBAC_REMOVE` — the gateway loses its Sandbox status write

**Blocked on the Runner Gateway image without the deletion seal pinned in every environment.** Remove the
`sandboxes/status` `patch` rule from the gateway's Role in `cluster/cdk8s/agentplane/runner_gateway.py`
and the `CLEANUP` beside it. The old image still patches `Quiesced` until its pin moves, so the rule goes
only after that.

### `RUNNER_COMMAND_RPCS` — `DeliverCommand` goes once nothing calls it

**Blocked on `GATEWAY_WRAPPER_RETIRE`; operator, 2026-10-13 PDT: one RPC per command rather than one
taking a union.** The runner serves `SubmitInput`, `ChangeModel`, `ChangeReasoningEffort`,
`InterruptTurn` and `StopRunnerSession` beside `DeliverCommand`, and the proxy admits every method of
`Runner` (it builds its method set from the service descriptor). The only caller left of
`DeliverCommand` is the Runner Gateway's `SubmitCommand` path, which goes with
`GATEWAY_WRAPPER_RETIRE` and needs no split. Then `DeliverCommand`, `Command` as a request type and
`DeliverCommandRequest`/`DeliverCommandResponse` are deleted from `runner_control.proto` once no
pinned caller image sends it (the VM guest's runner wheel is a reader of the new RPCs, not a caller).
The journal's `CommandAdmitted` event keeps storing the `Command` message, so stored history needs no
migration and `Command` stays as that event's field type.

### `GATEWAY_WRAPPER_RETIRE` — callers use the proxy; `RunnerGateway` goes

**Blocked on `APP_SESSION_DEFAULTS`, `APP_GATEWAY_SEND_REMOVE` and `NOTIFICATION_GATEWAY_SEND_REMOVE`.**
Audit and migrate every remaining wrapper read caller (including notification `ListSessions`) to
History Service or the runner proxy, then pin those caller images before deleting the RPC receiver.
App and notification command sends go through Dispatcher; read-only calls may use the proxy. Delete the
`RunnerGateway` service and proto, its client, `session_registry.py`, `destinations.py`,
`session_lifecycle.py` and `wire.py`, obsolete client settings, projected tokens and network rules
whose final callers are gone. Delete associated BUILD targets and tests; retain the control stream
and runner proxy. This makes `SANDBOX_DATABASE_RENAME` moot; delete that deferred node here.

### `GATEWAY_DATABASE_DROP` — drop the `sandbox_commands` database

**Blocked on `GATEWAY_WRAPPER_RETIRE`; a data drop, merged by the operator.** Drop the database,
its role, Secret, migration init container, old models/migrations and image publication wiring once
the History Service holds every Session's Sandbox and all pinned gateways have stopped reading or
writing this database. First remove strict config keys from manifests with an accepting receiver;
only after that rollout delete their settings readers. This remains a separate data-drop PR.

## Scope and retirement of stale gates

- Notifications keep workers in the HTTP service. Retention, worker extraction, idle-polling
  optimization and new-source wiring are frozen with triggers, not hidden release prerequisites.
- Broad provider-matrix exploration is frozen; ordinary auth/signature denial regression coverage
  belongs with the implementation, not an unbounded production checklist.
- Native subsessions and runner durability/thin-adapter redesign remain frozen. Neither the runner
  channel nor multiagent design unfreezes the broader runner-state redesign.
- Existing security boundaries, data-preserving migration checks and representative deployment
  integration checks remain requirements. The freezer is not a waiver for a known data-loss or
  unauthorized-access bug; promote one when evidence makes it concrete.
