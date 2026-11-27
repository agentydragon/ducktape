# Agentplane task DAG

This is the dispatch map for **unfinished** work. Detailed contracts belong in component plans;
completed work belongs in component docs, not done nodes here. Maintenance rules are in
[task DAG maintenance](../docs/task_dag_maintenance.md). Deliberately deferred candidates and conditional
hardening live in the [freezer](task_freezer.md), except the explicit nonblocking
intra-Sandbox hardening reminder retained here at Rai's request.

## Current state and scheduling

### Operator interests, 2026-11-22

The operator listed these directions without ranking them. There is also a current staging failure.

- [Recover wedged Sessions and Sandboxes](#wedged_session_and_sandbox_recovery--archive-a-session-and-delete-a-sandbox-when-the-runtime-cannot-recover).
  Reproduce the Sandbox deletion wedge and capture the separate Session archive refusal. The
  deletion fix can proceed now. [Archive semantics](#session_archive_contract--what-archiving-a-session-means)
  are a later design decision.

- [Sessions and Environments cutover leftovers](#sessions-and-environments-cutover-leftovers).
  Verify the Sessions image rollout. Retiring the transitional Sessions Pod label requires a
  separate check of the live Service endpoints.
- [Firecracker on the cluster](#firecracker_integration_design--choose-firecracker-integration-and-privilege-boundary).
  Evaluate an existing Firecracker integration before choosing how to provision privileged
  infrastructure. Keeping the Agentplane launcher in a dedicated namespace remains an option.
- [Central Sessions with zero to many Environments](#sandbox-free-central-sessions). Refresh the
  creation, attachment and lifetime proposal against current Identity, setup and delivery behavior.
  The first implementation slice is a central Session that can continue with zero attachments.
- [Kubernetes decoupling](#kubernetes_decoupling_design--how-far-to-decouple-from-kubernetes).
  Identity still lives as a Kubernetes object. Trace its creation and live UID checks first if
  this direction is selected. Central Sessions can name an Identity with the existing store.
- [Organization work on GitHub Pages](#17-two-binaries-and-the-go-layout). The Go plan has no
  selected file moves left. Settings parity and test selection have concrete experiments.
- [Authored Thread and diagnostic History](#agent_authored_conversations_design--conversations-agents-act-on-through-tools).
  The CLI already demonstrates both views. The next product experiment is a hosted Session using
  an optional Thread attachment chosen at Session startup. Its messenger shows deliberate posts and
  edits. History remains available for diagnosis.

The Oct 30 sprint ended Nov 13. Concrete failures in ordinary Session use still warrant repair;
the operator waived broad disruption exercises and assigned low priority to VM startup latency.
Scheduled wake, the daily CI agent and local Bazel in a VM have no current dispatch priority.
The longer-term goal remains reliable long-running agents, including independently addressable
child Sessions. See the [release decisions](#release-decisions) before dispatch.

The [two binaries](../docs/adr_two_binaries.md) have separate duties. Sessions owns admission,
delivery and automatic wake. Environments owns Sandbox lifecycle and calls into its Runtime.

**Quality bar for every selected outcome:** sound domain models, clear ownership/lifetimes and
maintainability are acceptance criteria. Review the affected domain contract before implementing
each slice. Keep Session identity, native state, running processes, environment instances and
authority distinct; a smaller feature scope must not create duplicate ownership or hidden coupling.
Apply the relevant runtime-model directions now where a selected feature needs them, without
requiring the entire north-star redesign first. Record concrete tradeoffs rather than treating
technical debt as an automatic later cleanup.

Apply the [Agentplane principles](../docs/principles.md) throughout: resilient distributed ownership,
service-owned recovery and independently usable capabilities. A required service or constraint
needs an intrinsic execution/security reason or an explicit practical tradeoff; incidental current
colocation and implementation limitations do not make it part of the domain contract.

**Agent usability is part of completion:** an agent must discover and naturally use the workflow
from an ordinary task description, understand results/failures and recover without an operator's
step-by-step tool script. Reuse canonical APIs/CLI/tool surfaces and concise actionable results.
Use Claude/Codex spawning, messaging, waiting, result and cancellation workflows as reference
designs; do not make native-child projection or exhaustive harness parity prerequisites.

**Realistic working examples are required; operator, 2026-10-30 PDT.** Subagent/delegation,
Session spawning, environment provisioning/attachment, communication and history-reading work must
include concrete end-to-end agent workflows mapped to Agentplane's actual service contracts.
Show how an agent discovers operations and schemas, obtains valid environment/Session handles,
learns usable harness/model/cwd choices, and uses its authenticated authority. Include the separate
creation and initial-input steps, follow-up, result/history reads, waiting and relevant
failure/retry/cancellation behavior. Return structured status with its source and meaning; distinguish
service admission, harness observations and any explicitly supported agent-declared task status.

Cover an agent reusing an existing environment and an external MCP-only caller. Describe new-environment
and environment-free cases according to their actual supported capabilities. State required
configuration, permissions and approvals. During design, map each step to an implemented operation
or name the concrete addition and its owning backend. Before implementation completion, exercise
the supported examples through the real service boundaries and retain proportional evidence in
the owning component's documentation or acceptance suite. Update canonical agent-facing discovery
and guidance so an agent can follow the workflow from an ordinary assignment.

Workflow validation may expose missing backend capabilities or service boundaries that need
refactoring. Adding those features or refactors is an acceptable outcome: record the concrete gap,
owning backend and real prerequisites, then revisit scope, ordering and priorities with the
stakeholder. An example with unresolved steps remains a proposal until that work is implemented
and exercised.

The operator is reusing environments partly because fresh startup is slow. Separate directories
are acceptable for cooperating agents with shared access; that does not imply isolation between
different principals. Starting less-privileged workers can wait: the selected first spawning
scope uses the caller's ServiceAccount and may reuse its environment.

Audit metadata consumers (`sandbox`, `harness`, `model`, `cwd`, summary/activity fields) before
deleting useful UI projections.

States: **in flight** means reported work is underway; **in review** means an unmerged PR;
**paused** means no worker is assigned; **decision** needs a reviewed outcome;
**blocked** names prerequisites; **candidate** is dispatchable when selected, not a priority claim.
**Unscheduled**, **deferred** and **parked** nodes have no owner and no priority; a deferred or parked one waits on a trigger its text names.
A **capstone** closes an integrated contract, not another implementation. Solid arrows below are
prerequisites; dashed arrows explicitly label scheduling holds or conditional choices. All service
contracts remain multi-replica unless a reviewed temporary restriction says otherwise.

### Unexplained cleanup timeouts

The original native-cleanup timeout remains unexplained; the closed
[#13442](https://github.com/agentydragon/ducktape/pull/13442) path-reconstruction workaround is not
selected. A later, separate [Runtime.Close caller-deadline failure](../env/runtime/debug/2026-11-01_runtime_close_wait_timeout.md)
also lacks causal evidence. On recurrence, capture shutdown-stage/join evidence before attributing
either failure or claiming it fixed. Passing later tests and the merged Runtime COSI improvements
do not establish either cause.

A third is also unexplained: a Dispatcher system-test cleanup timed out waiting for the Sandbox to
stop ([note](../debug/dispatcher_cleanup_timeout.md)). On recurrence, read its captured diagnostics
before changing lifecycle behavior.

### Release decisions

Removing `CommandSettled`, retiring `orchestration_v1` and adding launcher auto-grants remain held. The stable provider
prefix design [awaits operator review](#awaiting-operator-review).

### Sandbox-free central Sessions

The operator asked to track central sampler Sessions that can run without a Sandbox (2026-11-14
07:43 UTC). Today `central_sampler_v1` requires one exact executor in Dispatcher admission, sampler
binding and History ingress. The review sequence is below. Multiple attachments are follow-on work
after zero-Environment admission and shared-Environment Identity enforcement.

**Operator direction, not a release (2026-11-17 13:21 UTC, among "things that are on top of my head
right now as stuff i'd like to pursue sometime"):** "architecturally-wise, one nice thing would be
to land the "central sampler's sessions can live without a sandbox attached and sandboxes can
attach/detach to them at runtime, possibly multiple of them", but that's more a "it would be nice
if" / refactor to make agentplane impose less structure that isn't strictly speaking necessary".
The table records the remaining decisions and their implementation dependencies.

| Step                        | Node                                                                                                                          | State                                                                                                       |
| --------------------------- | ----------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| Decide creation             | `CENTRAL_SESSION_CREATION_CONTRACT`                                                                                           | Drafted with the next two in [#15436](https://github.com/agentydragon/ducktape/pull/15436), awaiting review |
| Decide attachments          | `SESSION_ATTACHMENT_CONTRACT`                                                                                                 | Same PR                                                                                                     |
| Decide lifetimes            | `SESSION_AND_INBOX_LIFETIME`                                                                                                  | Same PR; row and inbox retention already implemented                                                        |
| Decide setup                | `SESSION_ENVIRONMENT_SETUP_DESIGN`                                                                                            | Setup stays per Session for now (operator, 2026-11-17); revisit before zero or several attachments          |
| Decide identity details     | `SESSION_IDENTITY_CONTRACT`                                                                                                   | Principal decided (an `Identity`); name, incarnation and shared-Environment rules open                      |
| Zero-Environment path       | `CENTRAL_SESSION_WITHOUT_ENVIRONMENT`                                                                                         | Blocked on the decisions above                                                                              |
| Follow-ons                  | `CENTRAL_SESSION_NOTIFICATIONS`, `SESSION_HISTORY_PRESENTATION`, `SESSION_ENV_ATTACHMENTS`, `SESSION_LIFETIME_IMPLEMENTATION` | Blocked on the zero-Environment path                                                                        |
| Code-mode host (not a gate) | `CODE_MODE_HOST_DESIGN`                                                                                                       | Open question: keep Codex's pinned JavaScript host or own a V8 host                                         |

[Central code-mode Actions](../sampler/SPEC.md#action-tools) already run as the recorded Session
Identity without using the executor. History's general contract allows Sessions with no Sandbox UID,
including retained rows. Sampler ingress still checks the exact V1 executor in the announcement.
[#16336](https://github.com/agentydragon/ducktape/pull/16336) and
[#16356](https://github.com/agentydragon/ducktape/pull/16356), which would have removed such rows
and refused such writers, were closed unmerged.

```mermaid
flowchart LR
    subgraph DECIDE[Decisions]
        CENTRAL_SESSION_CREATION_CONTRACT[Review: creation without an Environment, #15436]
        SESSION_ATTACHMENT_CONTRACT[Review: Environment attachments, #15436]
        SESSION_AND_INBOX_LIFETIME[Review: Session and inbox lifetimes, #15436]
        SESSION_ENVIRONMENT_SETUP_DESIGN[Decision: where setup belongs]
        SESSION_IDENTITY_CONTRACT[Decision: identity details]
    end
    CENTRAL_SESSION_WITHOUT_ENVIRONMENT[Blocked: central Session with zero Environments]
    CODE_MODE_HOST_DESIGN[Open: keep Codex's JS host or own V8]
    CENTRAL_SESSION_CREATION_CONTRACT --> CENTRAL_SESSION_WITHOUT_ENVIRONMENT
    SESSION_ATTACHMENT_CONTRACT --> CENTRAL_SESSION_WITHOUT_ENVIRONMENT
    SESSION_AND_INBOX_LIFETIME --> CENTRAL_SESSION_WITHOUT_ENVIRONMENT
    SESSION_IDENTITY_CONTRACT --> CENTRAL_SESSION_WITHOUT_ENVIRONMENT
    SESSION_ENVIRONMENT_SETUP_DESIGN -- setup ownership --> CENTRAL_SESSION_WITHOUT_ENVIRONMENT
    CENTRAL_SESSION_WITHOUT_ENVIRONMENT --> CENTRAL_SESSION_NOTIFICATIONS[Blocked: inboxes without an Environment]
    CENTRAL_SESSION_WITHOUT_ENVIRONMENT --> SESSION_HISTORY_PRESENTATION[Blocked: app views without a Sandbox]
    CENTRAL_SESSION_WITHOUT_ENVIRONMENT --> SESSION_ENV_ATTACHMENTS[Blocked: attach and detach Environments]
    CENTRAL_SESSION_WITHOUT_ENVIRONMENT -. central lifecycle .-> SESSION_LIFETIME_IMPLEMENTATION[Blocked: reviewed lifetimes]
```

### Identity and Sessions follow-up, 2026-11-13

Session execution and inbox ownership use recorded Identity, MCP Connections delegate their exact
live Identity, and components authenticate with role service SVIDs
([rollout record](../docs/sandbox_identity_cutover_2026_11_12/README.md)). The remaining lanes:

| Remaining lane                    | Current outcome                                              | Remaining boundary                                                                                                                                    |
| --------------------------------- | ------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| Standing workload observation     | Identity subjects are in source                              | Confirm the staging proxy decision log names the expected Identity; no source work is implied.                                                        |
| Session inbox cross-Identity send | Inbox owner and History workload reads use recorded Identity | Current send rule remains Sandbox/tag-targeted; target/group model and launcher-grant lifetime are unresolved under `SESSION_ACCESS_GROUPING_DESIGN`. |
| Sub-Session entry                 | Identity `CreateSession` remains separate                    | `SESSION_SUBAGENT_DELEGATION`; it does not reopen the workload cutover.                                                                               |

The environment-free central Session has its own track:
[Sandbox-free central Sessions](#sandbox-free-central-sessions).

```mermaid
flowchart LR
    subgraph IDENTITY_TRACK[Identity and Sessions follow-up]
        STANDING_WORKLOAD_IDENTITY[Standing workload proxy evidence]
        SESSION_ACCESS_GROUPING_DESIGN[Review Session access grouping] --> SESSION_IDENTITY_AUTHZ[Cross-Identity inbox send target]
        SESSION_SUBAGENT_DELEGATION[Identity CreateSession design]
        IDENTITY_SYSTEM_TESTS[Remaining automated identity-boundary acceptance]
        SANDBOX_IDENTITY_CHANGE_CHECKPOINT[Deferred VM checkpoint transition]
    end
    subgraph PRIORITY_ONE[P1 of the ended sprint: delegation usability]
        THREAD_CREATE_AUTHORIZATION[Operator follow-up: delegation usability]
    end
    SCHEDULED_AGENT_WAKE_DESIGN[Unscheduled proposal: persistent agents and scheduled wake] --> SCHEDULED_AGENT_WAKE
    SCHEDULED_AGENT_WAKE[Unscheduled: durable scheduled wake] --> DAILY_CI_AGENT
    DAILY_CI_AGENT[Unscheduled daily CI capstone]
    SESSION_LAUNCH_FRICTION[Alongside: simpler reused-environment launch]
    VM_STARTUP_LATENCY[Later: measure production VM startup]
    LOCAL_BAZEL[Next: Bazel client inside a VM]
```

## 2. Service-owned command admission and later notification presentation

```mermaid
flowchart LR
    SCHEDULED_AGENT_WAKE_DESIGN[Unscheduled proposal: persistent agents and scheduled wake]
    SCHEDULED_AGENT_WAKE[Unscheduled: blocked on design and operator selection]
    SCHEDULED_AGENT_WAKE_DESIGN --> SCHEDULED_AGENT_WAKE
    MANAGED_SUBSCRIPTION_PROTECTION[Unscheduled hardening: managed subscriptions the agent cannot cancel]
    SCHEDULED_AGENT_WAKE --> MANAGED_SUBSCRIPTION_PROTECTION
    NOTIFICATION_COMMAND_OUTCOME_DESIGN[Unscheduled: notice delivery without reading history]
    NOTIFICATION_SERVICE_SPLIT[Deferred split: keep Notifications in one service]
    NOTIFICATION_SERVICE_SPLIT -. related .-> NOTIFICATION_COMMAND_OUTCOME_DESIGN
    COMMAND_WATCH_SUBSCRIPTION_DESIGN[Unscheduled: a subscription kind that runs an agent's command on a schedule]
    COMMAND_WATCH_SUBSCRIPTION_DESIGN -. shared schedule design .-> SCHEDULED_AGENT_WAKE_DESIGN
    ACTION_WAKE_AS_IDENTITY[Unscheduled: authorize Action-originated wake as caller Identity]
    WAKE_ON_DELIVERY_DESIGN -. wake rule .-> ACTION_WAKE_AS_IDENTITY
    SESSION_INPUT_METADATA[Typed input annotations and provenance]
    CENTRAL_SESSION_WITHOUT_ENVIRONMENT[Central Session without Environment, section 13]
    CENTRAL_SESSION_INPUT_METADATA[Blocked: central sampler metadata passthrough]
    SESSION_INPUT_METADATA --> CENTRAL_SESSION_INPUT_METADATA
    CENTRAL_SESSION_WITHOUT_ENVIRONMENT --> CENTRAL_SESSION_INPUT_METADATA
    SESSION_INPUT_METADATA_READ[Blocked: metadata reads and message correlation]
    NOTIFICATION_NOTICE_METADATA[Blocked: attach notice metadata to submissions]
    NOTIFICATION_PRESENTATION[Blocked: compact frontend presentation]
    SESSION_INPUT_METADATA --> SESSION_INPUT_METADATA_READ
    SESSION_INPUT_METADATA --> NOTIFICATION_NOTICE_METADATA
    SESSION_INPUT_METADATA_READ --> NOTIFICATION_PRESENTATION
    NOTIFICATION_NOTICE_METADATA --> NOTIFICATION_PRESENTATION
```

Details: [command admission](../session_dispatcher/dispatcher/README.md#contract); later
[notification presentation and input metadata](notification_presentation.md).

### `ACTION_WAKE_AS_IDENTITY` — authorize Action-originated automatic wake as caller Identity

**Unscheduled; follows [`WAKE_ON_DELIVERY_DESIGN`](#wake_on_delivery_design--whether-delivering-session-work-wakes-its-environment).**
Dispatcher temporarily assesses Action-attributed sends through the Actions service broker grant.
Replace that mapping with authorization as the Identity that called the Action, under the wake rule
that design selects. Keep the effective Identity
and service actor separate in row attribution. Then remove Actions from the Dispatcher's
`wake_service_callers` and remove the Action-proof mapping to `ServiceWakeCaller`.

**Exit:** Action-attributed sends wake under the caller Identity's reviewed grant and are denied
without it; no Actions service wake broker or Action-specific mapping remains.

Future ownership and wake-policy questions are recorded in the
[freezer](task_freezer.md#notification_dispatcher_ownership--review-notification-and-dispatcher-ownership):
consider moving Notification Service responsibilities into Dispatcher, and event/source-specific
auto-wake policy. Neither is selected or a prerequisite for v1 inbox messaging.

### `SCHEDULED_AGENT_WAKE_DESIGN` — choose the durable scheduled-wake contract

**Declaration path decided; remaining design unscheduled; no implementation released.** The [persistent-agents proposal](persistent_agents.md)
consolidates scheduled wake, declarative agents/subscriptions and per-Session notice cadence.
[#13552](https://github.com/agentydragon/ducktape/pull/13552) is superseded by this proposal;
this does not close that PR. Notifications stays one service (operator, 2026-11-17).

**Operator, 2026-11-17 13:04 UTC, verbatim:**

> to inform design: i think scheduled agent wake would probably make sense to implement as a kind of inbox subscription to a regular cron. one question there might be "should we have declarative configuration for subscriptions for sessions that can't unsubscribe themselves", or maybe more genrally declarative configuration for specific agents that should come up and exist declaratively.
> i think it might also make sense to have configurable things like "how often should a specific session receive inbox notices at most", e.g. "at most 1x per 10 min", might help manage costs.
> all of this is subject to more thinking about how it should work, what might be the user story of "i want to run a persistent agent in agentplane, how do i configure it, ideally in manifests/config/whatever" - although "i want to run an agent ad hoc manually should still work too"

**Operator, 2026-11-17 13:11 UTC, verbatim:**

> i guess one option might be something like manifest -> flux -> k8s object -> operator/controller -> agentplane subscription object

**Operator decision, 2026-11-17 13:33 UTC:** on "Choose how a persistent agent is declared in
manifests", chose "Flux CR plus a new reconciler": a whole managed agent's small CR is applied by
Flux and reconciled through Dispatcher and Notifications APIs; operational state stays in Postgres.
Controller authority and the plan's remaining ranked questions are undecided.
The plan absorbs `DECLARATIVE_SESSIONS_DESIGN` and the scheduled-agent cases of
`COMMAND_WATCH_SUBSCRIPTION_DESIGN`, `SESSION_AND_INBOX_LIFETIME` and `WAKE_ON_DELIVERY_DESIGN`;
their remaining decisions stay gated. `SUBSCRIPTION_AUTHORIZATION_DESIGN` remains deferred.
**Exit:** operator decisions on the plan's ranked questions, a concrete config/API/authority mapping,
and missed/duplicate/overlap/restart semantics. No new implementation nodes before that decision.

### `SCHEDULED_AGENT_WAKE` — wake a standing agent on a durable schedule

**Unscheduled; implementation blocked on `SCHEDULED_AGENT_WAKE_DESIGN` and operator selection.** Implement the selected
restart-safe scheduling and authorized delivery contract from [persistent agents](persistent_agents.md),
with retained run identities and receipts.
Exercise missed/duplicate delivery, overlap and restart reconciliation proportionally. Reuse
service-owned command delivery and the chosen wake policy; no second command queue. The
`DAILY_CI_AGENT` capstone proves useful agent behavior, while this node owns reusable scheduling.

### `MANAGED_SUBSCRIPTION_PROTECTION` — managed subscriptions the owning Session cannot cancel

**Unscheduled hardening; managed agents do not need it.** Operator, 2026-11-17 14:15 UTC: "yeah,
theoretically A might be nice to have, e.g. having rbacs or whatever to say "this can't be deleted,
flux manages this", but we can do that kind of hardening later/independently." Managed agents use
option B of [persistent agents](persistent_agents.md): the owning Identity may cancel a managed
subscription, and the reconciler restores it. This node adds option A: record the manager on each
managed subscription and inbox, and have `CancelSubscription`, `RenewSubscription` and `RetireInbox`
refuse the owning Identity on those rows while the manager's calls succeed. It needs the reconciler's
Notifications mutation path from `SCHEDULED_AGENT_WAKE`. **Exit:** Notifications tests show the
owner's cancel, renew and retire of a managed row refused with a defined code, and the manager's
accepted.

### `NOTIFICATION_COMMAND_OUTCOME_DESIGN` — notice delivery without reading conversation history

**Unscheduled; operator, 2026-10-11 PDT: not a priority, needs more thought.** The Notification
Service scans a Session's raw history from the History Service for its own command's admission and
`harness_user_message_confirmed`, and stores one `EventEntry` per notice (`Notice.runner_entry`):
the one service besides the History Service fold that derives state from Session events. A command's
`CommandAdmitted` entry says the runner admitted it, but not that the harness executed it or that
the message reached the model's context; today that needs the history. Decide where that
outcome is recorded before the Notification Service stops reading history. If the design still
requires a raw event scan, consider moving it to `FollowSession` while keeping receipt
reconciliation bounded per claim.

### `NOTIFICATION_SERVICE_SPLIT` — split, move or rename the Notification Service's responsibilities

**Unscheduled; operator, 2026-10-17 PDT: some renaming and refactoring around this service at some
point.** The service (`agentplane/notification_service/`) has grown to several responsibilities:

- mailboxes, their high-water marks and the get API;
- listening to webhooks, currently GitHub's;
- subscriptions, by which an inbox gets messages when a listener notices something;
- direct agent-to-inbox messages;
- deciding to send a notice to a Session because its mailbox holds new unacknowledged messages,
  possibly waking it if suspended, possibly with debounce by time or count, or a check of the Session's
  history so no second notice goes out before the Session has started executing tools or reasoning
  since the last notice or acknowledgement.

Decide which stay, which move to other services, and what the remaining service is called. Inbox
messaging v1 shipped in one deployment under the current name; this node is the later restructuring.
Related: whether notice delivery belongs with the Dispatcher
([freezer](task_freezer.md#notification_dispatcher_ownership--review-notification-and-dispatcher-ownership)),
and the history-free notice outcome (`NOTIFICATION_COMMAND_OUTCOME_DESIGN`), which bears on the notice
decision's last clause.

**Operator, 2026-10-21 07:33 UTC: rethink the inbox model and its names together with the rename.**
Noticing agents is only one part of what the service does; it is more a mailbox ("email" or "mailbox")
service. The operator's sketch, explicitly not a committed shape or naming: inboxes contain messages;
a message has a sender, which is another agent or an event source such as GitHub; a message is
structured, like a GitHub webhook, or unstructured text; a subscription connects an inbox to an event
source, with its configuration (which pull requests, for example). Today's terms to reconcile with it
include entries and their event identities (`actions`, `github`, `message`), notices, sources and
`DestinationRef`. The `InboxesPerAccount` quota counts inboxes per execution Identity, so its name goes with this rethink.

**Operator direction, 2026-11-14 01:11–01:12 UTC:** the service "might make good sense to split
into, approximately", "one service/thing that holds inboxes for agents" and "github webhook
processing as one actor that can tell this service 'i have this message and want to add it into
those inboxes'"; and "it's a little bit unclear at this point how the 'subscribe
to x from a source' seam would look; it would need to handle actions and github both". The seam is
the open design: who owns subscription records, who matches source events to subscribers, and who
appends to an inbox with what idempotency key and authority, with Action results and GitHub as the
two required sources. Today GitHub fact writes and inbox wake effects share one SQL transaction; an
actor writing through an inbox API turns that into an idempotent append. The Go plan's
`notification-inbox-split` waits on this seam; its GitHub extractions are cleanups of the current
form, done with Go plan stage 4 on 2026-11-17 (section 17).

**Not released; operator, 2026-11-17 03:59 UTC:** "for now we keep all this in one service, because
i don't quite like the form of this seam yet - but we keep it open for future considerations and if
there's cleanups to apply on the current form, we can do those". The seam sketched that night, and
the `inboxes` rename that went with it, are in
[`notification_service_split.md`](notification_service_split.md). Cleanups of the current form are
released one by one. **Exit:** a reviewed seam covering both sources, the resulting component names,
and the moves it implies as their own nodes.

When revisiting this seam, unify the entry content used by the CLI and the web app. The app's
notification details are a debug view: showing the entry as JSON is sufficient. Today the
`WatchInboxEntries` summary parses stored GitHub content to recover a check or workflow run's
conclusion for display. Keep provider facts typed while the provider needs to interpret them;
once it has a message for zero or more inboxes, the inbox should be able to deliver and show
that message without parsing provider-specific fields.

### `COMMAND_WATCH_SUBSCRIPTION_DESIGN` — a subscription kind that runs an agent's command on a schedule

**Unscheduled; operator, 2026-10-17 PDT.** Its scheduling/inbox seam is folded into
[persistent agents](persistent_agents.md#cron-source-recovery-and-overlapping-work), inside the
single Notification Service. Cron ticks alone run no commands. Before a command-watching source
can be selected, decide execution location and Identity/authority, output-change baseline and
nonzero/run-failure triggers, hung-command limits, minimum interval/quotas, stop condition and
payload shape. `SUBSCRIPTION_AUTHORIZATION_DESIGN` remains deferred; the scheduled-wake proposal
does not authorize command execution or release this source's implementation.

### `SESSION_INPUT_METADATA` — later typed annotations on input commands

**Unscheduled; operator's lean, 2026-10-12 PDT.**
Typed metadata and restricted trusted producer provenance on input commands, so a notice the
Notification Service sends is not rendered as a user message. The field must ride the durable
Session Dispatcher path to the Session owner. For native Sessions the Runner journals it; the
central sampler should similarly pass it through to History (operator, 2026-11-04), without needing an
Environment. Its trusted producer provenance and narrow authorization are open design points. The
harness/model never sees it. Keep that authorization outside the generic command-admission path;
attachments are invalid on controls. No untyped extension dictionary. The native path can land
independently; central support follows the applicable central Session path. See
[metadata ownership](notification_presentation.md#history-and-read-path).

### `CENTRAL_SESSION_INPUT_METADATA` — pass central Session input metadata to History

**Blocked on `SESSION_INPUT_METADATA` and `CENTRAL_SESSION_WITHOUT_ENVIRONMENT`.** The central
sampler passes authorized input metadata through to History alongside the input, with the same
provenance, command-ID correlation and retry/conflict contract as the native path. It must work
without an Environment and keep metadata out of model input. Cover authorization denial, retained
metadata after inbox expiry, duplicate/conflicting submissions and recovery. This is a separate
backend slice; native annotations and same-principal central Session creation need not wait for it.

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
    SUBSCRIPTION_AUTHORIZATION_DESIGN[Deferred: subscription authorization]
    SUBSCRIPTION_AUTHORIZATION[Blocked: enforce reviewed creation and continuing source grants]
    SUBSCRIPTION_AUTHORIZATION_DESIGN --> SUBSCRIPTION_AUTHORIZATION
    GITHUB_MERGEABILITY_FRESHNESS[Ready: current mergeability after base-only changes]
```

### `SUBSCRIPTION_AUTHORIZATION_DESIGN` — auto-allow and operator-approved subscriptions

**Deferred by the operator.** The [persistent-agents proposal](persistent_agents.md#authority-wake-and-lifetime)
identifies managed creation, mutation, renewal and revocation boundaries without selecting their
authorization mechanism. When resumed, compare an Action-backed subscribe operation using existing auto-allow/operator
approval with authorization inside Notification Service; do not preselect another policy engine.
Review which callers may subscribe to which sources, targets, fields/filters, destinations and
lifetimes, and who can approve/delegate that access. Subscription creation approval is distinct
from continuing source access and from ownership of the destination inbox. Define renewal, scope
expansion, policy changes/revocation and what happens to already-retained entries.

Use Kubernetes as a concrete design case: namespace/resource/UID scope, object/status/event/log
content, name reuse and whether grants delegate the caller's existing read authority or explicitly
allow additional observation. A privileged watcher must not expose arbitrary cluster data merely
because the agent owns an inbox. Review argument examples that auto-allow narrow approved scopes,
require operator approval for additional scopes, and reject requests nobody can delegate.
Define immutable normalized approval arguments, stable request keys and idempotent execution/recovery;
waiting for approval must not create a subscription. Decide who administers policies, what the
approver can delegate, and whether continuing authority uses a bounded grant or standing policy.
Cancellation cannot recall retained entries. Avoid per-event approval unless explicitly required.
This is independent of selecting a messaging transport or fixing existing GitHub delivery gaps.

### `SUBSCRIPTION_AUTHORIZATION` — creation gate and continuing enforcement

**Blocked on the authorization decision.** If Actions is selected, implement its subscribe operation
and reviewed policy bindings; otherwise implement the chosen direct policy path. Keep Notification
Service the subscription/delivery authority in either case. Carry verified caller/decision scope
across the service boundary rather than substituting the executor's broad identity. Enforce the
same access contract on direct APIs, renewals and updates; no approval bypass by another endpoint.

Test auto-allow, pending approval, denial, restricted destinations, scope escalation and expiry/
revocation with controlled identities, plus one bounded deployed identity/policy check for the
selected integration. No entry may be delivered beyond the reviewed source scope;
approval is not permission to wake a Sandbox or execute source content. Future Kubernetes sources
must depend on this contract/enforcement when promoted from the freezer. Ordinary authorized
subscription reads/cancellation and existing GitHub gap recovery need not wait for a broad redesign.

### `GITHUB_MERGEABILITY_FRESHNESS` — obtain a current result after base-only changes

**Ready.** The [live evidence](../notification_service/debug/github_mergeability_live_acceptance_20261028.md)
records stale REST positives after base-only changes, while the service's
[proof and retry policies](../notification_service/docs/api.md) keep the source `UNKNOWN`.

**TODO: test whether GraphQL obtains a fresh result sooner than REST.** Use controlled disposable
fixtures, advance only the base, and keep the PR head unchanged throughout polling. Compare
`mergeable`, `potentialMergeCommit` and its parents, and the actual base ref; record settling time
and request cost, including no improvement. Do not push a new head, retarget or reopen the PR, or
compute a merge locally during this comparison. The previous GraphQL read was followed by a head
change, so it did not establish whether GraphQL alone would settle. A negative remains
GitHub-reported without proof of the evaluated pair; neither elapsed time nor current-ref reads
supply that proof.

Exit: establish and validate a supported way to obtain the current result after a base-only change,
with actual refs, request cost and detection timing recorded. If no bounded mechanism is available,
bring the operator an explicit contract decision between GitHub-only observation, local conflict
calculation, or reporting prolonged uncertainty before implementing an alternative. Preserve the
shipped proof/fence/error policies. This does not reopen waived broad outage/restart drills or select
a general CI/comment/review replay mechanism.

## 3. Multiagent decisions before implementations

The caller-privilege delegation contract is approved for implementation. Broader multiagent
possibilities below retain their own decision gates. The selected first slice is caller-privilege
Session creation; it does not wait for the complete multiagent model or
different-privilege launch policy. Broader delegation still needs its identity/authority review.
Do not equate creating a resource, reading history, sending a message or receiving/acking it.

[The cross-agent plan](multiagent_communication.md#tentative-implementation-direction) tentatively
selects service-enforced Session-access `PolicySet` rules assigned through existing `PolicyBinding`s.
Resource-owning backends share evaluation code and enforce their own APIs. Native RBAC variants
remain alternatives if implementation reveals a poor fit; comparing or building them is not a gate.
**Operator clarification, 2026-10-30 PDT:** inbox tags and history tags are the current direction
for cross-ServiceAccount sends and reads, and may be reconsidered. Their presence in implementation
or existing plans does not make them a permanent domain constraint. Use the realistic workflow
examples to evaluate the mechanism; an alternative may require backend work and replanning.
Existing authorization continues to apply until a replacement contract is selected and implemented.
Session–Environment separation also requires revisiting what these groups label and how access
survives attachment changes or Environment deletion. `SESSION_ACCESS_GROUPING_DESIGN` gates new
cross-principal grouping and the proposed Sandbox history record; the Sandbox-scoped design below
is historical direction, not a settled prerequisite. Existing send enforcement remains in force.
SA-level caller attribution, configurable topology and live shared bundles are agreed constraints.
Replacing the authorization machinery across services is a separate deferred evaluation, not a
multiagent prerequisite. Preserve existing auth semantics; a concrete limitation must justify any
separately reviewed broader proposal.
Send and transcript-read permissions are independent in both directions. A2A is open at this
communication boundary; its harness/thread-protocol rejection has a different scope. Transport
facades are not prerequisite decisions, and content classification is outside v1. Exact schemas and
the broader contracts below retain their individual review gates.

```mermaid
flowchart TD
    MULTIAGENT_MODEL[Decision: identities, relationships and native-child boundary]
    THREAD_READ_POLICY_DESIGN[Held decision: remaining history read contract]
    SESSION_ACCESS_GROUPING_DESIGN[Decision: Session access grouping, section 13]
    HISTORY_SANDBOX_RECORD[Conditional: Sandbox history record and tags]
    THREAD_READ_POLICY[Blocked: scoped archive reads]
    HISTORY_SANDBOX_ASSOCIATION_SOURCE[Unscheduled: an authoritative Sandbox UID source for History]
    PLATFORM_GUIDANCE_OWNERSHIP_DESIGN[Later decision: platform-guidance ownership and composition]
    PLATFORM_GUIDANCE_SINGLE_OWNER[Later refactor: one platform-guidance owner]
    PLATFORM_GUIDANCE_OWNERSHIP_DESIGN --> PLATFORM_GUIDANCE_SINGLE_OWNER
    SESSION_TOOL_SURFACE_CONVERGENCE_DESIGN[Direction agreed; interface review open]
    SESSION_TOOL_SURFACE_CONSOLIDATION[Blocked: retire orchestration_v1 onto the backend APIs]
    SESSION_TOOL_SURFACE_CONVERGENCE_DESIGN --> SESSION_TOOL_SURFACE_CONSOLIDATION
    ACTION_PROTO_CONTRACT_DESIGN[Later decision: protobuf wrapper Actions and generated schemas]
    ENVIRONMENT_SHARED_IDENTITY[Blocked: environments sharing an Identity]
    ENVIRONMENT_TEMPLATE_CATALOGUE_DESIGN[Later decision: catalogue ownership and location]
    ENVIRONMENT_MANAGEMENT[Deferred: environment catalogue and creation]
    THREAD_CREATE_AUTHORIZATION[Operator follow-up: usable caller-privilege delegation]
    AGENT_LAUNCH_POLICY_DESIGN[Decision: constrained Sandbox launch and delegation]
    AGENT_SANDBOX_LAUNCH[Blocked: enforce agent Sandbox-launch policy]
    MULTIAGENT_MODEL --> AGENT_LAUNCH_POLICY_DESIGN
    THREAD_READ_POLICY_DESIGN --> THREAD_READ_POLICY
    SESSION_ACCESS_GROUPING_DESIGN --> HISTORY_SANDBOX_RECORD
    SESSION_ACCESS_GROUPING_DESIGN --> THREAD_READ_POLICY
    ENVIRONMENT_TEMPLATE_CATALOGUE_DESIGN --> ENVIRONMENT_MANAGEMENT
    ENVIRONMENT_SHARED_IDENTITY --> ENVIRONMENT_MANAGEMENT
    SCHEDULED_AGENT_WAKE[Unscheduled: durable scheduled wake]
    DAILY_CI_AGENT[Unscheduled capstone: daily CI-maintenance agent]
    SCHEDULED_AGENT_WAKE --> DAILY_CI_AGENT
    AGENT_LAUNCH_POLICY_DESIGN --> AGENT_SANDBOX_LAUNCH
    ORCHESTRATION_V1_BACKEND_GRANTS[Blocked: orchestration_v1 operations on backend grants]
    THREAD_READ_POLICY --> ORCHESTRATION_V1_BACKEND_GRANTS
    AGENT_SANDBOX_LAUNCH --> ORCHESTRATION_V1_BACKEND_GRANTS
    RUNNER_HARNESS_OBJECTS_DESIGN[Decision: process diagnostics and placement]
    HARNESS_IDENTITY_DESIGN[Decision: private store and native Session mapping]
    HARNESS_IDENTITY_DESIGN --> RUNNER_HARNESS_OBJECTS_DESIGN
```

### `MULTIAGENT_MODEL` — shared identity and authority vocabulary

**Decision; no runtime work implied.** Present concrete API examples for independent agents,
Sandboxes and Sessions, with creator, manager and parent/provenance represented separately. Ask
the operator to choose remaining addressing/ownership and delegation semantics. Use the agreed
SA-level caller attribution and the [tentative Session-access rule direction](multiagent_communication.md#tentative-implementation-direction),
without choosing a mandatory topology. No permission inheritance from an organizational edge or
preset. Review how native harness children could later be ingested: linked execution in the same
Sandbox trust boundary versus independently provisioned Sandboxes, addressability, observability
and which controls must remain unavailable. Native integration itself stays frozen, and v1 excludes
native children from the messaging and access model.

This gates broader different-privilege create/delegation and `AGENT_LAUNCH_POLICY_DESIGN`; the
approved caller-privilege delegation slice proceeds with its own contract.
Messaging needs only the
[Session inbox access rule](../notification_service/docs/api.md#messages) and
history reads only their own rule type, so creator, manager and parent semantics do not hold them up. The app's existing auth path may remain a special case for v1. Reducing that
special handling is optional, separately reviewed follow-up work and does not gate multiagent
implementation.

### `THREAD_READ_POLICY_DESIGN` — scoped history read policy

**Decision, held by the operator for now (2026-10-17 UTC): do not raise it until the operator reopens
it. Earlier Sandbox minimum, history tags and all-Sandboxes direction must be revisited with
`SESSION_ACCESS_GROUPING_DESIGN` before selection for independent Sessions.**
Reads get a rule type of their own, not the send rule's (operator, 2026-10-21 UTC), and this decision
shapes it. Finalize caller replacement semantics. Collection reads match history tags on History's
record of the Sandbox, which outlives it ([design](sandbox_tags.md)). V1 has no individual-Session
grants or computed collection selection. Presets seed launch tags; later removals persist. Reading a
Session resolves its recorded Sandbox identity, and all Sessions in that Sandbox use the same scope.
The proposed broader grant should not expose other principals' histories by default. Existing same-principal
reads are already implemented. Session listing is a distinct, weaker read-type permission, independent from send; its metadata fields are selected, and raw history read/follow
behavior remains to design. Read does not imply send/create, and no folded-read API is required for v1.
Cover discovery, linked evidence, live-feed revocation and unknown historical associations. Do not block
this on a messaging transport.

**Question for review:** should an `allSandboxes` grant also reach histories with no recorded Sandbox
UID? Those rows may retain a namespace but cannot match an exact Sandbox incarnation. Existing
same-principal reads use the retained execution principal independently of the Sandbox UID; rows
without a trustworthy principal remain limited to configured backend readers
([History README](../history_service/README.md#session-writer-and-execution-sandbox)).

### `THREAD_READ_POLICY` — authorized retained-history access

**Blocked on the held read policy and `SESSION_ACCESS_GROUPING_DESIGN`.** The proposed
`HISTORY_SANDBOX_RECORD` is required only if the reviewed access design selects it. Same-principal
retained reads already use the exact recorded execution identity. Add reviewed broader grants and
association handling through shared rule evaluation at the owning backend across
list, raw/native evidence, direct reads and feeds, including reconnect and revocation. Keep app-only UI
folds separate. Test allowed/denied histories, two principals, deleted Sandboxes and revocation with
deterministic service tests plus a bounded deployed auth check. Cover a listing-only caller separately
from transcript/evidence reads and follows. Recheck stream authorization/freshness and stop Event
emission on observed grant loss or stale required policy state.

### `HISTORY_SANDBOX_RECORD` — History's Sandbox record and history tags

**Conditional on `SESSION_ACCESS_GROUPING_DESIGN` selecting a Sandbox-scoped record.** #11835
records Sandbox UID, but does not decide access grouping for independent Sessions. The historical
[design](sandbox_tags.md#historys-record-of-a-sandbox) would add a set-typed `spec.historyTags` to `Sandbox`
in the same change, since History is its only reader; app presets supply a launch's initial history tags.
The Role granting History `get`, `list` and `watch` on Sandboxes lands first, then the code. History
keeps one record per Sandbox UID with namespace, name and history tags; a read-only watch writes it and
never deletes it. The History rule in [service boundaries](../docs/service_boundaries.md) and the
[History README](../history_service/README.md) change in the same PR. Test a tag edit reaching the
record, a deleted Sandbox keeping its last tags, a same-name replacement getting its own record, and a
restarted watch catching up.

### `HISTORY_SANDBOX_ASSOCIATION_SOURCE` — an authoritative Sandbox UID source for History

**Unscheduled TODO (operator, #11835).** History resolves a writer's Sandbox UID from the controller
ownerReference on its ServiceAccount ([README](../history_service/README.md#session-writer-and-execution-sandbox)).
Consider whether the Sandbox Controller can expose a cleaner authoritative association instead; keep the
ownerReference until that is reviewed. Related: a ServiceAccount recreated in place under the same Sandbox
keeps failing as a different writer, and letting the Sandbox UID take over as the pin is a separate
decision.

### Approved caller-privilege delegation implementation

**Approved by the operator, 2026-10-31:** [contract and realistic workflows](session_delegation_workflows.md),
tracked in [#13229](https://github.com/agentydragon/ducktape/pull/13229). Normal same-account calls
need no human approval; full template catalogue discovery is unfiltered by SA. The additive
`additional_instructions` brief preserves platform guidance and never starts a turn; callers
submit input separately. New environment creation initially needs human approval.

Prefer existing owning gRPC APIs and Agentplane CLI clients where straightforward. Actions-first
is approved for other paths, with explicit RPC/CLI extraction TODOs in the contract. The app is
never a backend dependency; `orchestration_v1` is not a prerequisite. Keep the same verified caller
and authority enforcement across all surfaces. No zero-environment child or different-privilege
worker is claimed by this slice.

The operator will evaluate natural-agent usability through normal use after activation; it is not
a separate release gate. Configuration checks remain required. Fresh
provisioning and broader authority keep their own decision gates. File conflicts alone do not
serialize independent PRs.

### `PLATFORM_GUIDANCE_OWNERSHIP_DESIGN` — decide platform-guidance ownership and composition

**Later decision; current duplication is accepted.** Design one authoritative owner/source for
platform guidance while allowing placement- and environment-specific variants. Compare ownership and
composition in the Environment Runtime's native harnesses and Central Sampler. Both paths already use
a shared renderer and template; these do not settle which services distribute or compose guidance, or how deployment-
specific content and runtime-specific needs reach each Session. Use the [Session/runtime model](session_runtime_model.md)
to compare concrete native-host and central-Sampler cases with zero, one and multiple attached
environments, including deployment-specific URLs/configuration, placement and capability-specific
guidance, and per-environment instructions.

The review outcome is an ownership and composition/distribution proposal that explains update/version
and failure semantics, and how the default platform guide remains truthful while an additive caller
brief remains separate, without requiring presets. Keep current duplication until that proposal is
reviewed; do not assume native and central runtime needs are interchangeable. This entry selects no
guidance service, new network dependency, Dispatcher ownership, or runtime-unification mechanism.
The [current guidance contract](../session_instructions/README.md) and
[managed rollout](../sampler/docs/rollout_acceptance.md#managed-platform-guidance-rollout-2026-11-02-utc)
remain valid during this later design; it does not block current reliability or delegation work.
Related deferred work: [consolidate agents' access to documentation and system configuration](task_freezer.md#agent_platform_information_design--consolidate-documentation-and-system-configuration-access).

### `PLATFORM_GUIDANCE_SINGLE_OWNER` — consolidate platform-guidance ownership

**Future refactor; blocked on `PLATFORM_GUIDANCE_OWNERSHIP_DESIGN` review.** Put the job of knowing
the platform instructions in one authoritative owner, then migrate Environment Runtime and Central
Sampler consumers to it. Remove independently maintained guidance configuration and selection rules
from those consumers. The existing shared renderer and template do not by themselves complete this refactor. Consumers may
cache or materialize resolved guidance; deployment-specific content, harness differences and
per-environment instructions still need truthful composition under the reviewed design.

Preserve frozen instructions for existing Sessions, additive caller instructions and harness/provider
base prompts. Exit with both native and central workflows using the common owner and the redundant
ownership removed. Owner placement and distribution remain design choices; this item does not
require a new service and does not block current reliability or delegation work.

### `SESSION_TOOL_SURFACE_CONVERGENCE_DESIGN` — review a common Agentplane tool surface

**Direction agreed, 2026-11-14 01:10 UTC.** To the proposal "Make the Dispatcher, EnvironmentService
and History APIs the one model, authorized as the caller's Identity, with the CLI and MCP tools as
thin adapters, and retire orchestration_v1", the operator answered "sounds good to me to show the
actual internal interfaces (assuming the interfaces are sane)". What remains of this decision: the
check that those interfaces are sane (gaps are fixed in the owning API, not an adapter), and how
agents create environments and child Identities once `orchestration_v1`'s direct Kubernetes writes
of Identities and launcher PolicySets go. The Go plan tracks the deletion as
`orchestration-v1-retirement` ([section 17](#17-two-binaries-and-the-go-layout)); it is not released.

**Original request, operator, 2026-11-01 UTC.** As capabilities are implemented, compare the
actual workflows and operations in [`orchestration_v1`](../action_service/orchestration_v1/README.md),
the existing Session CLI, the implemented opt-in Session MCP surface, and the
delegation/environment tools. Do not assume either tool set is a subset of the other. Map launch
and provisioning, model change, listing, history reads,
observation, control and recovery, along with workload/CLI/external-MCP usability. Compare the
operator-broker authority and approval semantics with caller-privilege enforcement; make no
implicit authority translation.

Recommend one common domain tool set where the capability and authority map supports it. Keep
unique operations until a reviewed replacement and migration are verified. The review links the
[approved delegation contract](session_delegation_workflows.md) and
[`ORCHESTRATION_V1_BACKEND_GRANTS`](#orchestration_v1_backend_grants--orchestration_v1-operations-on-backend-grants).
Exit with a capability/authority map, target contract and explicit retain/retire/replace proposal
for stakeholder review. Its implementation is tracked by `SESSION_TOOL_SURFACE_CONSOLIDATION`;
the decision does not block current receiver or tool work.

### `SESSION_TOOL_SURFACE_CONSOLIDATION` — consolidate orchestration and Session delegation

**Operator-requested follow-up, 2026-11-11; blocked on `SESSION_TOOL_SURFACE_CONVERGENCE_DESIGN`'s
remaining review.**
Consolidate the overlapping `orchestration_v1` and `session_delegation` executors and published
Action/MCP tools under the reviewed capability and authority map. Prefer retiring the older
`orchestration_v1` group once its required workflows have replacements. Its operator-broker
Decision authority is distinct from Session delegation's backend-enforced caller authority; shared
code does not make those grants interchangeable.

Preserve or explicitly replace launch/provisioning, model and reasoning-effort changes, Session
listing, retained reads, input observation, turn waiting and cancellation. Migrate configured direct
tools, launch policies/grants, CLI guides and every caller with each retired surface. If the selected
replacement requires broader backend grants, its affected operations wait on
`ORCHESTRATION_V1_BACKEND_GRANTS` and that node's authorization prerequisites; independent shared
helpers and same-caller operations need not wait.

**Exit:** the reviewed surface serves each retained workflow with positive and denied-authority
coverage; redundant executors, contracts, policy shapes and documentation are removed, with no
production caller depending on the retired group. Split implementation into independently reviewable
PRs after the design is selected. This node records the requested consolidation, not authorization
to delete unique operations or change authority before review.

### `ACTION_PROTO_CONTRACT_DESIGN` — protobuf contracts for Action inputs and outputs

**Later decision; operator request, 2026-11-02 UTC.** Consider making Actions thin wrappers around
backend RPCs, using protobuf request and response messages as their input and output contracts and
generating the published Action/MCP schemas from those descriptors. Compare direct protobuf
contracts with generated adapters to remove parallel handwritten schemas. Review concrete examples
covering both inputs and outputs, including field presence, validation and Action-specific
approval/authorization metadata, before selecting a pattern. This does not block current resume
or delegation work.

### `THREAD_CREATE_AUTHORIZATION` — caller-privilege delegation capstone

**Operator normal-use follow-up; no merge or rollout gate and no worker dispatched.** Backend authorization,
Discovery, guidance, Action/MCP tools, retained-result reads and scripted external OAuth/MCP and
parent-in-environment CLI workflows are implemented; those fixtures exercise real interfaces but
prescribe the model's calls. Do not treat scripted acceptance as unaided agent use.

Give an agent an ordinary delegation assignment and verify it discovers the target and tool
schemas, prepares its directory, opens a child, submits separate input, reads a correlated result
and uses follow-up/control operations when needed. Cover existing-environment and external-MCP
entry points; retain actionable failures and lost-reply recovery. The operator may run a prompt
later; reuse that feedback and evidence for `DAILY_CI_AGENT`.
Preserve implemented foreign-principal denial, revocation and no-privileged-fallback coverage;
broader archive and different-SA delegation are not prerequisites. Fresh provisioning remains
deferred until its catalogue and identity/approval prerequisites; it does not hold reuse.

### `DAILY_CI_AGENT` — a daily CI-maintenance agent that can delegate fixes

**Unscheduled product capstone; priority reconsidered by the operator, 2026-11-17 13:12 UTC.** Configure a standing agent to wake
daily, inspect CI latency over the previous 24 hours and open justified, focused fix PRs, or record
that no fix is warranted. Include creating and coordinating a caller-privilege Agentplane Session for a
piece of work. Use existing CI analysis capabilities, source access and permission mechanisms;
this is not a new CI analytics system or a generic agent-role framework.

Completion means an end-to-end useful run from the scheduled wake, with retained run/outcome
evidence and clear operator visibility. Dedicated sampler disruption acceptance is deferred. Confirm
that an ordinary task description suffices for tool discovery and delegation; hand-scripting every
tool call does not prove agent usability. Reuse existing work/PR evidence when deciding whether to
open another fix. Set the concrete schedule, model, credentials and notification destination before
activation; activation is follow-up work after this planning review.

### `ENVIRONMENT_SHARED_IDENTITY` — environments acting as one existing Identity

**Fresh agent provisioning remains deferred.** Several environments naming one `Identity` in
`Sandbox.spec.identity` is the
[identity plan](identity_and_kubernetes_accounts.md#identity-lifetime)'s model; this node finishes
what those two leave keyed to a Sandbox's own account. Borrowing the caller's Identity is a scoped
case; define who may name or delegate one. Coordinate native Session creation and
unbound-before-start creation with `SESSION_IDENTITY_CONTRACT` and
`ENVIRONMENT_IDENTITY_BINDING_DESIGN`. Controller rendering, shared readers, gateway admission,
History association and public projections must agree on the Identity; a reader that still assumes
the account name equals the Sandbox name leaves the contract incomplete, so update every affected
consumer together. Naming an Identity creates no grant or `PolicyBinding` for it, and a shared
Identity's grants apply in every environment acting as it. A recreated Identity is the same named
principal with a new UID; authenticate the current object and fence in-flight work against stale
incarnations. Discovery follows the Sandbox-owned account relation for its bounded same-account
lookup; its list backend must move to the Identity reference while preserving the existing paged
list contract.

### `ENVIRONMENT_TEMPLATE_CATALOGUE_DESIGN` — catalogue ownership and location

**Deferred decision; operator, 2026-10-31.** The first release can reuse existing environments
without exposing creation/templates. Decide where the backend's full published configurations
live, who publishes them, how underlying Kubernetes `SandboxTemplate` objects and app/deployment
launch defaults become one coherent catalogue, and how revisions/snapshots are retained. Compare
Git/deployment-owned publication with a backend-managed catalogue; no app dependency or new
mandatory process is assumed. Full catalogue visibility and pinning approved configuration across
restarts are accepted; storage/ownership and the explicit update interface remain to review.
Revisit when selecting fresh-environment creation, not as a gate on Session delegation.

### `ENVIRONMENT_MANAGEMENT` — dedicated environment tools and creation

**Deferred by the operator, 2026-10-31; template ownership is intentionally postponed.**
When selected, implement full published template discovery and creation/status/recovery as
the caller's `Identity` with a dedicated `environments/create` Action, initially human-approved.
`EnvironmentService` already provides typed create and basic runtime-template listing RPCs, but its
template names are not the full published catalogue. This new workflow requires
`ENVIRONMENT_TEMPLATE_CATALOGUE_DESIGN` and `ENVIRONMENT_SHARED_IDENTITY`; retain the verified
Action caller UID as admission and in-flight fencing evidence, even when a same-name replacement
is the same durable principal. Do not guess an unknown legacy caller's incarnation.
Existing-environment list/get is already available through backend Discovery and does not wait for
fresh provisioning.

Keep the API/executor over neutral Sandbox domain code and make the concrete backend/process
placement part of the catalogue decision. No integration-app dependency. Implement owning gRPC
reads and CLI where straightforward; track any initially Action-only RPC/CLI extraction TODOs.
Catalogue reads are not creation permission.

Accepted future behavior: act as the caller's existing Identity and permissions without owning it or adding
grants, and pin the exact approved configuration across restarts. Materialize that snapshot or
refuse before effects. Bind creation UUID to actual caller UID and exact request, separately from
the Action key, and reconcile unknown outcomes without replay. Creation returns a Sandbox UID,
not a Session or readiness promise. Other lifecycle and different-privilege launch remain separate.

### `AGENT_LAUNCH_POLICY_DESIGN` — Sandbox launch and delegation policy

**Later decision for broader/different-privilege launch.** Review which callers can use which templates and resource budgets, mounts, images,
secrets, egress and Kubernetes/Action grants; presets are defaults, not authority. A new Sandbox is
required for an independent isolation boundary; adding a Session within one is not. Define manager,
lifetime, parent termination, quota/fanout and revocation without automatic privilege inheritance.
Choose policy ownership and audit records before adding tables. State separately whether the
operation also opens a session; if so, depend on `THREAD_CREATE_AUTHORIZATION` for that composition.

**Operator input, 2026-10-21 UTC (not a decision):** "when a SA makes a sandbox, it should be able to send
it messages, at least when it requests that (if not by default)". A hand-written rule cannot name a
Sandbox that does not exist yet, so the launch would create the grant: a PolicySet and PolicyBinding
owned by the new Sandbox, whose `session_inbox_access` rule has a UID-pinned direct target, deleted with it.
An explicit launch option keeps "launch alone does not grant messaging"; whether it is the default is
the operator's to choose. Stamping an inbox tag at launch, which presets already do, is the
alternative. Spend limits: see the [approved scope boundary](session_delegation_workflows.md#budget-and-broader-policy-boundaries).

**Operator input, 2026-10-27 UTC (not a decision):** decide "how to specify what's the set of things that an agent
may allow its launched sandboxes to do": the templates, PolicySets and tags a launcher may give the workers it starts,
and how many it may run at once. In `orchestration_v1` the operator approves every launch, so the launch card is the
bound. Nothing is inherited, which suggests a list of the launcher's own rather than a subset of its grants, so an
orchestrator without GitHub access could still start a worker holding `common-github-read`; Kubernetes RBAC's `bind`
verb has this shape, and its counterpart here would grant attaching named PolicySets.

### `AGENT_SANDBOX_LAUNCH` — constrained agent-requested launch

**Blocked on launch policy.** Enforce the reviewed
effective spec and delegation server-side. Test allowed and forbidden launch parameters and
concurrent retries. Reuse the EnvironmentService create path and shared inventory. Launch alone
does not grant history read, messaging, credentials or execution of another principal's Actions.

### `ORCHESTRATION_V1_BACKEND_GRANTS` — `orchestration_v1` operations on backend grants

**Blocked; one operation at a time as each backend rule lands.** The
[`orchestration_v1` group](../action_service/orchestration_v1/README.md) runs every operation as the operator's act,
with the Action Service trusted by the Dispatcher and History ([ADR](../docs/adr_orchestration_v1.md)). Move each to
a grant its backend checks: Session creation and commands after `THREAD_CREATE_AUTHORIZATION` and
`DISPATCHER_SEND_AUTHORIZATION_GENERALIZE`, Session reads after `THREAD_READ_POLICY`, launch after
`AGENT_SANDBOX_LAUNCH`. The executor then calls as the caller, launcher grants write the backend rule instead of an
`argument_schema` auto-approval ([target shape](../docs/adr_orchestration_v1.md#why-shape-matching-is-a-stopgap)), and
`send_message` joins the group. Exit: the Action Service is in neither the Dispatcher's callers and wake callers nor
History's readers, and no launcher grant matches argument shapes. The original grant sketch targeted
Sandboxes with a ServiceAccount subject; reconcile it with `SESSION_ACCESS_GROUPING_DESIGN` and
`DELIVERY_DESTINATION_AUTHORITY_DESIGN` before applying it to Environment-independent Sessions; the
Session permission target and principal representation remain open.

### `RUNNER_HARNESS_OBJECTS_DESIGN` — decide process diagnostics and placement policy

**Placement decision remains open; lifecycle ownership approved (operator, 2026-10-17).** The runner
owns harness processes. Provisional process and Session diagnostics, including a frontend Sandbox-page
view, are selected in the [lifecycle contract](../docs/runner_delivery_and_harness_design.md#runtime-lifecycle); their exact shape may change. No public process
kill/restart API is introduced. Decide placement/store policy separately; expose a store API only if
its public need is selected.
Use the merged Codex multi-thread test and Claude's separate-store single-root characterization
alongside `HARNESS_IDENTITY_DESIGN` and the [pinned child evidence](../native/docs/child_sessions.md); do
not assume one Agentplane Session is one process, one persisted store, or one native conversation, or
that every discovered child is independently controllable. Native delegation with root-only output is
merged; child Session projection remains
[deferred](task_freezer.md#native_subagent_threads--as-linked-agentplane-threads).

Refine process diagnostics and per-Session projections as needed for introspection. Process placement
and allocation remain internal, without a public create-Session-on-process API or refusal. Specify
which options and status belong to the process incarnation, persisted store and public Session;
include concurrency, cancellation and failure/restart behavior.
Compare two coherent alternatives: one fixed harness type and persisted store per Runner, with the
process lifecycle coupled to that Runner; or multiple processes and stores per Runner, each
potentially serving multiple Sessions without an arbitrary same-type restriction. Do not retain the
hybrid of one process at a time switching among per-Session stores: it retains store routing while
also forbidding mixed harnesses. A fixed store resolves Session-to-store ambiguity but raises shared
store isolation and concurrency questions and sacrifices mixed Codex/Claude Sessions in one
Sandbox/Runner. Once support for N>1 processes/stores exists, a same-type-only rule adds little
architectural simplification; resource limits and proven harness capabilities still constrain actual
use. Restricting to one later is easier than adding multi-harness identities and routing later; treat
that as a modest migration-flexibility advantage, not a decision.
Use measured interactive-multiplexing and native-child evidence; read-only/discoverable children do
not prove independent interactive control. Reclaiming or replacing a process must account for every
hosted Session; interrupting one turn must not affect another Session.
Codex already runs one app-server and store per Runner, and Claude one process per Session
([harness process design](../docs/runner_delivery_and_harness_design.md#harness-process-store-and-session-identity));
the recommendation covers what remains open around them.

Central Codex hosting through Sandbox exec servers is the opt-in
[central sampler](../sampler/README.md), which coexists with local modes. It does not gate
the current placement decision or separate tool memory containment.

Deliver a recommendation and proposed API shape for review; implementation and migration/retirement
nodes follow only if selected. This does not unfreeze the broader runner durability redesign or
authorize native-subagent implementation.

## 4. VM environment phases

The [VM Sandboxes design](../docs/vm_sandboxes.md) holds the provider constraints, and the
[KubeVirt plan](kubevirt_environments.md) the unresolved platform constraints. Selectable VM-backed
Sandboxes, typed resource budgets, isolated Codex tools and the raw-block workspace are shipped;
[component evidence](../docs/vm_sandboxes.md#fresh-vm-trial-readiness) records their acceptance.
The remaining nodes do not gate the operator-authorized fresh VM trial. This is an unranked candidate
lane, not authorization for local Bazel in current containers. The
[runner control](../docs/runner_control.md) defines the private gRPC contract VM control networking
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
    ENVIRONMENT_SUSPEND_MODES_DESIGN[Decision: distinct suspend and resume modes]
    FIRECRACKER_INTEGRATION_DESIGN[Decision: Firecracker integration and privilege boundary]
    FIRECRACKER_INTEGRATION_DESIGN -. related lifecycle contract .-> ENVIRONMENT_SUSPEND_MODES_DESIGN
    VM_RAM_SUSPEND[Unscheduled: suspend a VM to disk with its RAM]
    ENVIRONMENT_SUSPEND_MODES_DESIGN --> VM_RAM_SUSPEND
    VM_CONSOLE_DEFERRED_ATTACH[Unscheduled: attach the console when a starting VM becomes attachable]
    VM_INTRA_SANDBOX_HARDENING[Deferred: optional intra-Sandbox process/state hardening]
    RUNNER_LAYERING_VM[Paused: slim VM runtime, section 7]
    STATE_VOLUME_BLOCK -. related .-> VM_RAM_SUSPEND
    LOCAL_BAZEL[Unscheduled: bounded local Bazel client in VM]
```

### `FIRECRACKER_INTEGRATION_DESIGN` — choose Firecracker integration and privilege boundary

**Decision; unscheduled; operator request, 2026-11-20.** Decide how Agentplane should run and
manage Firecracker Sandboxes in the cluster, including whether to build on an existing integration.
The current [Firecracker backend](../docs/firecracker_sandboxes.md) runs one launcher Pod per guest;
its network setup needs privileges that staging's baseline Pod Security policy rejects. KubeVirt
already uses privileged infrastructure while its launcher Pods can be unprivileged. Do not assume a
Talos API-server exemption or a new controller is the only solution.

Compare at least these patterns and their costs:

- Keep our launcher, with launcher Pods in a dedicated privileged infrastructure namespace. A
  controller's namespace does not exempt Pods created in another namespace; decide whether the
  existing controller can own this or a separate controller provides a useful authority boundary.
  Work through cross-namespace PVCs, Secrets, identity, network policy, ownership and cleanup;
  Pods cannot directly mount another namespace's PVCs or Secrets, and namespaced owner references
  cannot cross namespaces.
- Keep launcher Pods in their current namespace, using a narrowly constrained admission exception;
  assess the scope and operational cost of API-server exemptions and replacement policy, including
  the proposal in [#18428](https://github.com/agentydragon/ducktape/pull/18428).
- Follow the KubeVirt pattern: a trusted node-side helper or networking integration performs the
  privileged setup and fencing while the launcher has reduced privileges. Account for device
  access, TAP and firewall lifecycle, the helper's authority, and the complexity of an extra
  node service. Consider other container runtime or Kubernetes integration patterns as well.
- Evaluate existing systems, including [OpenSandbox Fast Sandbox](https://github.com/opensandbox-group/fast-sandbox)
  and its [checkpoint design](https://github.com/opensandbox-group/OpenSandbox/blob/main/docs/architecture/fast-sandbox/checkpoints.md),
  as well as [Kata](https://github.com/kata-containers/kata-containers),
  [Cocoon](https://github.com/cocoonstack/cocoon-operator),
  [Setec](https://github.com/zeroroot-ai/setec), and
  [SPACE](https://github.com/perplexityai/SPACE). Check maintenance and actual implemented behavior,
  not just advertised features; consider a bounded Fast Sandbox trial if it can resolve material
  uncertainties. Distinguish an integration usable as-is, an adaptable component, and an
  architectural reference. [firecracker-containerd](https://github.com/firecracker-microvm/firecracker-containerd)
  is archived; recheck the state of each candidate when investigating.

First candidate for a bounded comparison: Fast Sandbox's
[Firecracker pool](https://github.com/opensandbox-group/fast-sandbox/blob/main/config/samples/pool-firecracker.yaml)
uses privileged Fastlet Pods serving multiple guests and a node runtime agent. Its
[checkpoint format](https://github.com/opensandbox-group/OpenSandbox/blob/main/docs/architecture/fast-sandbox/checkpoints.md)
covers rootfs, vmstate and RAM. Establish whether it can boot our guest, preserve our separate
state/workspace volumes and re-establish guest identity and relay-only egress on restore. Compare
who can create privileged Pods and access another guest's storage or credentials in that model
and in a dedicated namespace for our launcher. A disposable KVM test can answer those questions
before changing staging admission.

Compare security and control-plane trust, node changes, scheduling and KVM, operational and
maintenance burden, resource accounting, workspace and persistent storage, guest networking and
egress, and rollout and cleanup. For suspend/resume, compare process and RAM preservation,
checkpoint storage and consistency, Pod/node loss, identity and credentials, clock repair,
network/admission fencing and recovery from interrupted operations. The current same-Pod behavior
is a baseline, not a requirement to retain its limitations; identify any contract changes and
their migration. Coordinate with `ENVIRONMENT_SUSPEND_MODES_DESIGN`, `VM_RAM_SUSPEND`, and
`SANDBOX_QUIESCENCE_DESIGN` without making their independent work wait on this investigation.

**Rollout state:** the staging preset and image pins are in source
([#18421](https://github.com/agentydragon/ducktape/pull/18421)). That PR recorded a server-side dry
run that rejected the privileged launcher under `baseline:latest`. The scoped admission proposal
[#18428](https://github.com/agentydragon/ducktape/pull/18428) closed without merging. The
[Firecracker KVM suite](../system_tests/firecracker/README.md) exercises the launcher with the
Debian test guest. The staging admission and published NixOS guest still need a canary under the
selected integration.

**Exit:** a reviewed comparison and recommendation, with a bounded experiment where needed,
explicit authority and lifecycle boundaries, security tradeoffs and unresolved gaps. Select and
track implementation and rollout after review. The rollout must exercise the admission boundary
and the published guest on staging, including the checkpoint, credential and cleanup checks in
[Firecracker validation](../docs/firecracker_sandboxes.md#validation-boundaries).

### `VM_INTRA_SANDBOX_HARDENING` — optional process and state hardening inside one Sandbox

**Deferred; nonblocking.** The accepted boundary is the Sandbox, so co-resident Sessions share its
filesystem, credentials and process trust domain. The VM Runner runs as guest root, native harnesses
run as uid/gid 107, and virtiofs presents `/state` files as 107:107; the Runner does not create a
separate mount or PID namespace for each Session. The existing per-harness cgroup groups a native
harness with its tool children for resource limits and teardown, not as a security boundary. Do not
require Runner-private storage, sibling Session stores, or harness/tool process views to be isolated
inside one Sandbox. Revisit Runner-state privacy and tool-child-versus-harness hardening separately
only if a concrete product requirement or incident warrants it. This node does not
gate VM resource acceptance or lifecycle work.

### `ENVIRONMENT_SUSPEND_MODES_DESIGN` — distinguish Environment suspend operations

**Decision; operator direction, 2026-11-04.** Design distinct suspend/resume operations for a
cold stop such as removing the Pod or VM and a memory-preserving VM suspend. Decide their API and
status, capabilities and unsupported-mode behavior, selection and authorization, idle/wake policy,
quiesce and journal boundaries, fencing, recovery and failure behavior. Specify what can be proved
about processes, terminals, memory, workspace, control connections and credentials after each mode;
never claim a resource survived or was lost without evidence. Coordinate with `VM_RAM_SUSPEND`,
`AGENT_RESUME_NOTICE` and the [control ADR](../docs/adr_sandbox_session_control.md#d3-quiescing-is-one-operation-names-say-which-stage-a-sandbox-is-in).
Existing cold-stop behavior and bounded notices about it can proceed independently. A
memory-preserving mode must not reuse the current final seal contract without resolving how the
running harness and journal resume safely.

**Exit:** reviewed cold-stop and memory-preserving examples, observable outcomes and ownership,
including failed/interrupted suspend and reconnect. This decision alone selects no VM mechanism.

### `VM_RAM_SUSPEND` — suspend a VM to disk with its RAM

**Unscheduled; blocked on `ENVIRONMENT_SUSPEND_MODES_DESIGN`; operator, 2026-10-13 PDT: nice to have.** A stopped VM Sandbox resumes with the memory
it had, not a cold boot, so a quick resume loses no in-memory state. Find what KubeVirt offers (a
memory dump or a snapshot with memory) and whether it works with the state volume (virtiofs today, block under `STATE_VOLUME_BLOCK`). The
[VM Sandboxes design](../docs/vm_sandboxes.md#guest-and-storage) records that a VM supports neither
RAM suspension nor live migration today; the opt-in [Firecracker backend](../docs/firecracker_sandboxes.md)
preserves RAM within its retained launcher Pod. Check what a resumed guest must redo: the runner's control stream and
projected tokens (the relay reopens them per request, so rotation alone needs no restart), and the
clock. Settle first what "seal before suspend" means when the harness keeps running across the suspend
(the current quiesce stops harnesses and seals journals). Where storage or the guest cannot carry it, stop/start from the state PVC stays the
lifecycle.

### `VM_CONSOLE_DEFERRED_ATTACH` — attach after VM startup makes the console available

**Unscheduled; lower priority, operator report 2026-10-15 PDT.** During VM startup the console tab
can show `Kubernetes answered 400` and remain disconnected. Treat a not-yet-attachable VM as waiting
and attach when its lifecycle signals readiness, without reopening the tab. Distinguish startup
from authorization failure, deleted VM, and another attached client; clean up on tab exit and never
open competing connections. Keep the existing read-only/no-replay contract. Test the unavailable
→ attachable transition and cancellation; do not add a periodic polling loop or bigger timeout.

Control design: [ADR](../docs/adr_sandbox_session_control.md).

### `LOCAL_BAZEL` — bounded local client in a VM

**Unscheduled.** The deployed template already provides 8 GiB, one vCPU and explicit byte
budgets. Wire Bazelisk, storage and authenticated RBE/cache/BES; bound the client's CPU/memory/disk
usage and cleanup. Inspect actual launcher requests/limits, namespace usage and eligible-node
capacity, including KubeVirt overhead. Run a representative build/test with the client inside the
VM and actions remote, without guest OOM; check cancellation/cleanup while the harness stays
responsive. Record useful sizing evidence in VM documentation. Installed tools and working hosted
builds do not authorize local Bazel in agent containers. Further guest slimming is independent of
this acceptance.

## 5. Independent UI and narrowly scoped service work

These candidates need no multiagent or VM decision. Any implementation that turns out to require
app/Sandbox Service database changes requires its own migration safety review.

```mermaid
flowchart LR
    CLAUDE_CODE_WEB_BRIDGE[Unscheduled: a frontend for Claude Code web sessions]
    SESSION_LAUNCH_FRICTION[Alongside: fewer clicks on reused environments]
    VM_STARTUP_LATENCY[Later: measure production VM startup]
    ACTION_SURFACES_DESIGN[Decision: where and in what states Actions render]
    EGRESS_RELAY_RENAME[Deferred: rename egress sidecar to relay]
    EGRESS_CREDENTIAL_TRANSPORT_DESIGN[Decision: credential upstream schemes and ports]
    RUNNER_HARNESS_DELIVERY_DESIGN[Decision: decoupled harness delivery]
    K3S_BUNDLED_SERVICES_SPIKE[Unscheduled: measure bundled control-plane services]
    KUBERNETES_DECOUPLING_DESIGN[Decision, unscheduled: how far to decouple from Kubernetes]
    KUBERNETES_DECOUPLING_DESIGN -. related test-weight options .-> K3S_BUNDLED_SERVICES_SPIKE
    DOCKABLE_PANES[Unscheduled: dockable panes in the web app]
    ACTION_SURFACES_DESIGN -. related .-> DOCKABLE_PANES
    NOTIFICATION_PRESENTATION[Blocked: compact notification rendering, section 2]
    ACTION_SURFACES_DESIGN -. related .-> NOTIFICATION_PRESENTATION
    BUILD_BOX_BAZEL[Unscheduled: build Sandboxes can run Bazel]
    PLATFORM_SERVICE_NAMES_DESIGN[Decision, unscheduled: reserved sidecar names for platform services]
    PLATFORM_SERVICE_ROUTING_DESIGN[Decision, unscheduled: direct platform access vs central egress]
    PLATFORM_SERVICE_NAMES_DESIGN -. related naming and transport choices .-> PLATFORM_SERVICE_ROUTING_DESIGN
    SESSION_RUNTIME_MODEL_DESIGN[Decision, unscheduled: Sessions, harness placement and execution attachments]
    AGENT_AUTHORED_CONVERSATIONS_DESIGN[Decision, unscheduled: conversations agents act on through tools]
    STRUCTURED_TOOL_OUTPUT[Unscheduled: tool results as ordered text and image parts]
    STRUCTURED_TOOL_OUTPUT -. multi-block output .-> CODEX_CODE_MODE_CELLS[Unscheduled: Codex code-mode cells as runner items]
    CENTRAL_SAMPLER_MODEL_CAPABILITIES[Unscheduled: live route check for standard-mode routes]
    CENTRAL_SAMPLER_PROMPT_COMPACTION[Unscheduled: client-side compaction prompt for routes without server compaction]
    CENTRAL_SAMPLER_COMPACTION_HEADROOM[Unscheduled: compact long tool loops before output starvation]
    TABBY_REASONING_REPLAY[Verify Tabby reasoning replay after proxy rollout]
    CENTRAL_SAMPLER_COMPACTION_TIMELINE[Unscheduled: surface sampler compaction in the timeline]
    CENTRAL_SAMPLER_BACKGROUND_COMPACTION[Unscheduled: compact a prefix while sampling continues]
    CENTRAL_SAMPLER_DRAIN_BUDGET[Unscheduled: consider longer sampling drain]
    CENTRAL_SAMPLER_DYNAMIC_TOOLS[Deferred: dynamic tool definitions when needed]
    CODE_MODE_HOST_DESIGN[Decision, unscheduled: keep Codex's code-mode host or own one]
    SESSION_SETUP_FAILURE_CAUSE[Unscheduled: show why a Session's setup failed]
    CLAUDE_REMOTE_EXECUTION_SPIKE[In review: gated native Claude remote tools]
    CLAUDE_CUSTOM_SAMPLER[In review: custom Claude sampler evidence]
```

### `SESSION_SETUP_FAILURE_CAUSE` — show why a Session's setup failed

**Unscheduled to-do; the operator, 2026-11-17 07:20 UTC:** "yeah, do add a todo that it would be nice
to have this fixed." That morning five Codex Sessions on one shared worker Sandbox failed setup and
never ran a turn. Their launcher saw only launch outcome FAILED and the Runtime's refusal "session
setup failed or was interrupted; create a new session" (`env/runtime/session/session.go`).

**Observed cause: storage exhaustion; node remains unreleased.** The operator, 2026-11-17
13:04 UTC: "i think the setup failure was most likely from a full disk". Runtime journals for all
five failed Sessions on `codex-workers` confirm `SetupFinished` exit `128` with `ENOSPC`
("No space left on device") during `git clone --depth 1 --branch devel` of the repo:
`2a52f30b`, `af580cbf` and `c2914da1` failed in checkout at 06:35 UTC; `a5a49b01` and
`cfd63a3b` failed in fetch/`index-pack` at 06:55 UTC. Workspace PVC samples at five-minute
resolution peaked near 94% bytes / 92% inodes; the volume filled between samples. The journals
cannot distinguish exhausted bytes from inodes. Workspaces are never removed when a Session ends.

- Setup output is journaled (`SetupOutput`, `SetupFinished{exit_code}`), but `get_session` and the
  refusal do not surface it, and `SetupInterrupted` carries no error: `runSetup` only logs it
  (`env/runtime/session/setup.go`). Carry the exit status or interruption error, with the output's
  tail, to the Session's status and to the error a refused command returns.
- A Sandbox admits new Sessions whatever room its workspace volume has left, and each Session's
  setup can clone a full checkout. Decide whether that needs a guard (refuse or warn near capacity),
  or whether the visible cause is enough.

**Exit:** a Session whose setup fails because its volume is full says so in `get_session` and the
App, without reading Runtime logs or metrics.

### `SESSION_LAUNCH_FRICTION` — fewer clicks to start useful work

**Selected alongside P0/P1; operator, 2026-10-30 PDT.** Make creating a Session on an existing
environment with its own working directory a short path with useful defaults. Preserve explicit
harness/model/access choices without requiring users to reconstruct ordinary settings each time.
Separate this flow from provisioning a fresh environment. Review the actual click path and
candidate UX first; do not require new agent/conversation entities to improve it.

Exit: the agreed common path needs fewer decisions/clicks and creates a useful Session in the chosen
directory, with intended defaults and override behavior visible. Include the normal visual review.

### `VM_STARTUP_LATENCY` — measure production time to a useful fresh VM Session

**Later; low priority, not wanted soon (operator, 2026-11-15 UTC); selected next on 2026-10-30
PDT.** First measure one ordinary staging `environment-vm` start using
the production NixOS guest on a KVM-schedulable node. The [prior-work audit](../debug/vm_startup_latency/2026_11_01_vm_startup_latency.md)
records the boot, image, and runner-disk improvements already shipped, and why the recent Debian/QEMU
system-test timings do not measure this product path. The staging [fresh VM acceptance](../docs/vm_sandboxes.md#fresh-vm-trial-readiness)
currently records functional readiness but no durations.

Record the exact guest image digest, node, storage class, and naturally observed image/cache state.
Capture a warm follow-up on the same node/image if available; do not evict caches or discard state to
manufacture a cold start. Correlate retained Kubernetes object timestamps/events and launcher, guest,
Runner, and Session evidence across Sandbox request, PVC binding, VM/VMI scheduling and start, guest
boot, Runner Ready/bootstrap, and first safe useful Session action. If an essential phase is missing,
add only its smallest timestamp at the owning boundary. Keep test-fixture spans separate.

**Exit:** retain a phase timeline that identifies the critical path and states whether a product
bottleneck is demonstrated. If it is, propose one bounded change at the owning boundary, naming the
mechanism and preserving KVM scheduling, storage/state ownership, and Runner/bootstrap/readiness
behavior; include matched same-image validation of the affected phase and first useful work. If no
phase dominates, or a naturally cold image state was unavailable, record that limit and stop without
choosing an image/layout redesign. Keep reusable environments supported. `RUNNER_LAYERING_VM` remains
paused and is not a prerequisite; `LOCAL_BAZEL` independently proves the VM-based coding workflow.

### `SESSION_RUNTIME_MODEL_DESIGN` — Sessions, harness placement and execution attachments

**Decision, unscheduled; operator requested future design work, 2026-10-28 PDT.**
The newer [Session/environment model and discussion](session_environment_model.md) records selected
direction and remaining decisions; current opening and recovery behavior is in the
[Dispatcher SPEC](../session_dispatcher/dispatcher/SPEC.md), [Runtime SPEC](../env/runtime/SPEC.md) and
[central sampler SPEC](../sampler/SPEC.md). The earlier descriptions below are context where superseded by that direction.
The [current component boundaries](session_runtime_model.md#role-boundaries-and-routing) inform this
broader object/API decision without making it a prerequisite for selected execution or delegation work.
Session/Environment dispatch roles are
distinct, but their physical merge/split is deferred. [Design brief](session_runtime_model.md).
Represent Sandbox-local native harnesses/state and central Sessions whose optional Sandboxes supply
execution in one shared domain vocabulary. A native harness's required local environment is a
capability constraint of that harness, not a universal Session attachment requirement. Keep
Session identity, durable harness stores, running hosts, placement and execution attachments distinct.
The landed [SessionSpec placement contract](../session_dispatcher/dispatcher/SPEC.md#session-binding)
makes the owner variant explicit: `environment_hosted` carries the exact native environment and
harness, and `central_sampler_v1` carries one exact required executor environment. #14314 retains
the Dispatcher Session row when that required Environment is deleted, but central V1 still has no
optional attachments, environment-free admission or alternate executor. The principal and
harness-store contracts remain unresolved.
The [Dispatcher outcome contract](../session_dispatcher/dispatcher/SPEC.md#opening-outcomes-and-dependent-commands)
also leaves unconfirmed native owner admission explicit: `UNKNOWN` outcomes hold dependent queued
commands without forwarding or waking, and those commands remain cancellable. Automatic resolution
is not provided; decide whether an authorized caller or operator can resolve the hold from durable
owner evidence, without replaying uncertain native effects or discarding their outcomes.
Preserve harness-specific constraints: Claude
Code naturally runs in the environment it acts in; a shared vocabulary must not pretend every
backend supports independent placement or executor replacement.

Use the current environment `CreateSession` as a concrete design case: it reserves a Session, while
setup and native harness or process startup may happen later, and the relationship between the RPC
result and observable harness readiness/failure needs a clearer model. Decide how Session identity
and reservation, harness placement, environment attachment, process start and status reporting fit
together for both a Session independent of environments and a harness running inside one. Specify
what each operation acknowledges and how callers observe progress or failure, without assuming every
harness supports the same placement or creation order.

Selected: distinguish Session identity/continuation authority, harness placement and execution
attachments; Dispatcher PostgreSQL owns the Session directory, with History stored separately. Prefer
external placement when capabilities are otherwise equal, for isolation and independent lifetimes.
Characterize complete per-feature placement; OpenClaw's historical partial remote support is a
counterexample to equating remote shell forwarding with a fully separated harness.
Support Sandbox-first native CLI use and Session-first central use with 0..N environments and later
attachment/detachment. Preserve central Session identity and continuation data after executor
deletion; retained Dispatcher identity alone does not make V1 environment-independent. The 2026-10-30
discussion adds durable harness stores versus running hosts, dispatcher/gateway role boundaries,
caller-SA provisioning, agent-facing environment context and unresolved skills delivery, programmatic
environment access, partial-output failures and recovery after JS host loss. Resource visibility,
rightsizing and Claude workload placement are independent follow-ups in the brief. Decide concrete
capabilities, lifecycle, authority and attribution; reuse the existing versioned Codex environment
API evidence without claiming unsupported modes work.
Operations on an existing Session should address the Session, without making callers choose or supply an
environment just to send, observe or otherwise act on it. Resolve placement at the responsible
boundary only when an operation actually needs it; environment lifecycle and execution operations
still address their environment. Preserve backend-specific native placement constraints, explicit
attachment authority and exact-incarnation fences.
Exit: reviewed vocabulary and lifecycle/API examples that represent both a CLI inside its required
environment and a central sampler with zero, one or multiple attached environments, including
shared-host/multiple-Session operation, environment loss and host restart. Include a data-preserving
transition proposal. Recording the broader model does not dispatch a worker or authorize a live migration.

### `AGENT_AUTHORED_CONVERSATIONS_DESIGN` — conversations agents act on through tools

**Decision, unscheduled; operator requested for later, 2026-10-30 PDT.** Explore a conversation
that agents participate in through tools such as `send_message` and `edit_message`. Its messages
need not mirror model context items or expose each reasoning/tool step as a work log. A Session
could participate in such a thread alongside humans or other agents; a human participant is optional.
Expose conversation operations through MCP so third-party harnesses can participate without changes
to their binaries. Treat publishing a message, notifying a participant and waking an agent as
separate concerns; incoming-message delivery and harness-specific notification support need design. The object is named a Thread ([naming ADR](../docs/adr_journal_and_thread.md)).

The [completed CLI experiment](../debug/authored_conversation/README.md) supplies interaction
evidence for this review. Its original Qwen configuration missed publications after reset and
correction. Later focused probes with an explanatory prompt improved publication on Qwen and
Luna. The [CLI Thread and Trace views](../x/authored_conversation/README.md) already separate
explicit posts and edits from model text, tool activity and diagnostic JSON.

**Operator direction, Nov 22.** Today the app folds Session History into a conversation-like view.
The proposed messenger displays messages and revisions authored by the human and agent on a
distinct Thread. The diagnostic view shows what the harness or model did, including tool calls and
context items where captured. Give the two views independent entry points. A trace entry alone
does not publish a message. Use the [hosted spike](thread_spike.md) to test whether users can
follow real work through the messenger and still inspect History when a response goes missing.
Prefer additive per-Session opt-in at startup, with a durable attachment to a Thread. Compare
Agentplane tools with central sampler tools for reading, posting and editing. Exercise an enabled
and a default Session side by side, including reconnect. The default Session continues through
today’s History workflow. Selection of a tool path, changing the default UI and rewriting the
journal format are separate decisions.

**Operator direction, not a release (2026-11-17 13:21 UTC, among "things that are on top of my head
right now as stuff i'd like to pursue sometime"):** "[...] together with "make a conversation-style
object that the user and the llm are both operating on with operations like 'edit message', 'send
message' etc, deliver notifiations of changes about it through inbox, give agent a bunch of prompts
to that effect like we did in that small existing python spike in x/, have that be the primary
interface or one of them". we could probably spike that with some feature flags attached. i think
the "truthful model" would be to make "conversation" or "thread" or something objects which have
their own ids, sessions can be attached to them as participants, potentially in the future threads
can have more participants, user is a participant. and i might test-drive that by having a view i
could open in frontend showing the attached thread(s). we could probably build it additively with
feature flags adding it and if it seems to be working ok enough we could demote "history" from the
main conversation-style interface of a session to just the diagnostic log that you go to for
debugging or if you for some relatively unusual reason want to talk to the agent directly raw.
assuming that it actually turns out agents can operate well in this mode." The object is a Thread
under the naming ADR.

**Decided, operator, 2026-11-17 13:29 UTC:** "i agree, i think Notifications would probably make
sense as a natural home at least initially". The spike's scope is in
[`thread_spike.md`](thread_spike.md); building it is not released.

**Operator clarification, 2026-11-12:** authored threads are optional. Background agents can use
other tools, inputs and outputs without a user-facing conversation object; the app can expose their
relatively raw transcripts/logs. Do not make this component a prerequisite for running a Session.

Recommendation for review: distinguish the authored conversation, harness continuation state and
retained evidence of harness activity. Today's History conversation view is already a lossy
projection and cannot resume a rollout; central-sampler evidence and native app-server observations
also arrive at different levels of detail. Consider adding authored conversations and participant
relationships alongside existing projected threads, with reasoning, tool calls and raw harness
evidence accessible through a debug view. Keep the ability to retain harness evidence. Evaluate
OTel/Langfuse as a possible destination, including fidelity and retention; do not assume ordinary
telemetry replaces durable evidence or continuation state.

**TODO (operator, 2026-11-12):** Follow the
[storage representation and compression plan](storage_representations.md), including shared content
across sampler request/response logs, History's native/semantic events and projected bodies.
Co-sequence this storage work with the authored-conversation design: keep divergent harness activity
as less user-visible diagnostic logs rather than forcing it into one conversation representation,
and consider an explicit thread with the user that the agent communicates through. Decide content
ownership/references, authorization, retention and replay/continuation requirements together before
removing copies; this TODO selects neither a shared storage schema nor a trace destination.

Exit: reviewed conversation/participant/message semantics, send/edit authorization and attribution,
edit history and delivery/retry behavior, and an incremental migration/UX proposal. The spike puts
the new entities in Notifications; whether they stay there is decided after it. Storage beyond the
spike and the trace destination remain open. This design does not block current thread work or
authorize implementation.

### `STRUCTURED_TOOL_OUTPUT` — tool results as ordered text and image parts

**Unscheduled; operator, 2026-10-21 07:11 UTC: "fidelity improvements like images for Claude let's put on task
dag".** Tool results become ordered parts (text, image) in the harness-neutral vocabulary instead of one string.
Today `event.proto` carries `ToolResult.output`, `ToolOutputDelta.text` and `RecoveredContent.output` as strings,
the Claude adapter's `resultText()` (`env/runtime/internal/harnesses/claude/protocol.go`) joins text blocks and drops images, and the
History fold's `THREAD_PAYLOAD_FIELD_OUTPUT` is one text field that deltas append to. Scope: the proto, both
runner adapters, the History fold with its storage and reads, the app API, the frontend tool cards, and the fold,
span and visual fixtures. Archived event logs and History rows hold `output` strings and sealed archives cannot be rewritten; the
default under "deployed state is disposable" is to drop that data in its own data-deletion PR, which is the
operator's call. Exit: an image a Claude tool returns renders in its tool card.

### `CODEX_CODE_MODE_CELLS` — Codex code-mode cells as runner items

**Unscheduled; operator, 2026-10-21 07:11 UTC: "It would be nice if we could get the code mode also out of codex
app server for fidelity but I'm guessing that might not be trivial".** Surface Codex code-mode `exec` cells (the
script and what it emitted) as runner items. Codex app-server has no `ThreadItem` for them, in 0.157.0 or on
`openai/codex` main at `c7234c11` (2026-10-21), and core emits the call output only to hooks, so the runner sees
only the nested `commandExecution`. Needs upstream support, or reading the rollout or the native frames. Multi-block
cell output likely needs `STRUCTURED_TOOL_OUTPUT`.

### `CENTRAL_SAMPLER_STREAM_LAG` — let the journal keep up with buffered provider events

**Open staging bug, 2026-11-23; no prerequisites.** The sampler took seconds to project deltas
already in its input buffer. Across two Responses models, median journal completion lagged provider
completion by 47–82 seconds. See the [measurements and fix candidates](../sampler/docs/stream_latency.md).
Interrupt polling and export checkpointing held conversation locks while stream writers waited.
Compare narrower locks, fewer round trips and bounded batching. Exit when an identical
buffered-stream replay and a bounded staging check show reduced wire-to-projection and terminal lag
without regressing first-delta latency, ordered replay or rejection of a stale writer.

### `CENTRAL_SAMPLER_DRAIN_BUDGET` — consider a longer sampling drain

**Unscheduled; operator requested tracking, 2026-11-21 PDT.** Consider increasing the sampler's
40-second shutdown drain to let more in-flight model responses finish during rolls, weighing
sampling durations against rollout latency. A staging roll interrupted a streaming response in
[this Session](https://agentplane-staging.allegedly.works/#/threads/de396206-d814-4443-8978-34bfbdd242ae)
when the drain deadline expired. Any increase also needs compatible shutdown cleanup budget and Pod
termination grace; it cannot guarantee every response completes.

### `CENTRAL_SAMPLER_MODEL_CAPABILITIES` — live route check for standard-mode routes

**Unscheduled; operator requested 2026-10-29 PDT after a concrete provider rejection.** The route
`ollama/oai-chat/qwen3.8-flash-next-iq4xs-256k` returned HTTP 400 through LiteLLM:
`input[0]: unknown input item type: "additional_tools"`. Each route now declares its Responses
dialect and exec encoding; standard mode sends top-level function tools and rejects compaction
([SPEC](../sampler/SPEC.md)). Remaining:

- A bounded check against the reported route establishing its actual accepted wire contract; the
  standard/JSON path is covered only by deterministic fixtures.
- Compaction for standard-mode routes: `CENTRAL_SAMPLER_PROMPT_COMPACTION`.

Do not silently strip required tool behavior, retry arbitrary HTTP 400s with progressively different
payloads, or replay already dispatched effects. Independent of the [deployed Anthropic adapter](../sampler/docs/anthropic_acceptance.md);
no implementation or rollout is scheduled by this entry.

### `CENTRAL_SAMPLER_PROMPT_COMPACTION` — extend sampler-managed compaction

**Unscheduled; operator requested tracking, 2026-10-31 PDT.** The sampler now supports
[configurable prompt compaction for Chat Completions](../sampler/SPEC.md#model-changes).
Responses Lite and Anthropic Messages can use provider-native compaction when configured.
Standard Responses and Messages routes without native compaction still need a sampler-managed
option. Extend the Chat Completions design by route, retaining complete recent turns and their
tool exchanges, and reuse the existing compaction intent, journaling and context-replacement path.
Compare cache reuse, latency and summary quality if offering it alongside provider-native
compaction. **Exit:** deterministic request, context-replacement and failed-summary coverage, plus
a bounded continuation with a tool call on a route without native compaction.

### `CENTRAL_SAMPLER_COMPACTION_HEADROOM` — keep room for reasoning and tool calls

**Unscheduled; Nov 24 staging regression.** A Chat Completions session exceeded its 192K
compaction threshold and reached 261,181 input tokens without recording a compaction intent.
At all 59 sampling boundaries with a known previous usage above threshold, the sampler saw one
live Code Mode cell, at least three user messages and no retry. The live-cell guard suppressed
compaction while sampling continued. #19867 removed that guard and exercises a yielded cell and
its in-flight nested tool across compaction in the [runtime recovery test](../sampler/internal/residentruntime/provider_compaction_recovery_test.go).

Chat now replaces earlier tool exchanges with a handoff and retains recent authored user messages
under a token budget (#19865). [Sampler contract](../sampler/SPEC.md)

Measure whether the current policy leaves room for both the summary and the next ordinary
completion. Repeating a nearly full prompt can exhaust even a bounded retry budget. Compare the
current replacement with a last-N-item tail that keeps a parallel tool-call batch and its results
together. For that alternative, evaluate omitting historical reasoning from the copied tail and
preserve the current task across a cut inside a user turn. Compare with
[Aider's token-budgeted tail](https://github.com/Aider-AI/aider/blob/main/aider/history.py)
and another open-source tool-using harness on the same workload.

**Exit:** stress one user turn with hundreds of tool exchanges, oversized reasoning and results,
multiple compactions, a live cell and a failed summary. Measure actual rendered input tokens,
available output tokens and whether the agent resumes the original task without losing a result
or repeating an effect. Verify historical reasoning at the tokenizer on the deployed model route
with a paired prompt-token measurement; include both a tool follow-up and a new user turn.

### `TABBY_REASONING_REPLAY` — preserve assistant reasoning on ordinary Tabby requests

**Post-rollout check; Nov 24 staging evidence.** The sampler recorded 18,416 characters of assistant
reasoning in a tool exchange, but Tabby's next prompt grew by only 99 tokens. Another exchange
added 10,283 reasoning characters and 93 tokens. Both Tabby aliases used LiteLLM's
[`hosted_vllm` adapter](https://github.com/BerriAI/litellm/blob/v1.103.0/litellm/llms/hosted_vllm/chat/transformation.py#L161-L174),
which removes `reasoning_content` from each assistant message before sending the request.
Qwen's template cannot replay a field that never reaches Tabby.

The generic OpenAI adapter [passed a direct replay probe](../../cluster/cdk8s/litellm/README.md#tabbyapi-reasoning-replay)
on the abliterated checkpoint. After rollout, repeat the paired prompt-token check through the
public model aliases, including the stock checkpoint. Verify streamed tool use and the
placement of a mid-conversation system message. Repeat through LiteLLM’s Responses and Anthropic
Messages bridges with their native reasoning items; the old stock route accepted both but
showed no prompt-token increase. Budget for the larger prompts; any omission at compaction is a separate policy.

### `CENTRAL_SAMPLER_COMPACTION_TIMELINE` — show when the sampler compacts context

**Unscheduled.** Render sampler compaction as a timeline activity with start and terminal
outcome. It can use the tool-row presentation, but must not count as a model-issued tool call.
Verify successful and failed compaction in both the live stream and reloaded History.

### `CENTRAL_SAMPLER_BACKGROUND_COMPACTION` — summarize while sampling continues

**Unscheduled; operator requested tracking, 2026-10-30 PDT.** Ordinary Anthropic compaction is
[implemented and deployed](../sampler/docs/anthropic_acceptance.md). [Design and acceptance](../sampler/plans/anthropic.md#background-compaction).
Add an opt-in flag for eager compaction on Anthropic and Chat Completions routes. At an early
context-usage threshold, send a compaction request for a recorded prefix while the agent continues
ordinary turns. When the summary is ready and usage reaches a later threshold, atomically replace
only that prefix at a provider-request boundary. Keep the entire suffix, including messages added
between taking the snapshot and applying the summary. Give the background request its own pending
state and reject stale results after context or ownership changes. Exit: deterministic concurrent-turn, suffix-preservation, restart and failure-isolation
coverage, plus one bounded direct real-model continuation from the background summary. No worker
or priority is assigned, and this follow-up does not gate ordinary Anthropic compaction.

### `CENTRAL_SAMPLER_DYNAMIC_TOOLS` — change tool definitions during a Session

**Deferred; operator requested DAG tracking, 2026-10-30 PDT.** Revisit when a concrete Actions/MCP
workflow needs tools or schemas to change mid-Session. The
[fixed-tool Anthropic adapter](../sampler/docs/anthropic_acceptance.md) is implemented and deployed.
[Component design](../sampler/plans/anthropic.md#dynamic-tool-definitions) distinguishes
provider-visible inline tools from nested code-mode tools and the existing Actions-as-tools work.
Exit when selected: tool additions/removals reach model input and runtime consistently, retain the
cached prefix where supported, survive restart, and preserve execution authorization plus
thinking/compaction validity. No worker or priority is assigned.

### `CODE_MODE_HOST_DESIGN` — keep Codex's code-mode host or own one

**Decision, unscheduled; operator, 2026-11-14 07:44 UTC, on [Sandbox-free central
Sessions](#sandbox-free-central-sessions):** "which might or might not mean replacing codex's v8
isolate; not sure". Each sampler replica runs every code-mode cell in a fresh V8 isolate inside one
shared, pinned upstream Codex JavaScript host process
([SPEC](../sampler/SPEC.md#runtime-and-effects)). That host runs in the sampler Pod, not in a
Sandbox. A Session with no Environment can use the existing host for
[code-mode Actions](../sampler/SPEC.md#action-tools). Agent-authored JavaScript from every Identity's Sessions already shares that one
process per replica, so a Sandbox-free Session adds no new tenant to it.

Reasons to own the host, from the [runtime model](session_runtime_model.md) and
[delegation](session_delegation_workflows.md) plans: typed `delegate/response` errors (the pinned
host passes primitive strings), serializable environment handles, and explicit cancellation and
waits. The pinned host already exposes the generated Action catalog. What the pinned host provides today and an
owned one would have to keep: `notify`, `store`/`load`, yield and the host-loss recovery contract. The
Go plan keeps the sampler's `codemode` package as one owner until this decides. **Exit:** keep or
replace, decided against a concrete Sandbox-free workflow that needs the difference.

### `CLAUDE_REMOTE_EXECUTION_SPIKE` — gated native Claude remote tools

**In review; [PR #12210](https://github.com/agentydragon/ducktape/pull/12210), 2026-10-27.** The question
is the specifically gated native remote-tools feature, its locally settable versus account gates,
transport and instruction/read-state behavior. Generic filesystem MCP feasibility is already known
and is not the acceptance outcome. Preserve exact binary versions and captures proving whether a
remote read makes root/nested `CLAUDE.md` appear in the next model request; file access alone does
not establish that. Exit: reproducible native-feature evidence or blocker and recommendation.
Independent of the Codex v1 implementation; no live account changes or runtime cutover authorized.

### `CLAUDE_CUSTOM_SAMPLER` — custom Claude loop and shared code-mode compatibility

**In review; [PR #12211](https://github.com/agentydragon/ducktape/pull/12211), 2026-10-27.** Record
scripted custom sampler evidence and CLIProxyAPI/CLI-shaped compatibility limits. A future Claude
backend could use the same central code-mode runtime with suitable prompting and tool contracts;
that is a later decision, not established by wire cloaking. Preserve wrapped official harnesses as
an independent transitional backend. History must retain backend-specific evidence and expose a
purpose-specific common display fold without inventing details absent from a harness JSON API.

### `PLATFORM_SERVICE_NAMES_DESIGN` — stable sidecar names for platform services

**Decision, unscheduled; operator idea, 2026-10-16 PDT.** Evaluate reserved names such as
`gateway-control.agentplane`, `history-ingress.agentplane`, `notifications.agentplane` and
`actions.agentplane` that map to deployment-configured service endpoints. Decide where the mapping
lives alongside `PLATFORM_SERVICE_ROUTING_DESIGN`; stable names need not select a central or local route.
Cover platform services an agent can access, including discovery of their APIs and effective access;
distinguish runner-internal interfaces from agent-facing interfaces. Keep namespace, cluster domain
and ports out of client configuration. Decide whether these are proxy-only authorities or resolvable
names usable by direct HTTP/gRPC clients; account for proxy exclusions, Pod/VM networking,
TLS authority if applicable, and the
overlap of `gateway.agentplane` with Kubernetes Service/namespace DNS shorthand.

Use exact deployment-owned mappings, coupled to endpoint, audience/token projection and
network-policy wiring; `cluster/cdk8s/agentplane/platform_routes.py` owns today's direct routes.
Preserve independent backend authorization and audience-specific identity. Naming alone grants no
access; any change to a service's egress-policy or credential requirements belongs to the routing
decision below, not an incidental alias change.
Unknown reserved names and unavailable routes fail closed without ordinary
DNS/public-egress fallback. No general wildcard-to-cluster-service rewrite is implied.

Exit: a reviewed naming and transport contract with concrete Gateway-control, History-ingress,
Notifications and Actions client examples, plus a bounded implementation/acceptance scope covering
routing, service discovery, effective access, injected identity, denials and supported Pod/VM clients.
This node authorizes design work; runtime changes wait for
the transport decision. No prerequisites on the other section 5 candidates.

### `EGRESS_CREDENTIAL_TRANSPORT_DESIGN` — credential upstream schemes and ports

**Decision; operator requested the audit/proposal, 2026-10-19.** Review the source inventory and
alternatives in [the component proposal](../egress/plans/credential_transport.md). Select where
substituted credentials may travel: external HTTPS/port restrictions and explicit internal
HTTP/h2c exceptions. Keep private-address permission separate from plaintext permission.

Exit: a reviewed transport-grant model and exception set, with bounded implementation/rollout
acceptance. This node authorizes design only; it does not change enforcement or select a platform
routing topology. `PLATFORM_SERVICE_ROUTING_DESIGN` remains a separate decision.

### `PLATFORM_SERVICE_ROUTING_DESIGN` — direct platform access versus central egress

**Decision, unscheduled; operator discussion, 2026-10-16 PDT.** Compare routing agent-accessible
Agentplane services through the central egress proxy with direct local-relay routes or direct
authenticated clients. Include Actions and Notifications as well as runner Gateway control and
History ingress. Gateway/ingress previously used central egress; existing proxy coverage includes
interactive plaintext bidirectional gRPC with metadata substitution. Protocol alone does not require
their current special routes. Naming and API discovery can be designed independently; settle the
combined transport contract before implementation.

The central proxy keeps real substituted credentials outside the Sandbox/Pod/VM and outside what
its ServiceAccount can access, records request/decision evidence, and applies Agentplane egress
policy. Agentplane's own services instead authenticate the agent as itself: distinguish that workload
identity from delegated external credentials the agent must never possess. Backend authorization
remains required on every route.

Arguments for direct platform access: these services are naturally agent-facing, their caller's own
identity may be acceptable for the agent to hold, and required runner control/history delivery would
not depend on central-proxy availability or egress-policy readiness. A local relay could attach
identity without requiring `Bearer <placeholder>`; weigh that small convenience against an asymmetric
client contract that hides authentication. Stable reserved names help prompts and instructions under
either routing choice.

Costs of direct relay routes: another route/configuration surface to distribute and update across
Pod and VM runners, and another place that attaches workload authentication to requests. The minimal
relay already authenticates its hop to central egress, but per-service forwarding expands its role.
Compare generated startup configuration with mounted ConfigMaps or another existing distribution
mechanism; specify update/reload behavior, rollout scope and failure behavior. Central routing
consolidates policy, substitution and decision logging, but adds those dependencies to platform calls.
Decide what request/decision visibility and access controls direct routes need and who owns them.

Direct routes also widen the destinations the workload can reach directly: Pod network policy cannot
distinguish the relay from its agent container, so allowing the relay to reach a backend also permits
the agent to attempt that connection. Backend token validation still gates access. Weigh the marginal
exposure against the central path: a stolen token could also be presented through central egress
where the caller's host/method/path policy admits the request and no substitution replaces it.
Direct access bypasses that egress gate and its decision logging; it does not itself grant the
backend identity or authority. Assess this separately from whether the agent may hold its own token.

Evaluate making central egress optional for deployments that need neither external credential
substitution nor Agentplane egress filtering: external networking may be independently disabled, or
the operator may accept unrestricted access and agent-held credentials. State which functions and
diagnostics are absent in that mode and how platform identity, authorization and discovery still work.
Absence of the proxy alone does not disable external networking.

Exit: a reviewed routing/dependency and identity contract, including whether central egress is optional,
configuration lifecycle, and concrete client examples coordinated with `PLATFORM_SERVICE_NAMES_DESIGN`.
Define bounded acceptance for actual runner control and History push/reconnect, agent calls to Actions
and Notifications, authorization denials and the selected proxy-unavailable/absent behavior. No runtime
change or removal of existing policy checks is selected by this node.

### `ACTION_SURFACES_DESIGN` — where and in what states Actions render

**Decision; operator, 2026-10-13 PDT: rethink it, since it is a patchwork and possibly redundant.**
Decide every place an Action is shown and in which states, then reduce the code to that. Surfaces
found in `app/frontend/actions/` and the service worker: the sidebar rows (collapsed and expanded,
with card decision buttons only where the Action type's `offersCardDecisionButtons` says so), the top "Actions waiting" strip (a count),
the Actions page queue and the detail view (`PendingActionCard`, the full `ActionCall`, with a raw
and a pretty toggle), history, the policy view, and the Web Push notification (formatters in a
registry separate from the React one). Several of them choose independently what to render, whether
to offer a decision, and how tall to let the card grow. Settle which surfaces exist, what each is
for (a glance, a quick decision, full review, a record), and which states each has, so that one
rule decides inline approval and height and each Action type is described once.
Whether a card offers its decision buttons is decided by the Action type's own
`offersCardDecisionButtons(args)` for each invocation, from its content (for example a read-only call
against a listed resource), not by a per-kind allowlist. The names **compact card** (the glance form in
the sidebar and the strip), **card decision buttons** (Approve/Deny on a compact card) and **attention
strip** (the top "Actions waiting" count) are already used in code and tests. Still to settle with the
design: **full card** (the detail view with the whole `ActionCall`) and **notification buttons**
(Approve/Deny on a Web Push notification), then use every name everywhere (UI copy, docs), since "quick
approve" could mean the buttons on a card or on a desktop notification. `NOTIFICATION_PRESENTATION` (section 2) renders notices on the same surfaces and uses the same card
names.
Known inconsistency, left as is (operator, 2026-10-24: acceptable for now, handling still to be
thought through): notification buttons ignore `offersCardDecisionButtons`. The service worker offers
Approve/Deny whenever exactly one Action waits, whatever its arguments, so a notification can approve
a call (a long shell script, a large file push) that the app offers only on the full card.

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

### K3s startup experiment constraints

The independent candidates below were requested by the operator on 2026-10-16 PDT; none waits
for the others. Use equivalent source/scenarios and report transfer, import/unpack, process
readiness and total test time separately in
[the k3s stack timings](../system_tests/k3s/debug/stack_timings.md).
Preserve assertions and timeouts. Language rewrites require Rai's decision;
rank them by unique image bytes removed and startup time on the dependency chain. Remaining
Python services still need the shared interpreter.

Keep the fixture declarative: supply images and manifests, let Kubernetes reconcile, wait
for readiness, then test. Startup dependency ordering belongs to Kubernetes; do not add
PostgreSQL-first sequencing or a custom parallel import/unpack scheduler to the test harness.

### `EGRESS_RELAY_RENAME` — rename the egress sidecar to relay

**Deferred pending operator approval.** The [relay migration plan](egress_relay_rename.md) owns
the image, settings and template rollout gates. R0 starts with a fresh inventory of live and
retained relay consumers.

### `RUNNER_HARNESS_DELIVERY_DESIGN` — choose how runners obtain harness binaries

**Decision; operator, 2026-10-16 PDT: investigate for k3s test latency.** Decide how a runner obtains
a needed harness independently of its image: digest-pinned downloads from an independent
Agentplane service or directly from OpenAI/Anthropic distribution endpoints, or separately pinned
harness OCI images made available through mounts or an init container copying into a shared volume.
Operator preference: eventual delivery should decouple harnesses from the runner image; the
narrower test images are an interim optimization. Decide delivery and service/cache ownership. Any serving service must
respect the backend dependency rule; the integration app cannot own this path.
Whatever delivers Codex must keep the version the model-catalog generator pins at least as new as the runner's
([ingress README](../llm_ingress/README.md#codex-model-catalog)).

The [image-split comparison](https://github.com/agentydragon/ducktape/blob/7fd9f16f7d/agentplane/x/k3s_env/README.md#harness-specific-runner-comparison-2026-10-16)
measures removal of unused harness payload; it does not measure runtime delivery of a needed harness.
Its profiles and inventories are in [#10813](https://github.com/agentydragon/ducktape/pull/10813); the image split itself landed in #10812.

Exit: a measured recommendation and explicit operator choice. Compare equivalent scenarios and
pins, separating image build/upload, action-input transfer, containerd import/unpack, harness
fetch/cache hits and first usable Session. Include cold runs: moving bytes from an image to a
service may only move the same transfer later, and fetching upstream still costs network and
extraction time. Specify version/digest verification, atomic installation, concurrent requests,
cache scope, unavailable-artifact behavior and allowed egress for any runtime-delivery proposal.
No new artifact service or runtime-download implementation is selected by this node.

### `K3S_BUNDLED_SERVICES_SPIKE` — measure a less isolated control plane in real k3s

**Unscheduled spike; operator, 2026-10-15 PDT.** Evaluate running Agentplane control-plane
services together in one Pod to reduce integration-test startup time. Keep real k3s, RBAC
objects, service APIs, databases, controllers and separately managed Sandbox workloads;
exercise the existing end-to-end lifecycle scenarios. Preserve each service's intended
Kubernetes identity and permissions using separate credentials rather than the Pod's union
of service permissions. Explicitly assess how to provide those credentials: a Pod has one
ServiceAccount, so bundling must not silently bypass authorization coverage.

Compare the normal deployment layout with the bundled layout on equivalent inputs and
scenarios. Measure image import/unpack, startup/readiness and total test time separately;
bundling alone does not remove image bytes or process initialization. Record implementation
complexity and coverage lost through shared networking, scheduling, restart and failure
boundaries, including NetworkPolicy isolation. Deliver a measured recommendation to adopt
or discard the reduced mode, with normal-layout coverage retained for those boundaries.
Keep the expensive workflow manual-only; this spike does not authorize a production topology
change, removal of existing coverage or a language rewrite. Keep one documented invocation path
per supported test tier.

### `KUBERNETES_DECOUPLING_DESIGN` — how far to decouple from Kubernetes

**Decision, unscheduled; operator request, 2026-11-14 UTC.** The operator asked for a plan on making
Agentplane more decoupleable from Kubernetes, for example for lighter integration tests, with Flux
still configuring it through something like Crossplane, and with environments that execute commands
without Kubernetes; he is unsure it is a good idea. [The plan](kubernetes_decoupling.md) inventories
the coupling and compares an API-server-only test tier (O1), removing incidental coupling behind
existing service APIs (O2), an environment backend without Kubernetes (O3) and moving the record
store ([D4](architecture_overview.md#four-directions), O4).

**Decision.** Which payoff warrants investment: cheaper tests, environments outside Kubernetes,
or owning the Identity lifecycle through an Agentplane API? The operator named Identity again on
Nov 22 as a candidate if the track is selected. On Nov 26, working through
[Identity ownership](#identity_ownership_design--who-owns-an-identity-and-what-binds-to-it), the operator called
Identity "a pretty clear case": its delete and Session-Sandbox invariants span Sessions, which
must stay in a database. A proposed test for other kinds, not yet confirmed by the operator: move one when an invariant
spans it and database-held state; keep it in Kubernetes when it is mainly desired state for
something Kubernetes runs. Sandboxes are unproven under that test; the trace should check whether
a Sandbox's Identity binding can move while the rest of the CR stays. `docs/principles.md`'s
"Kubernetes is a shared baseline" predates this and needs revisiting with the chosen direction. Today it is a Kubernetes CR. Start with a trace
of one Identity from creation through delegation, same-name replacement, revocation and cleanup.
Compare keeping the CR behind PolicyService with moving the authoritative record behind that API
and reconciling Flux declarations. Price the gain and the authorization, outage and migration
cost before choosing. The [Identity proposal](kubernetes_decoupling.md#o4-move-the-store)
records both options. Zero-Environment Sessions can proceed on the current store.

### `BUILD_BOX_BAZEL` — build Sandboxes can run Bazel

**Unscheduled; held by the [BuildBuddy auth policy](../docs/buildbuddy_remote_auth.md): no local Bazel client in an agent container until agents have VM-backed environments, which is `LOCAL_BAZEL`'s outcome.** Not the same outcome as `LOCAL_BAZEL`: build Sandboxes are Pods today, so a candidate route is letting Action-service Sandboxes be VM-backed (operator, 2026-10-18), after which `LOCAL_BAZEL`'s client would serve them. A build Sandbox cannot run Bazel here: the repo's configuration uses `remote.buildbuddy.io`,
whose gRPC dial ignores the box's HTTP proxy and times out, and it needs credentials. `agentplane/` has no
`go.mod`, so a plain `go build` is not available either. Options: a local-only Bazel configuration for boxes,
or a BuildBuddy route through the box's egress (the Go egress for boxes exists). The key can stay in the
egress proxy as a header substitution, as the staging `claude-ai` Sandboxes' `BUILDBUDDY_POLICY` already does for
`bazel --config=rbe`; whether boxes get that binding is the operator's call.

## 6. Existing access and lifecycle follow-ups

### Egress implementation follow-ups

Unscheduled, each its own change: idle tunnel timeouts and metrics.

These are bounded existing work, not dependencies on the broader multiagent model. Confirm current
source/deployment state with the relevant owner when dispatching; old acceptance notes are not live
observations. New database work follows the task-specific review and rollout requirements.

```mermaid
flowchart LR
    CLAUDE_AI_SA[Decision: review caller authority] --> MANAGED_SA_RBAC[Blocked: account-owned grants]
    AGENT_PERMISSION_REQUESTS[Blocked: an Action type and approval view for permission requests]
    ACTION_SURFACES_DESIGN[Decision: where and in what states Actions render, section 5]
    ACTION_SURFACES_DESIGN --> AGENT_PERMISSION_REQUESTS
    SANDBOX_RBAC[Deferred: bounded deployed grant checks]
    NOTIFICATION_INGRESS_BOUNDARY[Deferred: public-route security check]
    PC_KUBERNETES_POLICY_SET[Unscheduled: public-coder's Kubernetes access as one role set]
```

### `PC_KUBERNETES_POLICY_SET` — public-coder's Kubernetes access as one role set

**Unscheduled; operator, 2026-10-12 PDT: wants it eventually.** The grant and role-set reconciler landed in #15076. Move
public-coder-agent's whole set of RoleBindings and ClusterRoleBindings (logs, pods, node and
cluster-metadata reads, VolSync status and the rest, in `cluster/cdk8s/public_coder/app.py` and its
entries in `cluster/cdk8s/agent_access_profiles.py`) into one `KubernetesRoleSet` with a Flux-owned
`ServiceAccountGrant` on its ServiceAccount
([Policy Controller SPEC](../policy_controller/SPEC.md#serviceaccount-grants)).
A grant names a ServiceAccount only, so the grants that name the `haku:access-profile:public-coder`
Group either move to the agent's ServiceAccount or stay plain Flux RoleBindings.

### `CLAUDE_AI_SA` — review the external caller's actual authority

**Decision.** Review the claude.ai account's Connection and arbitrary-shell Sandbox privileges,
Kubernetes scope and egress/Action grants. Record intended authority in generated configuration,
not ad hoc grants. Test an allowed and denied operation; do not add privileges as part of this review.

### `AGENT_PERMISSION_REQUESTS` — an Action type and approval view for permission requests

**Blocked on `ACTION_SURFACES_DESIGN`; operator, 2026-10-13 PDT:
eventually.** A specific Action type for the request instead of the retired Kubernetes MCP grants
escape hatch, with an approval view that shows the operator the account, the sets and the duration
rather than a blob of Kubernetes YAML. It shares with the app's sandbox-page grants the one path
that writes a `PolicyBinding` for the operator (PolicyService grant, #14796), so an approved request creates
the same kind of binding. How the request renders is the Action surfaces' decision: the approval
view is one Action type described once there.

The operator also wants an agent to be able to request extra permissions without requiring a
per-Session ServiceAccount. Existing guidance uses an approved Kubernetes Action to create a
temporary PolicyBinding for the caller's current SA; approval, policy convergence and effective
access are distinct. A dedicated request should make the target SA, PolicySets, duration and shared
scope clear: every workload using that SA can acquire its grants. The same mechanism can grant an
existing EgressCredential through an egress PolicySet, including the shared GitHub bot credential.
Kubernetes access beyond the caller's own account is the identity plan's mechanism, decided
2026-11-05: an approved request creates an expiring `ServiceAccountGrant` on a shared account or a
`ManagedServiceAccount` owned by the identity, used through the proxy's impersonation source.

### `MANAGED_SA_RBAC` — grants for accounts without a Sandbox

**Blocked on the caller review** (the grant reconciler landed in #15076). The claude.ai caller's Kubernetes access
becomes a Flux-owned `ServiceAccountGrant` on its ServiceAccount, referencing the
`KubernetesRoleSet`s `CLAUDE_AI_SA` approves, replacing its remaining static per-namespace grants
in `agent_access_profiles.py` (`STATIC_GRANTS["claude-ai"]`; the acceptance-testing RBAC already
moved to a `PolicyBinding` in #10522). Its egress and Action rules stay a Flux-owned `PolicyBinding`.
Static Group subjects (Haku OIDC; public-coder until `PC_KUBERNETES_POLICY_SET`) stay Flux
RoleBindings. Test an allowed and a denied request. Static caller accounts' grants and bindings are
Flux-owned, never Sandbox-owned.

### `SANDBOX_RBAC` — bounded deployed grant checks

**Deferred by the operator; not dispatchable until selected.** Inspect the managed-Sandbox grant path as `PolicySet`s leave it
and verify intended versus unrelated scope, including Role edits and credential-use boundaries,
without recording credentials. Use [agent RBAC](../../cluster/docs/agent_rbac.md) and existing tests;
only unresolved deployed wiring needs a live check, not a repeat of every already-covered operation.
What no acceptance test covers yet, to check with the affected accounts' own credentials and not a production
account: two Sandboxes sharing a set and a third with a one-off grant, adding and removing a namespace in the
set, widening and narrowing a referenced Role, and revoking the one-off and the shared binding independently;
deleting a Sandbox, recreating a ServiceAccount of the same name and restarting the controller; and a grant
that fails partway, which shows in the binding's `KubernetesReady` condition while the rest still applies.

### `NOTIFICATION_INGRESS_BOUNDARY` — bounded public-route security check

**Deferred by the operator; not dispatchable until selected.** Existing tests
cover invalid signatures. Close the remaining route-wiring uncertainty with an unsigned webhook
request and a private-API request through the public hostname: both must be refused, without
creating subscriptions or inbox entries. Record the result once; no exhaustive provider matrix.

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
    DECLARATIVE_SESSIONS_DESIGN[Design folded into persistent agents]
    SUBSCRIPTION_AUTHORIZATION_DESIGN[Deferred subscription authorization, section 2] -. related authority design .-> DECLARATIVE_SESSIONS_DESIGN
    STATE_VOLUME_BLOCK[Deferred: block storage for Pod and VM state and workspace]
    NODE_LOCAL_BLOCK_STORAGE[Unscheduled: node-local block volumes on every node]
    VOLUME_LAYOUT_CONVERSION[Unscheduled: explicit offline volume conversion, much later]
    SANDBOX_SPEC_EDIT[Unscheduled: edit a suspended Sandbox's template or kind]
    WORKSPACE_NODE_PORTABILITY[Unscheduled: move a workspace between nodes]
    SANDBOX_AUTODISKFUL[Decision, unscheduled: auto-diskful storage for Sandboxes]
    SANDBOX_ARCHIVE[Unscheduled: a suspended Sandbox archived to object storage]
    AGENT_RESUME_NOTICE[Unscheduled: tell agents when their Sandbox was suspended and resumed]
    ENVIRONMENT_READINESS_WAIT[Unscheduled: bounded CLI and Action/MCP readiness waits]
    AGENT_EXECUTOR_LOSS_NOTICE[Unscheduled: tell agents when executor restarts or kills lose their processes]
    CENTRAL_EXECUTOR_LIFECYCLE_DESIGN[Decision: central Session execution and Sandbox lifecycle]
    SANDBOX_QUIESCENCE_DESIGN[Decision: Sandbox stop, Session settlement and unused-Sandbox deletion]
    WEDGED_SESSION_AND_SANDBOX_RECOVERY[Open bug: recover a wedged Session and Sandbox]
    SESSION_ARCHIVE_CONTRACT[Deferred decision: what archiving a Session means]
    WEDGED_SESSION_AND_SANDBOX_RECOVERY -. archive behavior .-> SESSION_ARCHIVE_CONTRACT
    CENTRAL_EXECUTOR_LIFECYCLE_DESIGN -. related execution lifetime .-> SANDBOX_QUIESCENCE_DESIGN
    VM_RAM_SUSPEND -. related suspend semantics .-> SANDBOX_QUIESCENCE_DESIGN
    RUNNER_BUSY_TRACKING -. related .-> CENTRAL_EXECUTOR_LIFECYCLE_DESIGN
    AGENT_RESUME_NOTICE -. related .-> CENTRAL_EXECUTOR_LIFECYCLE_DESIGN
    RUNNER_BUSY_TRACKING -. related .-> AGENT_RESUME_NOTICE
    ENVIRONMENT_SUSPEND_MODES_DESIGN[Decision: distinct suspend modes, section 4] -. multi-mode notice .-> AGENT_RESUME_NOTICE
    RUNNER_BUSY_TRACKING[Unscheduled: the runner counts terminals and background processes as busy]
    SESSION_ACTIVITY_STREAM[Unscheduled: the notification service delivers on a Session's idle time]
    RESUME_CREDENTIALS_VALID[Decision: optional grants versus resume prerequisites]
    VOLUME_LAYOUT_CONVERSION -. future incompatible edits only .-> SANDBOX_SPEC_EDIT
    WORKSPACE_NODE_PORTABILITY -. related .-> SANDBOX_ARCHIVE
    VM_RAM_SUSPEND[Unscheduled: suspend a VM to disk with its RAM, section 4]
    DISPATCHER_SEND_AUTHORIZATION_GENERALIZE[Deferred: scoped sender grants beyond v0 service trust]
    STATE_VOLUME_BLOCK --> STATE_VOLUME_VIRTIOFS_RETIRE[Deferred: retire the superseded state attachment]
    IDLE_SUSPEND_UI_DRIVE[Operator: remaining VM idle-suspend/send-to-wake UI drive]
    VM_RAM_SUSPEND -. the same experience, resuming with memory kept .-> IDLE_SUSPEND_UI_DRIVE
```

### `DECLARATIVE_SESSIONS_DESIGN` — long-running Sessions and managed subscriptions through Flux

**Design folded into [persistent agents](persistent_agents.md#user-facing-configuration-and-ownership);
no implementation released.** The operator selected a whole managed agent via Flux + small CR +
new reconciler on 2026-11-17 13:33 UTC. It applies intent through Dispatcher and Notifications APIs;
operational state stays in Postgres. The plan's remaining lifecycle and authority decisions stay open.

Retain the existing direction (operator, 2026-11-04): Kubernetes may own bounded desired state
reconciled into Dispatcher; Dispatcher owns Session existence/placement, Notifications owns
subscriptions/inboxes, and operational per-Session data stays out of etcd. No second concurrent
Session authority. A webhook subscription still needs a choice between an existing source and a new
authenticated inbound endpoint; cron selects neither. The plan covers explicit adoption of UI/API-created objects, drift, edits,
removal, outage recovery, controller ownership and method authority. Source authorization remains
with deferred `SUBSCRIPTION_AUTHORIZATION_DESIGN`; declarative intent grants no access or wake.
**Exit:** the operator settles the remaining lifecycle/authority semantics in
`SCHEDULED_AGENT_WAKE_DESIGN`; implementation follow-ups are named only after that decision.

### `STATE_VOLUME_BLOCK` — block storage for Pod and VM state and workspace

**Deferred; operator, 2026-10-25 PDT: eventual endpoint, todos for later.** Both execution kinds
eventually use block-backed state and workspace storage, including replacing the VM state
virtiofs attachment. Select PVC modes and Pod mount/format ownership explicitly; preserve
journals, native harness homes, permissions, suspension and kind-compatibility checks. Virtio
remains the VM disk transport. This is independent of all-node local provisioning and does not
gate the selected VM workspace cutover. Details: [deferred endpoint](vm_block_storage.md#deferred-endpoint).

### `NODE_LOCAL_BLOCK_STORAGE` — node-local block volumes on every node

**Unscheduled; operator, 2026-10-13 PDT.** Extend capacity-enforcing node-local provisioning to
all cluster nodes, tracked by [#4717](https://github.com/agentydragon/ducktape/issues/4717).
`cluster/cdk8s/piraeus.py` defines strict-local SSD and HDD storage classes backed by node pools;
that does not establish usable capacity on every node or both volume modes. `local-path` does not
enforce PVC capacity. Verify capacity,
node affinity and both required volume modes. A node-local volume remains pinned to its node;
moving it is `WORKSPACE_NODE_PORTABILITY`. This provisioner work does not gate Sandbox ownership
or the explicit layout contract for workspace PVCs.

### `STATE_VOLUME_VIRTIOFS_RETIRE` — delete the superseded state attachment

**Deferred with `STATE_VOLUME_BLOCK`; revisit after its acceptance/activation and old workloads are gone.** Remove VM state
virtiofs mounts, guest ownership/UID workarounds used only by them, old template paths and tests/docs.
Keep unrelated virtiofs uses where live callers still need them. Verify Pod/VM state persistence on
the selected explicit layouts before retiring the path; data drops stay in a separate cutoff PR.

### `VOLUME_LAYOUT_CONVERSION` — explicit offline volume conversion

**Unscheduled, much later; operator, 2026-10-13 PDT.** The 2026-10-25 VM block-storage cutover
uses an accepted Sandbox reset and needs no converter. General conversion remains a future option,
not work for #10440 or a gate on its rollout. Until implemented, incompatible layout changes are
rejected. When scheduled, design a stopped-Sandbox operation with an explicit destination layout,
verified data/metadata preservation, crash-safe binding switch and separately authorized source
cleanup. See [storage layouts](../docs/sandbox_storage.md#formatting-and-conversion). Do not build
a generic conversion framework now.

### `SANDBOX_SPEC_EDIT` — edit a suspended Sandbox's template or kind

**Unscheduled; operator, 2026-10-13 PDT.** Let the operator change a Sandbox's template, including a
switch between VM and Pod, from the app, only while the Sandbox is suspended. Each incarnation
already reads the current template ([controller contract](../environments/SPEC.md#vms)); no
spec is replaced automatically; this is the explicit, operator-driven edit. Use the deployed
[workspace layout contract](../docs/sandbox_storage.md) so a compatible kind switch keeps the
workspace. Validate the
retained state and workspace layouts before replacing a workload. Reject incompatible edits;
`VOLUME_LAYOUT_CONVERSION` is a separate, much later task and does not block compatible edits. The
preset the app records on a Sandbox as a label must stay true after the edit.

Control design: [ADR](../docs/adr_sandbox_session_control.md).

### `WORKSPACE_NODE_PORTABILITY` — move a workspace between nodes

**Unscheduled for remaining node-local workspaces; operator, 2026-10-13 PDT: even later.**
Fresh VM Sandboxes use [replicated Piraeus storage](../docs/vm_sandboxes.md#guest-and-storage). For other
node-local volumes, consider portability by copying through VolSync, object storage or directly
while the Sandbox is suspended. This broader copy-based path does not gate replicated VM storage.

### `SANDBOX_AUTODISKFUL` — consider auto-diskful storage for Sandboxes

**Decision, unscheduled; operator, 2026-10-26 PDT.** Evaluate replicated Piraeus auto-diskful
storage for Pod Sandboxes, covering both retained state and workspace volumes. Fresh VM Sandboxes
already use `piraeus-ssd-2-autodiskful`; Pod Sandboxes still use strict-local HDD claims.
On 2026-10-26, both volumes pinned `public-coder-ducktape-2` to `ovh-ns102453`, where memory
requests prevented resume until a stateless GitLab replica was moved elsewhere.

Compare keeping strict-local placement against auto-diskful attachment within the OVH zone;
recommend whether the scheduling flexibility justifies replication capacity and I/O costs.
Include runner filesystem requirements and preservation of existing retained data in the decision:
changing a template does not migrate existing PVCs. This is an evaluation, not an approved storage
cutover or a prerequisite for the separate copy-based portability task.

**Exit:** reviewed recommendation for Pod state and workspace storage, with any migration and
activation work recorded as follow-up nodes if selected.

### `SANDBOX_ARCHIVE` — a suspended Sandbox archived to object storage

**Unscheduled; operator, 2026-10-13 PDT: a thought that volsync suggests, not a plan.** Resource
affordances for a Sandbox that still exists but whose volumes sit in restic or S3, so it must be
rehydrated before it can start, freeing the node's disk meanwhile. Sandbox status would say that it
is archived and where, and resuming one rehydrates first. Related to `WORKSPACE_NODE_PORTABILITY`,
which shares the copy mechanism. Preserve the declared workspace layout and retained identity;
the runner's state PVC would archive the same way.

Control design: [ADR](../docs/adr_sandbox_session_control.md).

```mermaid
flowchart TD
    SANDBOX_GENERIC_SPLIT[Unscheduled: split the generic Sandbox from the runner's sealing]
    RUNNER_IMAGE_LAYERING_DESIGN[Decision: what carries the runner, harnesses and per-template tools]
    RUNNER_LAYERING_SPIKE[Paused: sparse-root compatibility and measurement work]
    RUNNER_LAYERING_SPIKE_EXIT[Blocked: finish update-boundary evidence, measurements and recommendation]
    RUNNER_LAYERING_SPIKE_RETIRE[Blocked: port or retire the sparse-root scenario after design]
    SANDBOX_GENERIC_SPLIT -. related .-> RUNNER_IMAGE_LAYERING_DESIGN
    RUNNER_LAYERING_SPIKE --> RUNNER_LAYERING_SPIKE_EXIT
    RUNNER_LAYERING_SPIKE_EXIT --> RUNNER_IMAGE_LAYERING_DESIGN
    RUNNER_IMAGE_LAYERING_DESIGN --> RUNNER_LAYERING_SPIKE_RETIRE
    RUNNER_IMAGE_LAYERING_DESIGN --> RUNNER_LAYERING_VM[Paused: a guest takes runner and tools from separate images]
```

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

### `RUNNER_IMAGE_LAYERING_DESIGN` — what carries the runner, the harnesses and the tools

**Decision; operator, 2026-10-13 and 2026-10-15 PDT.** The 2026-10-15 framing names the property wanted: independent updates and RAM headroom. Today one artifact still carries the harnesses and tools:
`agentplane/env/images/container-disk.nix` bakes both harnesses and the tools into the NixOS qcow2 that
KubeVirt boots, and `agentplane/env/images/image.nix` bakes them into the Pod image. The environment
processes and `agentplane` command are packaged by the Bazel-built `//agentplane/env/images:disk_image`, a read-only disk
the VM attaches and an image the Pod mounts, so a runner bump no longer republishes either; bumping a
harness or a template's installed tools still rebuilds the whole artifact. Separate the concerns so a platform user supplies an image or definition with
no runner and no harnesses and the runner side layers them in, and nobody bumps build versions to move a
runner. State which of these the design must satisfy, in order:

- **Independent updates.** Runner and per-template tools move without rebuilding what they sit in.
- **The thing Pods genuinely cannot give** (operator, 2026-10-15): a workload may use a lot of RAM inside
  itself, as a Bazel build does, without starving the host node. A VM reserves its guest RAM, so what the
  guest does stays inside it; a Pod's burst is bounded by what the node has left. Say plainly whether
  layering is meant to reach for that property or whether it stays a VM-only one, because it decides how
  far to go.
- **Additive.** New runtime types are added beside the existing ones, not swapped in, so a runtime can be
  tried without retiring the one that works.

Related to `SANDBOX_GENERIC_SPLIT`: that separates the resource, this separates the image.

**Options worth costing, none chosen.** The operator named the first two; the rest are the field.

- **Extra read-only filesystem image per stage.** Attach the runner and the tools as their own images or
  disks; the base carries neither. Exactly the decoupling asked for, and Claude Code web already does it:
  on its Firecracker guest `/opt/claude-code` and `/opt/env-runner` are separate squashfs disks
  (`/dev/vdb`, `/dev/vdc`) mounted read-only beside a `/dev/vda` root —
  [web container spec](../../devinfra/claude/web_env/docs/container_spec.md).
- **Two-stage guest.** A lean stage 1 boots and runs the runner and mounts a stage 2 filesystem; the
  runner jails a forked child into stage 2 and execs Claude or Codex there. Runner lifetime becomes
  independent of the tool tree, at the cost of a pivot or chroot boundary and the work of trusting a
  mounted image.
- **Stay on KubeVirt, split the disks.** A `containerDisk` is already an OCI image carrying a disk, and
  `environment_vm.py` already attaches `config` and `trust` as separate volumes, so a three-disk guest (root /
  runner and harnesses / tools) needs no new mechanism — only the split. Halted image replacement already
  exists, which is the update path.
- **Kubernetes image volumes.** An `image` volume mounts an OCI image's filesystem into a Pod. The
  API server is v1.35.1, where `ImageVolume` is beta and on by default (GA in 1.36), and every node
  reports containerd 2.3.x. This is the direct answer to "this container should mount these 2 images",
  and beats an init-container copy on every incarnation. The runner already runs from such a mount in
  production Pods; whether the harnesses can is open.
- **Init container into a shared volume.** Today's portable form: copy the runner and harnesses from image
  X into an `emptyDir`. Works everywhere; pays copy time and disk each start.
- **Firecracker instead of KubeVirt.** The runtime the operator raised as acceptable if it helps: fast
  boot, small guest, squashfs-friendly, and the shape Claude Code web chose. The cost is a second VMM
  stack beside KubeVirt with no cluster scheduler for it. The RE under `devinfra/claude/web_env/re/` —
  including `process_api/src/firecracker_init.rs` — documents what that guest's init actually does and is
  the closest worked example we hold.

The disk split and Firecracker are not alternatives to each other: splitting disks is additive on the
runtime we have, and Firecracker is a new runtime added beside it.

The Firecracker primitive spike landed in #13957 and the opt-in environment backend in #14029.
For current behavior use the backend's
[contract](../docs/firecracker_sandboxes.md) and
[system-test scenario](../system_tests/scenarios/firecracker.py) define the checked same-Pod behavior.
It reuses the separate runner disk and preserves RAM within the same retained launcher Pod.
Cross-Pod recovery, native execution-session parking with durable terminal release, and staging
acceptance with Cilium enforcement remain separate work. KVM-enabled BuildBuddy execution on wyrm2 is tracked
in #14034. None of this selects a separate tools/harness disk layout.

### `RUNNER_LAYERING_SPIKE` — minimal guest, harness and tools-layer evidence

**Paused.** The [spike notes](../x/runner_layering/README.md) record the historical two-disk
Firecracker experiment, the Claude child-context abort, and the K3s ImageVolume trial. The retained
sparse-root scenario exercises a workload-owned tool, native Codex resume and private-state
separation. The local boot probe's exact recipe is available from its original revision.

Candidate layout to review: keep stage-one init and runner control outside the tools root. Mount
per-template tools read-only with explicit writable workspace and harness homes, tmpfs for `/tmp`,
and the required `/proc` and `/dev/null` paths. Consider the same filesystem contract via Pod image
volumes with separate workspace and persistent Session state. The Sandbox is the isolation boundary.

Finish the update-boundary and startup measurements for the design review. State any remaining
limits around full coding tasks, Claude, and Nix roots. Exercise independent tools-image replacement
and the selected mount layout on the maintained runtime that adopts them. After the decision, port
the sparse-root scenario to its production owner if its assertions apply, or retire it.

### `RUNNER_LAYERING_VM` — a guest takes the runner, harnesses and tools from separate images

**Paused (no worker since the runner disk landed, 2026-10-20); operator chose to split the disks on KubeVirt first (2026-10-20 PDT). The opt-in Firecracker backend landed in #14029; a runtime-class abstraction and whether harnesses become a separate disk stay open in `RUNNER_IMAGE_LAYERING_DESIGN`.**
Split the guest so a runner bump or a template's tool list does not rebuild and repush the whole VM. The
two-stage shape the operator named — lean stage 1 booting the runner and mounting a stage 2 filesystem,
with the runner jailing a forked child into stage 2 to exec Claude/Codex — is one option, with the
squashfs-disk and split-`containerDisk` shapes in the decision node beside it. KubeVirt is not assumed and
Firecracker stays open; what must not change is that the state volume and its ownership rules survive
(`STATE_VOLUME_BLOCK`, and the `107:107` virtiofs constraint). Halted containerDisk replacement already
works, so the update path exists. Landed: the environment processes and CLI are published as their own
`containerDisk` (`//agentplane/env/images:disk_image`), which the `environment-vm` template attaches as a
second read-only disk. The guest mounts it at `/run/agentplane/environment` and runs Environment Runtime
and the CLI from it, leaving the guest root disk without those binaries (a Sandbox on `agentplane-testing`
booted this way). Remaining: the tools disk is not split yet.
Preserve the shipped typed guest resource policy when splitting layers: stage 1 owns the OS
reserve and service caps, and native workloads and the executor keep their separate budgets.
Deliver a lean bootable guest: a bare root filesystem with the runner and harnesses available from
their chosen layer, and the tools-heavy environment separate. The layering decision still owns exact
disk placement. This work does not gate resource acceptance on the existing guest. The k3s test guest
(`agentplane/system_tests/vm/guest`)
shows a guest root filesystem can be an `oci_image` target: a candidate runtime then needs no Nix or Attic
and is an additive target rather than a second release pipeline.

### `IDLE_SUSPEND_UI_DRIVE` — finish the VM staging UI drive

**Partially accepted by the operator (2026-10-17 PDT); remaining VM drive belongs to the operator.**
The operator test-drove the five-minute idle suspension and message-to-wake successfully with Codex;
see [UI acceptance](../docs/runner_delivery_and_harness_design.md#rollout-and-rollback).
Do not repeat that accepted path as a rollout gate. The report does not identify a VM test.

The remaining check is the same experience through the staging web app with a VM Sandbox: idle
suspension, composer message, wake and delivery, and preservation of the expected context/workspace.
Record only observed gaps and what survived; VM-local disks and processes are lost on suspension,
while retained state/workspace volumes survive. No claim of a completed VM UI drive follows from
the automated Pod/VM lifecycle coverage.

Related: [lifecycle coverage](../system_tests/deployment/README.md#sandbox-lifecycle-coverage), and `VM_RAM_SUSPEND` for
resuming with memory retained.

### `AGENT_RESUME_NOTICE` — tell agents when their Environment was suspended and resumed

**Unscheduled; operator, 2026-10-13 PDT: it would be good.** Central sampler execution already
[reports verified incarnation changes](../sampler/SPEC.md#runtime-and-effects) when opening
a replacement exec-server session. Broader lifecycle notices should state the observed suspend mode,
duration, incarnation and what is known about processes, terminals, memory and workspace. Today's
cold-stop path can lose processes and terminals; a future memory-preserving VM suspend may retain
them. Do not claim either result without evidence. The `ENVIRONMENT_SUSPEND_MODES_DESIGN` review
owns the distinction and gates notices that support both modes, not a bounded notice for today's
cold-stop behavior. Account for `RUNNER_BUSY_TRACKING`, and distinguish a crash restart from an
intentional suspend. Choose the event/delivery owner and wording; test retained, lost and unknown
state as applicable to the implemented modes.

Related: [lifecycle coverage](../system_tests/deployment/README.md#sandbox-lifecycle-coverage).

### `ENVIRONMENT_READINESS_WAIT` — wait for environment readiness in client adapters

**Unscheduled; separate client follow-up.** The `EnvironmentService` watch reports current state but
does not wait for readiness. Add bounded wait behavior to CLI and Action/MCP adapters by consuming
`WatchEnvironment`; callers choose the timeout and receive a typed timeout, source-error or target
change outcome. Keep resume acceptance distinct from readiness and do not duplicate or extend the
server watch RPC. This follow-up does not block the shipped resume path.

### `AGENT_EXECUTOR_LOSS_NOTICE` — tell agents when executor restarts or kills lose their processes

**Unscheduled; operator, 2026-10-29 PDT: nice to have.** Notify affected agents when an executor
container/process restarts or an observed kill invalidates their commands, background jobs or
terminals, even if the Runner and Sandbox stay up. The Qwen CI collector thread
`68f0e6ea-d4cf-4dab-8d39-0aa479843c9a` lost processes across repeated executor-container OOMs while
its files survived; the model had to infer what happened from missing processes and transport errors.
Report the observed cause (OOM, restart, or unknown), scope and lost process handles, and what state
survived. Account for multiple Sessions sharing an executor. Choose the event/delivery owner and
deduplication semantics before implementation; do not replay uncertain commands automatically.
Exit evidence: a controlled executor restart or kill produces a notice for affected Sessions without
claiming a whole-Pod restart, lost files or a cause that was not observed. Related to
`AGENT_RESUME_NOTICE`, but not blocked on Sandbox suspend/resume work.

### `RUNNER_BUSY_TRACKING` — the runner counts terminals and background processes as busy

**Unscheduled; operator, 2026-10-14: not now, an open TODO that may be looked at later.** The runner
publishes `idle_since` (`EnvironmentRuntimeFacts`), busy while a delivery is under way, the bootstrap runs, any
Session has a turn, an unfinished command or a setup script, or an Execution connection is active.
An active Execution connection therefore blocks suspension, but detached processes and the VM
console do not have a general busy-lifetime contract. Decide which surviving background work and
attached terminals should block idle suspension and who reports them.
Test an attached terminal that detaches, a background process that outlives a turn, and a runner
restart. `AGENT_RESUME_NOTICE` tells the agent what a suspend ended.

### `CENTRAL_EXECUTOR_LIFECYCLE_DESIGN` — central Session execution and Sandbox lifecycle

**Decision; unscheduled.** Decide how a central-sampler Session and model interact with a suspended
executor Sandbox and how execution affects idleness ([issue #18311](https://github.com/agentydragon/ducktape/issues/18311)).
Options include automatic wake and an activity hold, informing the model and providing explicit
resume/lifecycle controls, or a combination; no approach is selected. Map existing wake, busy and
authorization ownership before choosing. The selected design must honor wake policy and Sandbox UID
and incarnation fences, preserve uncertain outcomes without replay, and cover suspended requests,
long-running processes, disconnect/recovery and eventual idleness.

### `WEDGED_SESSION_AND_SANDBOX_RECOVERY` — archive a Session and delete a Sandbox when the Runtime cannot recover

**Open user-visible bug, Nov 22.** The operator reports that the affected Sessions refuse archive
and the Sandbox refuses to finish deleting. In `agentplane-staging`, the Sandbox
`systemd-vm-placement-c30fa03431` has had a deletion timestamp since 2026-11-21 09:13:11 UTC.
The state-volume finalizer and both bound PVCs are present. The replacement VM launcher is Pending
and unschedulable. The Runtime is disconnected, a quiesce attempt is recorded, and the Sandbox
reports “publishing the replacement incarnation before quiescing.” The per-Session archive error
was not captured. [App archive admission](../app/internal/httpapi/threads/handler.go) refuses
unknown activity, including a disconnected Environment. [Sandbox deletion](../environments/SPEC.md#deletion)
normally waits for a seal before releasing state. The existing narrow VM suspension fix
[#18530](https://github.com/agentydragon/ducktape/pull/18530) predates this deletion request.

**Sandbox recovery.** Reproduce deletion on a disposable Sandbox whose Runtime cannot reconnect.
From the supported operator interface, complete deletion of that exact Sandbox without manually
patching a finalizer or relying on the guest booting. Fence new work and distinguish confirmed
History from any unexported journal tail. Preserve authored data and receipts, make retained
state recoverable, and never replay an ambiguous effect. Verify retries and late reconnect. The
[quiescence design](#sandbox_quiescence_design--redesign-sandbox-stopping-and-session-settlement)
and [attachment model](#sandbox-free-central-sessions) provide context.

**Archive symptom.** Capture the affected Session’s archive refusal and the activity Dispatcher
reported at the time. The operator wants a wedged Session to be manageable. The product response
belongs to the [deferred archive design](#session_archive_contract--what-archiving-a-session-means).
Sandbox recovery can ship independently.

### `SESSION_ARCHIVE_CONTRACT` — what archiving a Session means

**Deferred by the operator, Nov 22.** App currently stores the Thread archive flag after checking
Dispatcher activity. The operator chose App ownership as a conservative starting point. Eventual
ownership and behavior are open.

On Nov 23 the operator reported that Identity cleanup on
[staging Sandbox `public-coder-ducktape-3`](https://agentplane-staging.allegedly.works/#/sandboxes/public-coder-ducktape-3)
listed three Sessions as blockers even though all three were already archived:
`4a52cdbf-2436-4878-8a51-b162574da0a5`, `7d17ea9a-43f1-4b77-a6a9-84429aea2283` and
`d76abba7-10d1-4f77-9d40-3a828c2f75c7`.

The operator's proposed behavior is to prevent a Session from continuing while archived and
release its bindings, including its claim on the Identity. The Identity half is the teardown's first step in
[Identity ownership](#identity_ownership_design--who-owns-an-identity-and-what-binds-to-it). Decide which bindings archive
releases, how cleanup recognizes other Sessions that still use a shared Identity, and what Identity
an explicit unarchive would require if the original has since been deleted. The design should
preserve History and cover Sessions already archived under the current behavior.

The proposed direction is for Session Dispatcher to know whether a Session is archived when it
admits new work, dispatches queued work or wakes the Session. Decide which service owns archive
state. Walk through archiving during an active turn, a permanently disconnected host and a queued
notification.

For a central sampler Session, consider an explicit unarchive that allows another sampling turn
in the same Session. Agentplane owns the Session and its continuation records, so it can retain
them through archive and check the required bindings before admitting the next turn. Could Codex
and Claude offer the same operation when their native continuation state is still available?

Does the Notification Service also need to observe archive? Decide what should happen to
notification delivery, unread entries and subscriptions while the Session is archived.
[Notifications](../notification_service/api/notifications/notifications.proto) already offers inbox
retirement and subscription cancellation. Whether archive invokes either is a design question.
Consider Sessions sharing an Environment and Sessions with zero attachments alongside
[Session and inbox lifetime](#session_and_inbox_lifetime--decide-session-and-inbox-lifetimes).

### `SANDBOX_QUIESCENCE_DESIGN` — redesign Sandbox stopping and Session settlement

**Decision; unscheduled.** Redesign the ownership and protocol for suspend, resume and deletion
across the Sandbox Controller, Session Dispatcher, Gateway, Environment Runtime, Execution Service
and History. Separate deciding the fate of Sessions and queued commands from stopping processes,
sealing Runtime journals, delivering their final History cursors, and removing workloads and
volumes. Consider whether Dispatcher should coordinate the Sessions that might belong to a Sandbox;
do not assume it owns all Runtime work or select a particular protocol yet. Preserve the
[managed deletion fence](../environments/runtimeproxy/SPEC.md#managed-environment-deletion)
and coordinate with `CENTRAL_EXECUTOR_LIFECYCLE_DESIGN` and the suspend-mode decision.

**Desired outcome:** ideally, deletion of a Sandbox that has never received commands or Session
commands should not need to start or quiesce an Environment Runtime. Establish what durable,
exact-Sandbox and incarnation evidence would prove that is safe, including whether a Runtime ran
but admitted no work; do not restrict the goal to a Pod that was never scheduled. Account for
bootstrap and setup, direct Execution calls, Session creation and retained journals, and any
other work outside the Dispatcher. Fence new admission and concurrent forwarding before making a
no-work decision; distinguish known-unsent work from possibly forwarded or admitted work and
preserve unknown outcomes without replay. A Dispatcher acknowledgment proves journal admission,
not completed effects or History delivery, and an absent or disconnected Runtime is not proof it
never ran. Preserve existing journal, History, process-fencing and authored-data guarantees when
the evidence is uncertain, including after controller takeover or an incarnation replacement.

Compare Pod, KubeVirt and Firecracker lifecycles. Firecracker RAM suspension retains processes and
needs its admission barrier and checkpoint semantics; a launcher reporting `suspended` can mean
either a cold, never-booted guest or a completed checkpoint. Cover the scheduler bind versus stop
race, lost acknowledgments, startup and bootstrap races, prior unsealed incarnations, crash and
reconnect, queued but unsent commands, and deletion of a genuinely unused Sandbox. The observed
unscheduled staging VM that remained in quiescence is a concrete motivating case;
[#18530](https://github.com/agentydragon/ducktape/pull/18530) is a narrower VM fix, not the
redesign's exit criterion.

**Exit:** a reviewed ownership and race contract identifying each durable proof and service
boundary, the unused-Sandbox deletion behavior, and a testable migration from current guarantees.
Select implementation separately after that decision.

### `SESSION_ACTIVITY_STREAM` — the notification service delivers on a Session's idle time

**Unscheduled.** The History Service folds `idle_since` into `SessionSummary` (the turn and harness
events that start and end idleness), and the app shows it. The notification service currently chooses
when to deliver from its existing quiet window and sends every notice through the Session Dispatcher.
What remains is a second listener: read the Session summary after each `WatchSessionSummaries` notice
and choose to deliver after a minute of idleness, at most ten minutes after the first pending entry.
`idle_since` is display and delivery timing: suspension reads the Sandbox Controller's current
Runtime busy facts and never this field. Per-Session idle times stay out of Kubernetes.

Control design: [ADR](../docs/adr_sandbox_session_control.md).

### `DISPATCHER_SEND_AUTHORIZATION_GENERALIZE` — replace v0 service trust with scoped sender grants

**Deferred TODO; v0 decision does not block delivery design.** Dispatcher may specifically trust the
App and Notification service identities to act for their users, with wake permission separately
allowlisted. Do not extend that exception to agents or other services, except the one the operator accepted on
2026-10-26: the [`orchestration_v1` group's executor](../action_service/orchestration_v1/README.md), which acts only on Action Decisions,
until `ORCHESTRATION_V1_BACKEND_GRANTS` gives each of its operations a caller grant. Design destination- and
operation-scoped grants, delegated caller proof and the effect of revocation on queued work before
general senders are admitted. Consider RBAC-backed permissions, but choose the authority with the
multiagent model; the operator prefers verb-over-target rules to shape-matched auto-approval and expects Session
commands to be RBAC-checked in the Runner Gateway (2026-10-27,
[orchestration ADR](../docs/adr_orchestration_v1.md#why-shape-matching-is-a-stopgap)). Include authorization before wake and any required execution-time recheck. The
resolved environment UID or placement-generation fence is part of `DELIVERY_DESTINATION_CONTRACT`, independent of sender policy.

### `RESUME_CREDENTIALS_VALID` — grants and tokens still valid after a long suspension

**Decision; the [credential audit](../debug/resume_credentials_valid.md) is published in merged #10573.** Are app-created runtime
`PolicyBinding`s optional additive capabilities, or prerequisites for useful resume? The current
app adds independent expiring grants; Sandbox resume does not renew them. Recommend preserving
that additive model: expiry and revocation remain authoritative, with no automatic re-grant.
If required grants are desired, first define their durable identity, missing/expired status and
explicit operator re-grant flow. The audit distinguishes controller-driven expiry from exact
post-deadline denial, and projected-token renewal from grant renewal. Add implementation work only
for a reviewed contract that the code does not already satisfy; do not add an implicit Ready gate.

## 8. Ownership and naming cleanup

Decisions and designs about what things are called and which service owns what. The decided
renames and ownership moves (`ACTION_BOX_RENAME`, `HISTORY_DATABASE_RENAME`) are dispatched
from [Cleanups](#15-cleanups). Staging data follows
[staging data preservation](../docs/staging_data_preservation.md): Session identities and history
are irreplaceable, and a node that deletes a column or row stops for the operator.

```mermaid
flowchart TD
    ACTION_BOX_IDLE_REAPER[Unscheduled: delete Action boxes nobody uses]
    SANDBOX_ENVIRONMENT_RENAME_DESIGN[Later proposed: scope Sandbox to Environment naming]
    HISTORY_SERVICE_RENAME[Decision, unscheduled: History as a transcript service]
    SERVICE_SETTINGS_RETHINK[Decision, unscheduled: choose a shared service settings framework]
```

### `SANDBOX_ENVIRONMENT_RENAME_DESIGN` — review a scoped Sandbox-to-Environment rename

**Later proposed review; operator, 2026-11-01 UTC: probably rename Agentplane's execution-environment
domain from Sandbox to Environment once its model and boundaries settle.** This is exploratory; no
rename is selected. Start from the [Session/runtime model](session_runtime_model.md) and inventory
Agentplane-owned domain objects, public API/proto/CLI/UI/config names, Kubernetes resource names and
CRDs, storage and migrations, generated manifests, documentation and callers. The upstream
agent-sandbox `Sandbox` CRD remains separately owned. Action execution boxes keep their distinct
[`ACTION_BOX_RENAME`](#action_box_rename--action-boxes-stop-being-called-sandboxes) scope.

**Exit:** review a scoped vocabulary map and, if a rename is recommended, an ordered API/resource
and reference migration that preserves identities and stored data. Do not use a global text
replacement or implicitly rename upstream APIs/resources. No implementation or wire/CRD/storage
cutover is approved here; this proposal is independent of current authorization and reliability work.

### `HISTORY_SERVICE_RENAME` — History as a transcript service

**Decision, unscheduled (operator, 2026-10-20 04:05 UTC: "consider maybe").** Consider renaming the
History Service to a transcript service "or something like that", and history tags to transcript
tags with it ([tags](sandbox_tags.md#tags)). Deciding before `HISTORY_SANDBOX_RECORD` lands lets the
Sandbox field take its final name with no CRD rename. A service rename reaches its protos, token
audiences, images, the `agentplane/history_service` package and docs, and
[`HISTORY_DATABASE_RENAME`](#history_database_rename--name-the-database-for-its-owner) would take
the new name. **Name decided, operator, 2026-11-14 UTC:** the Journal service
([naming ADR](../docs/adr_journal_and_thread.md)); the rename itself stays unscheduled.

### `ACTION_BOX_IDLE_REAPER` — delete Action boxes nobody uses

**Unscheduled; operator, 2026-10-11 PDT.** A small controller deletes Action box Pods with no
`exec` for a configured time. The Action Service stamps a last-exec time on the box at each `exec`;
a plain Pod delete is enough, since boxes have no seal. It takes over `TODO(sandbox-lifetime)` in
`action_service/sandbox/inventory.go`; the closed draft [#7859](https://github.com/agentydragon/ducktape/pull/7859)
expired agent-sandbox boxes the same way. It also revokes a box's `WorkloadCredential` whose bound
Pod no longer exists, so a Pod deleted outside `dispose` or garbage collection leaves no resolvable
record. It also collects the boxes whose caller Identity is gone: they outlive the Identity with their
credentials revoked, and no caller can dispose of them since their old Identity cannot authenticate.
Boxes and Sandboxes are to converge into one object type
([`SANDBOX_BOX_CONVERGENCE`](task_freezer.md#sandbox_box_convergence--sandboxes-and-action-boxes-as-one-object-type)).

### `SERVICE_SETTINGS_RETHINK` — how Agentplane services are configured

**Decision, then migration. Unscheduled (operator, 2026-11-19).** Protobuf is the leading candidate
for generating Go and Python settings bindings. Compare it with the current typed YAML setup.

- Compare protobuf-aware YAML, textproto and binary protobuf for the settings file. Check presence,
  defaults, unknown fields, deployment diffs and startup diagnostics.
- Go must reject invalid settings at startup. Go-only validation is acceptable initially. Python may
  render well-formed settings that Go rejects on semantic grounds. Consider shared schema rules
  such as Protovalidate if synthesis-time checking is worth the integration cost. Keep runtime
  Secret inputs explicit.

The [sidecar and LLM ingress spike](https://github.com/agentydragon/ducktape/pull/12232) tried
generated bindings, Buf protoyaml and Protovalidate. Decide whether the App's generated configuration
API can reuse settings schemas for fields it exposes to the browser. Start from the
[current settings contract](../AGENTS.md#settings).

The shared strict YAML loader is the interim improvement. Choose the schema and file format before
planning the service migrations.

## 10. Runner ingress and control split

The runner pushes its journal's Events to the History ingress. For control, the Runner Gateway dials
the runner's private gRPC listener; the two routes are independent. Contract:
[runner control § Event ingress](../docs/runner_control.md#event-ingress).

```mermaid
flowchart TD
    RUNNER_JOURNAL_TRIM[Unscheduled: runners stop keeping what the ingress committed]
    HISTORY_INGRESS_OUTCOME_DESIGN[Unscheduled decision: item completion and command outcome conflicts at History ingress]
```

### `RUNNER_JOURNAL_TRIM` — runners stop keeping what the ingress committed

**Unscheduled; the operator's direction, 2026-10-12 PDT: later.** A runner could stop persisting
journal entries its ingress has already acknowledged, behind a flag, with full persistence kept
selectable; the freezer's
[`RUNNER_JOURNAL_SETTLEMENT`](task_freezer.md#runner_journal_settlement--settle-deltas-in-the-runner-journal-too) is the same idea at the delta level. Not designed: what the runner still needs locally (cursors, seal state, entries a
resumed harness or a lost push reads back) once acknowledged entries are gone, including what a revived incarnation reads back
(the retained incarnation history).

### `HISTORY_INGRESS_OUTCOME_DESIGN` — item completion and command outcome invariants at History ingress

**Decision, unscheduled; operator direction, 2026-11-10.** The incident's
`HarnessUserMessageConfirmed` and later `CommandFailed` name the same command. The
[Thread fold](../history_service/internal/threadfold/fold.go) narrowly tolerates that sequence for a
`SubmitInput` only when the failure reason is `session blocked by unresolved effects`; other
contradictory settlements still fail the fold. The [History append](../history_service/internal/storage/store.go)
accepts exact cursor replays and commits new cursor-valid entries without checking command outcomes,
so other contradictions can remain in raw history while that Session's Thread projection stalls.
Consider whether ingress should enforce one-time settlement invariants, and whether item completion
and command outcome need different rules; rejection is an option to review, not a selected protocol
policy.

Alternatives include refusing a contradictory outcome before commit, retaining every raw event and
folding the conflict into a visible anomaly while continuing the projection, or leaving ingress
permissive and requiring explicit recovery for an unfoldable projection. Recommendation: design item
and command rules separately, preserve raw evidence and per-Session availability, and require a
runner recovery path before selecting ingress rejection. The journal is append-only and cursor
ordered, so refusal can strand that cursor and prevent later entries, including later input
confirmation, from reaching History.

**Review question:** which repeated outcomes are exact replay versus contradiction, what item and
command transitions are valid, and where should a conflict be handled? Design the check across one
batch, earlier committed pages and concurrent Push calls; specify how existing malformed histories
remain readable and how their projections recover. **Exit:** an operator-reviewed contract with
concrete item and command examples, atomicity and concurrency boundaries, raw-history retention and
projection recovery, and a bounded runner recovery path for any rejected record. Add an
implementation node only if the decision selects a protocol change.

## 11. History reads and follows

The History Service's [`FollowSession`](../history_service/README.md#read-and-follow-one-session)
reads raw entries from a cursor, the tail or a number of matching observations back, with a kind
filter and optional live follow. The browser follows Threads through Electric
([thread sync](thread_sync/README.md)). Remaining consumers and decisions:

```mermaid
flowchart TD
    JOURNAL_STORAGE_BASELINE[Pending rollout: measure staging journal packing]
    JOURNAL_STORAGE_DESIGN[Decision: efficient retained event storage]
    JOURNAL_STORAGE_BASELINE -->|measured baseline for final comparison| JOURNAL_STORAGE_DESIGN
    JOURNAL_REPRESENTATION_DESIGN[Decision, unscheduled: History as stored journals]
    THREAD_SYNC_WITHOUT_ELECTRIC[Decision, unscheduled: replace Electric with our own thread stream]
    JOURNAL_REPRESENTATION_DESIGN -. what the stream follows .-> THREAD_SYNC_WITHOUT_ELECTRIC
    NATIVE_HARNESS_INTERFACES_DESIGN[Decision, unscheduled: native harness interfaces past the Runtime]
    JOURNAL_REPRESENTATION_DESIGN -. if (a): journal view before the Runtime stops deriving Events .-> NATIVE_HARNESS_INTERFACES_DESIGN
    SESSION_ACTIVITY_STREAM[Unscheduled: deliver on a Session's idle time, section 7]
    NOTIFICATION_COMMAND_OUTCOME_DESIGN[Unscheduled: notice delivery without reading history, section 2]
    THREAD_READ_POLICY[Blocked: scoped archive reads, section 3]
```

Revisit replacing `ReadSessionEvents` with `FollowSession` and `follow = false` once callers can
preserve bounded reads and the browser's raw observation detail through the HTTP facade.
Automatic titles remain deferred under
[`THREAD_TITLE_GENERATION`](task_freezer.md#thread_title_generation--revisit-automatic-thread-titles).

### `JOURNAL_STORAGE_BASELINE` — measure staging journal packing

**Pending staging rollout, 2026-11-24.** History and sampler packing merged. Each writer waits
for its own reader rollout. Record when each writer starts packing, whether historical History
rows were backfilled, and the subsequent relation growth and event volume. Compare with the
[November 24 recurrence and earlier incidents](storage_representations.md#november-24-recurrence-and-storage-decision).

### `JOURNAL_STORAGE_DESIGN` — choose how to store retained event values

**Operator decision requested, 2026-11-24.** Can an existing library or backend provide
cross-record compression behind simple value writes, lookups and scans while preserving sampler
recovery, History replay and whole-session deletion? Prioritize engine-managed compression and
evaluate content references, including alternatives to PostgreSQL. Begin the workload study now. Use the
post-rollout baseline for the final comparison. **Exit:** bring the operator a measured
recommendation, including the option of retaining the stopgap, before scheduling implementation.
The [storage plan](storage_representations.md#november-24-recurrence-and-storage-decision) owns
the comparisons. Coordinate any change to what History retains with the journal representation
decision below.

**Follow-up: duplicated packing in Sampler and History.** The sampler packs its journal,
reads the events back for export, and History packs the imported events again for cursor-
and kind-filtered reads. Use one exported event range to measure encode/decode CPU, WAL
and retained bytes on both sides. Compare keeping separate layouts, sharing only the packer
code, and reusing sealed data across export. Sharing code alone would still compress and
store the events twice. Recommend which work, if any, to remove while preserving sampler
recovery during a History outage, idempotent import, filtered replay and independent retention.
This decision can follow the block-size experiment; it does not gate tuning either writer.
Commit the experiment's dated results on real staging event windows and its repeatable
measurement procedure in ducktape. Include the observed CPU, I/O and storage trade-offs by
block target, codec and level; publish aggregate measurements without event payloads.

### `JOURNAL_REPRESENTATION_DESIGN` — History as stored journals

**Decision, unscheduled; operator, 2026-11-14 UTC:** "journals" sounds like where History should
eventually go. Vocabulary is decided ([ADR](../docs/adr_journal_and_thread.md)): a Journal is what a
harness or process emits, and a Thread is the messaging object agents and people act on
([`AGENT_AUTHORED_CONVERSATIONS_DESIGN`](#agent_authored_conversations_design--conversations-agents-act-on-through-tools)).

Two separate problems:

- **Size.** On staging the projection tables (about 1.3 GB) outweigh the raw events (about 1.0 GB),
  and packed blocks and sampler capture keep several copies
  ([storage representations](storage_representations.md)). Removing redundant sampler response
  capture holds either way; sharing bodies between events and the projection waits on this node.
- **Coercion.** The fold approximates Claude Code, Codex, sampler and process formats in one
  materialized entity model instead of rendering each source on read.

**Recommendation:** History stores each source's Journal verbatim with its kind; the app renders a
journal view per kind at read time; the common surface keeps only what every source has (inputs,
turn boundaries, status, usage). **Question for review:** retire the fold and its tables for
per-kind rendering on read, and with what common surface?

**Operator direction, not a release (2026-11-17 13:21 UTC, among "things that are on top of my head
right now as stuff i'd like to pursue sometime"):** "then, making history more sane. making history
ideally not pretend there's some harness-neutral format into which we translate all harness logs,
and instead i guess either just expose harness logs as we get them, or do some basic folding of
in-progress streaming on them. either giving readers those logs as they exist, or giivng the logs
potentially viewed/projected into a neutral-ish format if needed at query time, rather than in
persistence. and we could possibly also get rid of electric. basically, "the thing we store is the
logs to the harness and we store them as they are and mostly display them as they are, they are
basically logs for forensics/debugging, not a thing that you'd look at during everyday usage"". He
couples it with the Thread in
[`AGENT_AUTHORED_CONVERSATIONS_DESIGN`](#agent_authored_conversations_design--conversations-agents-act-on-through-tools):
History stops being the main conversation-style view only if agents work well through a Thread.

**Today:** the Runtime already journals every native frame verbatim as `Native`, both directions,
beside the `Event`s its adapters derive ([interfaces](native_harness_interfaces.md#output)), and
History's raw log keeps both; the fold persists the derived projection. So the verbatim logs exist;
the per-kind journal view, read-time projection and fold retirement do not.

**Input side:** [`NATIVE_HARNESS_INTERFACES_DESIGN`](#native_harness_interfaces_design--expose-native-harness-interfaces-past-the-runtime);
the operator, 2026-11-16 06:58 UTC, calls the command translation "kinda the input side of the same
thing".

### `NATIVE_HARNESS_INTERFACES_DESIGN` — expose native harness interfaces past the Runtime

**Decision, unscheduled; operator, 2026-11-14 20:34 UTC:** considering whether the Environment Runtime
should expose Codex's and Claude's interfaces, input and output, more directly instead of translating
to neutral projections and from generic commands. **Operator, 2026-11-16 06:38 UTC:** two coherent
shapes, nothing in between: (a) the Runtime forwards the harness's own protocol, or (b) it does all
the translation, as today; a shared commands layer raises where it lives. **06:58 UTC:** (b) stays
by default as the status quo, no migration within about a week; the abstraction leaks and fancier
features (Agentplane-hosted tools for the model) would leak more; this is the input side of the
managed Thread versus harness Journal split.
[`native_harness_interfaces.md`](native_harness_interfaces.md) maps what the Runtime decides beside
the process and where each shape puts it. **Recommendation:** (b), with the shared layer in the
Runtime and the central sampler; harness-specific features as Runtime-framed operations typed per
kind. Under (a) the layer would sit in `session_dispatcher`, never `environments`, and the output
half would follow
[`JOURNAL_REPRESENTATION_DESIGN`](#journal_representation_design--history-as-stored-journals)'s
journal view. **Open question:** where the neutral-command translator lives long-term; revisit when
a feature such as driver-hosted tools crosses the common set.

### `THREAD_SYNC_WITHOUT_ELECTRIC` — replace Electric with our own thread stream

**Decision, unscheduled; operator, 2026-11-14 UTC:** dropping Electric and doing the sync ourselves
"might be better", "to be looked into"; it costs a separate container.

Electric replicates two History tables, `thread_entity` and `thread_payload_chunk`, per Thread and
projection epoch, through the app's authorizing [proxy](../app/internal/electricproxy/electric.go), for
the thread view alone ([`electric_sync.tsx`](../app/frontend/sessions/electric_sync.tsx)). Every
other live browser view already follows gRPC watches through the app facade
([`watch_stream.ts`](../app/frontend/transport/watch_stream.ts)). Its costs are:

- a single-replica `Recreate` Deployment with a 5 GiB `emptyDir` ([exception](../docs/electric_single_replica.md));
- a logical replication slot on the History database;
- the mismatches in [Electric limits](../docs/thread_sync_electric_limits.md): every follower gets
  the shape's whole live log, deltas exist only as committed rows, and compaction is re-sent to
  followers.

[Thread sync](thread_sync/README.md) already scores replacements; the moving window meets every
requirement row where Electric is short.

Coupled to [`JOURNAL_REPRESENTATION_DESIGN`](#journal_representation_design--history-as-stored-journals): if History becomes harness journal storage rendered per kind,
the synced fold tables, and Electric's job, go away, and the stream should follow journal records
instead of fold rows.

The operator, 2026-11-17 13:21 UTC, on the same direction: "and we could possibly also get rid of
electric" ([`JOURNAL_REPRESENTATION_DESIGN`](#journal_representation_design--history-as-stored-journals)).

**Recommendation:** replace Electric with a History server stream reached through the existing app
watch facade, and build it once against whichever History representation is chosen rather than
first over the fold tables. **Question for review:** build the replacement now over the fold, or
after the History representation decision, over journal records?

## 13. Delivery boundaries, APIs and harness identity

This section holds the unfinished delivery-acceptance cleanup, destination and harness-identity design and
implementation, and API and test-tier work.
Decision nodes do not authorize runtime changes. Detailed code paths and alternatives are in the
[delivery and harness design note](../docs/runner_delivery_and_harness_design.md).

**Approved transport direction (operator, 2026-10-15 UTC):** gRPC for internal APIs,
gRPC-Gateway for the generated HTTP/JSON facade, and an ergonomic CLI, adopted incrementally by
service and interface. Use JSON server streams for watches/waits and native gRPC for full duplex.
Individual service migrations and the CLI rollout remain separate work items below, and the
HTTP/JSON facade is built per service inside each migration; this direction authorizes no runtime change
beyond the transport track nodes the operator dispatched.

```mermaid
flowchart LR
    DISPATCHER_PERSISTENCE_MODEL_DESIGN[Decision: immediate-or-fail send]
    SESSION_DATA_OWNERSHIP_DESIGN[Later decision: Session data and service boundaries]
    KUBERNETES_UID_CHECK_AUDIT[Unscheduled: audit Kubernetes name and UID checks]
    SESSION_IDENTITY_CONTRACT[Decision: Session identity and shared-Environment admission]
    ENVIRONMENT_IDENTITY_BINDING_DESIGN[Decision: Environment Identity binding and Session creation order]
    SESSION_IDENTITY_PROVISIONING_DESIGN[Deferred decision: Session-scoped identities]
    SESSION_MANAGED_IDENTITY[Deferred: provision Session-scoped identities]
    SESSION_NATIVE_IDENTITY_WORKFLOW[Blocked: native Sessions in a new Environment with an existing Identity]
    ENVIRONMENT_SHARED_IDENTITY[Environments sharing an Identity, section 3]
    SESSION_ACCESS_GROUPING_DESIGN[Decision: Session access grouping and tags]
    CENTRAL_SESSION_CREATION_CONTRACT[Decision: central Session creation without an Environment]
    SESSION_ATTACHMENT_CONTRACT[Decision: Environment attachments and targeting]
    SESSION_ENVIRONMENT_SETUP_DESIGN[Decision: setup ownership and scope]
    SESSION_IDENTITY_ENFORCEMENT[Blocked: enforce identity in shared Environments]
    CENTRAL_SESSION_WITHOUT_ENVIRONMENT[Blocked: central Session without an Environment]
    SESSION_ENV_ATTACHMENTS[Blocked: attach and detach Environments]
    SESSION_ATTACHMENT_SURFACES[Blocked: attachment tools and presentation]
    SESSION_ATTACHMENT_HISTORY[Blocked: retain attachment transition history]
    SESSION_LIFETIME_IMPLEMENTATION[Blocked: implement reviewed Session and inbox lifetimes]
    CENTRAL_SESSION_NOTIFICATIONS[Blocked: environment-independent Session inbox delivery]
    DELIVERY_DESTINATION_AUTHORITY_DESIGN[Decision: send authority and internal fencing]
    COMMAND_ADMISSION_REFUSAL_DESIGN[Decision: authoritative command admission refusals]
    SESSION_AND_INBOX_LIFETIME[Decision: Session and inbox lifetimes after environment deletion]
    DELIVERY_DESTINATION_CONTRACT[In progress: Session-ID command addressing]
    SESSION_HISTORY_PRESENTATION[Blocked: Session history and environment presentation]
    DISPATCHER_RUNNING_STATUS[Acceptance: Runtime restart is the operator's call]
    TURN_STATE_PLACEMENT_DESIGN[Unscheduled: decide where turn states live]
    SESSION_IDENTITY_CONTRACT --> CENTRAL_SESSION_WITHOUT_ENVIRONMENT
    CENTRAL_SESSION_CREATION_CONTRACT --> CENTRAL_SESSION_WITHOUT_ENVIRONMENT
    SESSION_ATTACHMENT_CONTRACT --> CENTRAL_SESSION_WITHOUT_ENVIRONMENT
    SESSION_AND_INBOX_LIFETIME --> CENTRAL_SESSION_WITHOUT_ENVIRONMENT
    SESSION_IDENTITY_CONTRACT --> SESSION_IDENTITY_ENFORCEMENT
    SESSION_IDENTITY_CONTRACT --> SESSION_MANAGED_IDENTITY
    SESSION_IDENTITY_PROVISIONING_DESIGN --> SESSION_MANAGED_IDENTITY
    SESSION_IDENTITY_CONTRACT --> SESSION_NATIVE_IDENTITY_WORKFLOW
    ENVIRONMENT_IDENTITY_BINDING_DESIGN -. if unbound creation is selected .-> SESSION_NATIVE_IDENTITY_WORKFLOW
    SESSION_MANAGED_IDENTITY -. optional Session-scoped Identity .-> SESSION_NATIVE_IDENTITY_WORKFLOW
    ENVIRONMENT_SHARED_IDENTITY --> SESSION_NATIVE_IDENTITY_WORKFLOW
    SESSION_IDENTITY_ENFORCEMENT --> SESSION_NATIVE_IDENTITY_WORKFLOW
    SESSION_IDENTITY_CONTRACT --> SESSION_ATTACHMENT_CONTRACT
    SESSION_ATTACHMENT_CONTRACT --> SESSION_ENV_ATTACHMENTS
    SESSION_ENVIRONMENT_SETUP_DESIGN -- setup ownership --> SESSION_ENV_ATTACHMENTS
    SESSION_ENVIRONMENT_SETUP_DESIGN -- setup ownership --> CENTRAL_SESSION_WITHOUT_ENVIRONMENT
    SESSION_IDENTITY_ENFORCEMENT --> SESSION_ENV_ATTACHMENTS
    CENTRAL_SESSION_WITHOUT_ENVIRONMENT --> SESSION_ENV_ATTACHMENTS
    SESSION_ENV_ATTACHMENTS --> SESSION_ATTACHMENT_SURFACES
    SESSION_ENV_ATTACHMENTS --> SESSION_ATTACHMENT_HISTORY
    CENTRAL_SESSION_WITHOUT_ENVIRONMENT --> SESSION_HISTORY_PRESENTATION
    SESSION_ENV_ATTACHMENTS -. multiple attachments .-> SESSION_HISTORY_PRESENTATION
    SESSION_AND_INBOX_LIFETIME --> SESSION_LIFETIME_IMPLEMENTATION
    SESSION_IDENTITY_CONTRACT -. independent identity .-> SESSION_LIFETIME_IMPLEMENTATION
    CENTRAL_SESSION_WITHOUT_ENVIRONMENT -. central lifecycle .-> SESSION_LIFETIME_IMPLEMENTATION
    SESSION_AND_INBOX_LIFETIME --> CENTRAL_SESSION_NOTIFICATIONS
    SESSION_IDENTITY_CONTRACT -. identity review .-> SESSION_ACCESS_GROUPING_DESIGN
    SESSION_ATTACHMENT_CONTRACT -. attachment review .-> SESSION_ACCESS_GROUPING_DESIGN
    SESSION_AND_INBOX_LIFETIME -. lifetime review .-> SESSION_ACCESS_GROUPING_DESIGN
    SESSION_ACCESS_GROUPING_DESIGN -. cross-principal access .-> CENTRAL_SESSION_NOTIFICATIONS
    SESSION_IDENTITY_CONTRACT --> CENTRAL_SESSION_NOTIFICATIONS
    CENTRAL_SESSION_WITHOUT_ENVIRONMENT --> CENTRAL_SESSION_NOTIFICATIONS
    HARNESS_IDENTITY_DESIGN[Decision: private store and native Session mapping]
    RUNNER_STORE_SESSION_IDENTITY[Blocked: implement reviewed private mapping]
    RUNNER_IDENTITY_LEGACY_CLEANUP[Blocked: retire old identity assumptions]
    IDLE_SUSPEND_UI_DRIVE[Operator: remaining VM idle-suspend/send-to-wake UI drive, section 7]
    RUNNER_HARNESS_OBJECTS_DESIGN[Decision: process diagnostics and placement, section 3]
    CNPG_FREE_K3S_INTEGRATION_TIER[Blocked: add a PostgreSQL-free K3s tier]
    DELIVERY_DESTINATION_AUTHORITY_DESIGN --> DELIVERY_DESTINATION_CONTRACT
    RUNNER_STORE_SESSION_IDENTITY --> RUNNER_IDENTITY_LEGACY_CLEANUP
    HARNESS_IDENTITY_DESIGN --> RUNNER_STORE_SESSION_IDENTITY
    HARNESS_IDENTITY_DESIGN --> RUNNER_HARNESS_OBJECTS_DESIGN
```

### `DISPATCHER_PERSISTENCE_MODEL_DESIGN` — decide on an immediate-or-fail send

**Decision; queue and database direction selected by operator, 2026-11-02.** Dispatcher
PostgreSQL and queue-backed admission remain mandatory; the database-free / queue-off mode is no
longer a candidate. This does not require every Environment Service operation to enter the queue.
Whether to offer an explicit immediate-or-fail send within the mandatory queue contract remains open.
See the [Session/environment model](session_environment_model.md#where-should-live-control-live).
Topology ([section 17](#17-two-binaries-and-the-go-layout)) and
[delivery ownership](../docs/adr_session_delivery_lease.md) are decided and built.

### `DISPATCHER_RUNNING_STATUS` — the Dispatcher records running status

**Implementation landed; only the Exit acceptance remains. It needs a Runtime restart, which is
the operator's call.** The
Session Dispatcher is the authority on which Sessions exist and what state they are in (operator,
2026-11-16 07:12 UTC: "yeah, session watch plus migrating app sounds good to me"); archived state and
Session/Thread titles stay in the App for now. It records hosted Sessions' HarnessHost status from
one watch per running Sandbox through the Environments proxy (operator's card, 2026-11-17
06:52 UTC: "Dispatcher watches via proxy"), and central Sessions' status from one sampler watch. It
publishes both on its Session `WatchSessions`
([Dispatcher specification](../session_dispatcher/dispatcher/SPEC.md#harnesshost-activity-observation)).

**Exit:** a shared Codex host with attached and dormant Sessions, a Runtime restart and a suspended
Sandbox each show the accepted current, stale and unknown states in the App.

### `TURN_STATE_PLACEMENT_DESIGN` — decide where turn states live

**Unscheduled to-do; the operator, 2026-11-17 06:40 UTC:** "okay we can leave that for a future
todo" "(to figure out where what states of turns and such should live)". It follows their question
at 06:29: "i wonder whether there should be some representation of e.g. turns aborted with an
error, idk".

Decide where each turn fact lives: the host's HarnessHost status, the Journal and History, or the
Dispatcher's running-status record. The facts include the active turn, admitted commands, how a turn
ended and why, and what ended it. Where they are on devel (a428a59):

- Both hosts journal `TurnCompleted{turn_id, status, error}`. FAILED comes from the native harness
  or from the sampler's provider and execution failures; PROCESS_LOST from a harness exit or Runtime
  restart during the turn.
- History folds only the status into `SessionSummary.last_turn_status`. The App's turn-error mark
  and `get_session` read it; the App timeline and `read_turns` show the error from the event.
- The [HarnessHost Session status](../harness_host/README.md#session-status) carries the active turn and pending commands,
  not how turns ended.

Examples to settle, not the whole scope:

- The operator saw the timeline report a failed Chat Completions turn while the Session indicator
  still said active. Cover output exhaustion and a rejected streamed tool call. Compare the host's
  active turn and command with Dispatcher watch updates and the App badge, live and after reload.
  With no subsequent work, the terminal turn must cease displaying as active without another input.
- Whether readers need a pointer to the ending event (`{turn_id, status, cursor}` in place of the
  bare status).
- A sampler interrupt that cannot confirm its cleanup journals an ordinary INTERRUPTED and blocks the
  Session. Only the recovery runbook reads `blocked`, so the App shows that Thread as idle until it
  reads the running status.

Coordinate with `SESSION_DATA_OWNERSHIP_DESIGN` and `DISPATCHER_RUNNING_STATUS`.

**Exit:** a reviewed table of turn facts with owner, readers and lifetime, and scoped follow-ups.
This node authorizes no API change.

### `SESSION_DATA_OWNERSHIP_DESIGN` — review Session data, services and databases

**Later design decision; operator requested review, 2026-11-04.** Agentplane now owns globally
addressed Session records, and Session-keyed data is spread across Dispatcher Sessions and commands,
History ingress and projections, Notification inboxes, central sampler state, and environment-local
Runtime records and journals. Inventory each fact, its authoritative writer, readers, lifetime and
recovery boundary; include the command and ingress ends separately.

Compare retaining modular services and stores, moving selected Session state from Environment
Runtime to Dispatcher or another Session owner, using one database with linked Session-keyed tables,
and other service/database groupings. Distinguish a shared PostgreSQL database from a shared service,
write ownership and transaction boundaries; none follows automatically from a common Session UUID.
The existing decision to use Dispatcher PostgreSQL and keep History separate remains the working
implementation direction until explicitly reconsidered. Evaluate isolation, availability, scale,
authorization, schema migrations, native continuation and lost/uncertain effects in each option.

Coordinate with `SESSION_AND_INBOX_LIFETIME` on the candidate independent Session create/delete
lifecycle and the treatment of inbox, History and other Session data. A shared database alone does
not settle deletion or justify deleting authored data. Do not treat this later review as a
prerequisite for unrelated safe native-first work. See the
[Session data review](session_environment_model.md#session-keyed-data-and-storage-boundaries).

**Exit:** an evidence-backed inventory and comparison, a reviewed ownership and database direction,
and appropriately scoped follow-up work. This node authorizes no service move or data migration.

### `KUBERNETES_UID_CHECK_AUDIT` — audit Kubernetes names and exact-object checks

**Unscheduled review; operator, 2026-11-04.** Inventory Agentplane checks and stored references
that require a Kubernetes object UID or reject a same-name replacement. Kubernetes RoleBindings
grant ServiceAccounts by namespace and name, and Flux similarly uses names for ordinary resource
references; an exact-object check does not automatically improve that authorization boundary.
Trace representative checks end to end across SA principals, policy and RBAC, Session/History
ownership, delegated callers, Environment/Pod/VM identity, controller ownership and deletion.

For each check state its intended identity, actual authority and replacement behavior, what breaks
if it changes, and whether Kubernetes or another enforcement point can enforce the same distinction.
Compare stable name-based references with exact-object checks backed by a concrete need: token and
caller authentication, stale-work or Environment-incarnation fences, owner/deletion preconditions,
and historical attribution. Include the consequences of SA and role-binding recreation, who may
create Pods or issue tokens under an SA, and revocation/convergence windows. Consider whether a
check is redundant, mismatched with name-based RBAC, or an essential guard; do not presume either
that UIDs are always safer or that all references should follow names.

**Exit:** a source-linked inventory with specific keep/change recommendations and independently
reviewable follow-up slices. For each chosen behavior, explain why its name or UID semantics are
correct for the real authority and lifecycle, what failure or replacement case they protect, and
why the plausible alternative is unnecessary or insufficient; identify tradeoffs and uncertainty.
Preserve current checks until their replacement contract and migration are reviewed. This audit does
not block the focused Session identity work or authorize a blanket security change.

### `SESSION_IDENTITY_CONTRACT` — Session identity and shared-Environment admission

**Decision; the identity is an Agentplane `Identity` (operator, 2026-11-05 and 2026-11-10).** A
Session's principal is one `Identity`, reached by the
[three credential forms](identity_and_kubernetes_accounts.md#target-one-identity-three-credential-forms);
a ServiceAccount is only an account an Identity may act as toward the Kubernetes API. Session
execution uses its recorded Identity; retain exact workload-incarnation and Environment fencing,
but do not reintroduce a ServiceAccount-keyed Session principal. The broader decisions below remain
open within the Identity model:

- **Name and incarnation.** The durable principal is the Identity's namespace and name; many
  Sessions may share one. A same-name replacement is the same named principal but a different
  incarnation: retain and verify Identity UIDs for authentication, attribution and fencing stale
  in-flight work, and never let a replacement inherit an accepted operation's fence or an old
  incarnation's `WorkloadCredential`.
- **Who may act as it.** Who may create a Session naming an Identity, bind it to an Environment,
  grant it PolicySets, and change or revoke those grants. The
  [sampler already delegates](../sampler/SPEC.md#identity-and-admission) for the recorded Identity.
  The trust boundary includes who may create or recreate the Identity, write a `WorkloadCredential` naming it
  ([who writes](../docs/workload_authentication.md#workloadcredentials)) and bind policy to it; a UID pin in
  Dispatcher alone cannot override name-keyed bindings.
- **Existing and Session-scoped identities.** A Session may name an authorized existing Identity,
  including its Environment's; it never deletes, or implicitly grants to, one it did not create. A
  fresh Identity for a single Session is wanted (operator, 2026-11-10); its provisioning is
  `SESSION_IDENTITY_PROVISIONING_DESIGN`. Preserve the web's preset and editable PolicySet workflow
  where it creates an Environment or an explicit grant, showing when the Identity is shared; presets
  convey no authority.
- **Lifetime.** The Session's Identity is independent of Environment lifetime
  ([identity lifetime](identity_and_kubernetes_accounts.md#identity-lifetime)). Decide how
  intentional revocation differs from deletion, and whether a managed Identity is reconciled back
  into existence; deletion alone does not durably revoke a declaratively managed one.

**Required shared-Environment invariant:** Sessions using the same Environment must have the same
verified Identity. An Environment is a shared trust domain: co-resident agents can communicate and
use its ambient egress and placeholder credentials, so different Session IDs, directories or
narrower nominal grants do not isolate them. The Environment's Identity is the subject its current
incarnation's `WorkloadCredential` names, proven by its SVID; reject a Session of another Identity
on native placement and central attachment
([changing an environment's identity](identity_and_kubernetes_accounts.md#changing-an-environments-identity)).
Names identify the intended principal, not proof: authenticate the SVID, authorize the caller, fence
the actual incarnations and prevent check/admit races. An Environment's Identity changes only while
nothing runs and no Session of another Identity is placed on or attached to it; accepted work is
never silently reassigned. If the Identity disappears, identity-dependent operations fail closed
until it exists and is authorized again; a same-name replacement acts as the named principal, not
as the old incarnation. Operator-only access to a Session whose Identity is gone stays distinct from
a live Identity whose Session has no Environment.

**Rebind boundary to review:** #15445 serializes an `EnvironmentService` identity change with
Dispatcher Session admission, but a direct Kubernetes `Sandbox.spec.identity` edit bypasses that
guard. Delivery and wake reject a mismatch, which can strand an existing Session. The service
rechecks the chosen Identity's UID before patching a name-only reference; deletion and same-name
recreation after that read can make the patch name the replacement. Decide whether those outcomes
are acceptable under the existing name-based and fail-closed contract or whether direct writers and
replacement need a stronger coordination boundary. Recommendation: retain that contract until the
tradeoff is reviewed, rather than silently choosing UID-bound Sandbox references. Cover bypass,
replacement between the final read and patch, accepted work and recovery in
`SESSION_IDENTITY_ENFORCEMENT`; do not assume a Kubernetes/PostgreSQL transaction.

**Exit:** reviewed examples for a central Session with zero Environments naming an existing
Identity, two same-Identity Sessions sharing an Environment, a permission request for that Identity,
and rejection of a different Identity, covering creation, placement, delegation, cleanup and denial;
plus where a Session-scoped Identity fits, without choosing its provisioning.

### `ENVIRONMENT_IDENTITY_BINDING_DESIGN` — Environment Identity binding and Session creation order

**Decision; binding before start is the mechanism, the creation order is open.** An environment may
be created unbound and bound before it starts: `Sandbox.spec.identity` changes only while nothing
runs, and the next start gets a fresh `WorkloadCredential`
([changing an environment's identity](identity_and_kubernetes_accounts.md#changing-an-environments-identity);
the field, admission rule and controller Identity switch have landed).
Native Session placement names an existing exact Environment UID, and central V1 a fixed executor
Environment; reusing an existing Environment's Identity needs nothing below. Decide the sequence for
a Session that brings its Identity, existing or Session-scoped
(`SESSION_IDENTITY_PROVISIONING_DESIGN`), to a new Environment: create the Identity and grant first,
then create or bind the Environment and start, or persist a separate Session reservation before
binding it. The launch sequence for an Identity-bound Sandbox is already settled in the
[identity plan](identity_and_kubernetes_accounts.md#open-questions-and-tricky-bits); this node
covers native Session placement and recovery. Preserve
exact Environment UID and Identity incarnation, authorization, conflict handling, idempotency and
recovery of partial creation. Decide who may bind an unbound Environment and what happens to
abandoned, failed, deleted or concurrently claimed ones. No workload starts while a binding is
pending (`IdentityMissing`), and none with a default or unverified identity.

SVID mTLS authenticates both Environment Runtime control and Sandbox egress through the
incarnation's `WorkloadCredential`, so binding before start keeps one scheme and a running
incarnation never changes identity. Coordinate with `ENVIRONMENT_SHARED_IDENTITY`,
`SESSION_MANAGED_IDENTITY`, `SESSION_IDENTITY_ENFORCEMENT` and `SESSION_ATTACHMENT_CONTRACT`;
central zero-Environment creation need not wait for this native flow.

**Exit:** a reviewed creation and recovery sequence covering new and already-bound Environments,
existing and Session-scoped Identities, authorized concurrent claims, Identity replacement and
partial failure.

### `SESSION_IDENTITY_PROVISIONING_DESIGN` — Session-scoped identities

**Deferred decision; wanted, design not selected (operator, 2026-11-10).** From the app a user can
ask for a Session with a fresh `Identity` used only by it. Decide:

- **Creator.** Dispatcher at `CreateSession`, the app at launch, or the app's path collapsed into
  Dispatcher's ([open questions 7 and 11](identity_and_kubernetes_accounts.md#open-questions-and-tricky-bits)).
- **Lifetime and cleanup.** Whether the Identity is deleted with its Session (explicit Session
  deletion does not exist yet), retired with it, or kept while an Environment or Connection still
  names it; how a leaked one is found (the app's unreferenced filter) and removed; what a retained
  Session's History shows once it is gone.
- **Policy.** Where the launch PolicySets and later grants bind, owned by the Identity so they go
  with it.
- **Kubernetes account.** None by default: an Identity reaches the Kubernetes API only through an
  account a use rule names. Decide whether a Session-scoped Identity uses shared accounts, gets a
  temporary managed account on approval, or needs neither. Agentplane calls need no Kubernetes
  object: egress and services authenticate the SVID's Identity, and the sampler acts for a central
  Session under [Dispatcher's Session-to-Identity binding](../sampler/SPEC.md#identity-and-admission).
- **Scale.** The operator expects fewer than 10,000 Sessions in existence; one Identity and
  `PolicyBinding` each is a working assumption, not a measured limit. Measure object counts before
  claiming a control-plane cost.

Materialize no credential or policy an operation does not need, while preserving unattended work
and predictable readiness. Define creation/deletion races, recreation, intentional revocation, stale
bindings, retained audit identity and unknown outcomes.

**Exit:** a reviewed creator and lifetime policy with examples for a central Session and a native
Environment, covering authorization, failure and retry, and cleanup. It gates
`SESSION_MANAGED_IDENTITY`, not unrelated native work.

### `SESSION_MANAGED_IDENTITY` — provision Session-scoped identities

**Deferred; not required for Session/Environment separation or permission requests.** After
`SESSION_IDENTITY_CONTRACT` and `SESSION_IDENTITY_PROVISIONING_DESIGN`,
have the chosen owner durably reconcile a Session-scoped `Identity` and its owned `PolicyBinding`,
including the web preset and editable PolicySet workflow, and stamp the origin marker
`APP_IDENTITIES_UI` reads. Keep the existing-Identity path without taking ownership or adding grants.
Persist the exact Identity, desired policy and provisioning progress, and recover retries and
uncertain outcomes without adopting foreign objects or reusing an old incarnation's evidence. Do not
rely on a Kubernetes ownerReference to a PostgreSQL Session. Coordinate central-sampler delegation
and Session deletion with their contracts; fail closed where they are not implemented.

**Exit:** generated-facade create/retry/status coverage for existing and Session-scoped Identities,
permissions and denials, PolicySet changes, partial creation, UID fencing and reviewed cleanup. No
new service or binary is required by this node.

### `SESSION_NATIVE_IDENTITY_WORKFLOW` — create native Sessions in new Environments with an existing Identity

**Blocked on `SESSION_IDENTITY_CONTRACT`, `ENVIRONMENT_SHARED_IDENTITY` and
`SESSION_IDENTITY_ENFORCEMENT`.** Implement the reviewed native flow that creates a new Environment
acting as an authorized existing `Identity` across Dispatcher, Environment Service, Sandbox
Controller and app. Keep an exact Environment in the Session placement, whose SVID-verified Identity
carries workload and egress authority. An already bound Environment must match the Session's
Identity; never silently replace it or broaden grants. Reusing an already bound Environment does not
need `ENVIRONMENT_SHARED_IDENTITY`. The unbound-Environment sequence follows
`ENVIRONMENT_IDENTITY_BINDING_DESIGN`, and Session-scoped Identities are `SESSION_MANAGED_IDENTITY`.

**Exit:** end-to-end generated-facade creation, retry and status with an existing Identity, with
mismatch, concurrent binding and failure recovery coverage. Preserve preset PolicySet workflows
without treating a grant to a shared Identity as isolated to one Session.

### `SESSION_ACCESS_GROUPING_DESIGN` — access groups across independent Sessions and Environments

**Decision; coordinate with Session identity, attachment and lifetime reviews.** Inbox tags today are
Sandbox attributes used by Notification to authorize sends. History tags and a retained per-Sandbox
history record are proposed, not implemented. Reconsider whether either mechanism should instead
group Sessions, inboxes, identities or another resource, or be replaced; choose authority and storage
for membership and grants separately. Reconsider the older Sandbox-only orchestration authority for
Session commands, interrupts and reads as well as inbox/history grouping, without choosing a new
policy target prematurely; coordinate with `DELIVERY_DESTINATION_AUTHORITY_DESIGN` and
`ORCHESTRATION_V1_BACKEND_GRANTS`. Address zero, one and multiple Environment attachments,
attachment changes and Environment deletion, retained history and inboxes, identity loss and
replacement, edits and revocation. Distinguish Session access from Environment access and preserve
independent send, history read, listing, inbox read/ack and Environment privileges. Co-resident
Sessions remain in the same Environment trust domain regardless of narrower nominal grants.

Existing Sandbox-tag enforcement remains authoritative until a reviewed replacement is implemented;
never silently broaden grants or lose retained data or accepted messages in migration. A draft can
proceed alongside the identity, attachment and lifetime decisions, but the final cross-principal
contract must be consistent with them. Same-principal independent-Session work need not wait for
this wider policy design. Reopen the held History read policy only when the operator chooses.

**Exit:** reviewed access and revocation examples for a Session without an Environment, a shared
Environment, changed or deleted attachments and retained history, including migration and ownership.
The decision may retire or reshape `HISTORY_SANDBOX_RECORD` rather than require it.

### `SESSION_ENVIRONMENT_SETUP_DESIGN` — choose where setup belongs

**For now, setup stays per Session; this decision must be revisited before a central Session can run
with no Environment or with several attachments.** Operator, 2026-11-17 06:28 UTC: "the conservative
thing would be to keep setup per session for now if we can, but we should revisit it before central
sampler can have detached sessions / multiple attachment". Earlier, 06:22: "it would become maybe
setup state on a _attached sandbox_, or we might just get rid of the session / sandbox setup script
feature. it might be easier, we would need to think about it - there are tradeoffs." That is why this
node is a hard prerequisite of `CENTRAL_SESSION_WITHOUT_ENVIRONMENT` and `SESSION_ENV_ATTACHMENTS`.
Until then, the [HarnessHost Session status](../harness_host/README.md#session-status) reports setup on the
host side of each Session.

Facts on devel, 2026-11-17:

- `setup_script` is a per-Session `CreateSession` field. It is supplied by the App's new-Thread form
  and Thread presets, the Actions `create_session` and delegation inputs, and the CLI.
- The Runtime runs it once per Session, after Sandbox bootstrap, in the Session's cwd, and starts the
  harness only after it succeeds. Two Sessions on one Sandbox each run their own script.
- The sampler runs it through the Executor on the executor Sandbox. A failure or interruption blocks
  that Session for good, because setup is never replayed.
- The Sandbox already has its own once-per-Sandbox `bootstrap`.
- Three staging Thread presets clone a repository into a per-Session workspace this way: Haku,
  finance-agent and the ducktape coder. Shared worker Sandboxes rely on the same per-Session clones.

The options:

- **Keep it per Session.** This is the current choice.
- **Make it per (Session, attached Sandbox).** This only means something once attachments can change
  after creation.
- **Drop it.** Removing it deletes the field from both `CreateSession`s, the Dispatcher record and
  every input. It also removes the Runtime setup job, four journal events, the sampler's setup path and
  block, and the App's setup UI. Clones would move to Sandbox bootstrap, giving one checkout shared by
  all Sessions, or to the agent's first turn. That loses isolated per-Session checkouts on shared
  Sandboxes and the structured never-replayed failure.

**Earlier operator lean, 2026-11-04:** the current Session `setup_script` may belong to the
Environment instead. Compare Environment creation/bootstrap, an explicitly scoped attachment or
workspace setup operation, and any narrowly justified Session-specific setup. Do not make a
zero-Environment Session run an Environment script or silently choose an attachment. Define behavior
for zero, one and multiple Environments, later attach/detach, shared Environments, initial versus
per-Session workspaces, readiness/failure visibility and authorized callers. Existing delegation uses
Session setup to prepare a child workspace when the caller lacks direct execution; preserve that use
case through an explicitly authorized operation if the field moves or is removed.

Coordinate with `CENTRAL_SESSION_CREATION_CONTRACT`, `SESSION_ATTACHMENT_CONTRACT` and Environment
creation/bootstrap ownership. Specify script/data ownership, when and how often effects may run,
idempotency and uncertain-outcome recovery, and how existing stored/pending setup requests are handled;
never replay an uncertain external effect or discard retained scripts silently. The decision gates
changing setup semantics, not provider-only central Sessions with no setup or unrelated native work.

**Exit:** a reviewed owner/API and examples for no Environment, a later attachment, multiple targets
and a shared Environment, including caller authority, failure/retry and migration of retained work.

### `CENTRAL_SESSION_CREATION_CONTRACT` — opening a central Session without an Environment

**Decision; drafted for review in [#15436](https://github.com/agentydragon/ducktape/pull/15436)
(central-session-design, 2026-11-10): [creation contract](https://github.com/agentydragon/ducktape/blob/448cd6eafce4427af1eb3baa801bc318578d4daa/agentplane/plans/central_session_contract.md#1-central_session_creation_contract-opening-with-zero-environments).**
Recommended there: a new `central_sampler {}` placement variant with the Identity named in
`CreateSessionRequest`; `setup_script` and `cwd` rejected at creation and moved to attachments;
`central_sampler_v1` retired in the same cutover, each executor becoming the Session's first
attachment; central openings adopt the native opening-outcome contract. Identity callers still
cannot create a Session; child-Session authority remains under `SESSION_SUBAGENT_DELEGATION`.
Waits on the operator's answer to that section's question. Define the smallest zero-Environment creation and command contract consistent with
the chosen Session UUID and Dispatcher-owned logical reservation: central owner/placement, immutable
request and retry identity, owner unavailable or materialization rejected, and observable progress.
Decide how optional setup, cwd and Environment-dependent instructions behave without an executor;
do not silently run setup elsewhere or make an Environment a condition for provider-only work.
Map authenticated Session identity, command authorization and History ingress to the selected owner.
Coordinate setup semantics with `SESSION_ENVIRONMENT_SETUP_DESIGN`; do not make provider-only Sessions
wait for an Environment. Coordinate with `SESSION_IDENTITY_CONTRACT`; either decision can be drafted independently, but both
are prerequisites for implementing the complete zero-Environment path. Multi-attachment semantics
belong to `SESSION_ATTACHMENT_CONTRACT`, not a prerequisite for this decision.

**Exit:** reviewed API and state examples for create, retry, send and History observation while no
Environment exists, with exact acceptance and unknown-outcome boundaries.

### `SESSION_ATTACHMENT_CONTRACT` — Session-to-Environment relationships

**Decision; drafted for review in [#15436](https://github.com/agentydragon/ducktape/pull/15436)
(central-session-design, 2026-11-10): [attachment contract](https://github.com/agentydragon/ducktape/blob/448cd6eafce4427af1eb3baa801bc318578d4daa/agentplane/plans/central_session_contract.md#2-session_attachment_contract-environments-a-session-may-use).**
Recommended there: Dispatcher-owned desired attachments (`AttachEnvironment`/`DetachEnvironment` on
SessionDispatcher, caller-chosen attachment ID, Session-local name, exact target UID, optional cwd,
desired versus observed state, a per-Session revision and a `session_attachment_transition` table as
the History handoff source), applied by the sampler and fenced per executor call by the Gateway's
UID, incarnation and Identity checks; `bind_session` on the target is the native attach grant, detach
needs only Session write authority, attachment is not a grant, and nothing wakes an attachment
implicitly; native placement stays unattachable. Waits on the operator's answer to that section's
question. Define native required placement
versus central zero-or-more attachments; who may attach, detach, inspect, execute and wake; and
whether a default target is allowed. Decide attachment identity, exact Environment UID, revision,
desired versus observed status, operation targeting and incarnation fencing, concurrent
attach/detach, replacement, unavailable targets and in-flight effects. Enforce the shared-identity
invariant from `SESSION_IDENTITY_CONTRACT`; do not treat an attachment or source subscription as a
grant. Define an authoritative transition record and recoverable handoff for the separately tracked
History work. Reconcile any setup during attachment with `SESSION_ENVIRONMENT_SETUP_DESIGN`; avoid
implicitly running a Session script on every target. A draft may proceed alongside identity review;
implementation waits for both decisions.

**Exit:** reviewed zero, one and multiple attachment examples, including denial, race/retry and
Environment loss. Choose no service relocation or automatic reattachment by this decision alone.

### `SESSION_IDENTITY_ENFORCEMENT` — enforce one identity per shared Environment

**Blocked on `SESSION_IDENTITY_CONTRACT`.** Apply the reviewed invariant to existing native
Session admission and provide an atomic identity gate for every path that permits Session use of an
Environment. Verify the Session's Identity and the workload's, as its SVID resolves it; serialize or fence concurrent
admissions and identity changes so different identities cannot both pass. Cover direct, delegated
and privileged callers, named Identities and their incarnations, exact Environment UIDs,
revocation and already accepted work.
A privileged caller must not accidentally bypass the trust-domain rule.

**Exit:** automated coverage of matching Sessions sharing an Environment, mismatched admission,
concurrent races, Identity replacement and policy change. The central attachment endpoint does not
yet need to exist: `SESSION_ENV_ATTACHMENTS` must integrate and test this gate before shipping it.

<a id="central-session-environment-independence"></a>

### `CENTRAL_SESSION_WITHOUT_ENVIRONMENT` — central Session without an Environment

**Blocked on `SESSION_IDENTITY_CONTRACT`, `CENTRAL_SESSION_CREATION_CONTRACT`,
`SESSION_ATTACHMENT_CONTRACT`, `SESSION_ENVIRONMENT_SETUP_DESIGN` and
`SESSION_AND_INBOX_LIFETIME`.** The central principal is the Session's `Identity`, named at
`CreateSession`; Dispatcher currently derives a central Session's Identity from its executor
Sandbox, and an environment-free Session needs a different authorized creation path. An SA-keyed
principal here would undo the completed workload cutover. Whether the named Identity must exist or may be minted for the Session is
`SESSION_IDENTITY_PROVISIONING_DESIGN`. Before implementation, settle who can create,
read or send to a central Session without an Environment; Dispatcher logical admission and owner materialization when no
executor exists; whether optional zero-to-many attachments ship in this cutover or later; notification
subscription, pending inbox and authorized wake behavior without an attachment; and the remaining
inbox and History behavior when an Environment is deleted. PR #14314 already retains the Dispatcher
Session record and exact required-environment placement for current central V1; it does not provide
an alternate executor or environment-free admission. Coordinate notification semantics with
`CENTRAL_SESSION_NOTIFICATIONS` and attachment implementation with `SESSION_ENV_ATTACHMENTS`.
Then implement the reviewed zero-Environment create, resolve, command and provider-only execution
path through Dispatcher and central Sampler. Today both creation requests require a Sandbox/executor;
the sampler also binds executor identity, setup and stored configuration to it, and History's sampler
ingress requires an executor. Update the relevant producers, consumers, History ingress and retained
data in the same coherent contract cutover. A Session can be logically committed with no Environment;
it must accept only explicitly authorized work, and commit never proves owner materialization or
model/tool effects.

An Environment-dependent operation without a target must report an actionable unavailable or
unsupported outcome, not invent an Environment, reuse an old target or replay an uncertain effect.
Preserve stable IDs, frozen prompt semantics, retained native/central data, source attribution, History
ordering and existing native behavior. Migrate retained central and History data in place; never
delete and recreate it, replay provider/tool effects, or discard unconfirmed outcomes during cutover.
Exact protobuf, storage and binary boundaries follow the reviewed contract; the optional service/database
reshuffle is not a prerequisite.

**Exit:** end-to-end create, retry, send, provider-only continuation and History reads with zero
Environments; owner-unavailable, missing-target, authorization and restart/unknown-outcome coverage.
No full multi-attachment UI or broader identity-provider integration is required.

### `SESSION_ENV_ATTACHMENTS` — attach and detach Environments

**Blocked on `SESSION_ATTACHMENT_CONTRACT`, `SESSION_IDENTITY_ENFORCEMENT`, and
`CENTRAL_SESSION_WITHOUT_ENVIRONMENT`.** An attached
environment's tools act with its SVID, so attachment is sound only once that SVID resolves to
`Sandbox.spec.identity` and admission compares it with the Session's `Identity`. Implement the reviewed central Session relationship for
zero, one and multiple exact Environments, including explicit targeting, durable desired state,
status, attachment revision and identity/authorization checks. Detaching the last Environment
requires the zero-Environment path; native harness placement remains a distinct required constraint.
Preserve exact-incarnation fencing, authorized wake, independent failure of one target, and
unconfirmed effects on disconnect or detach. Do not silently retarget work to a replacement.

Durably record enough attachment-transition evidence for `SESSION_ATTACHMENT_HISTORY` to recover
later; that History delivery and full UI are independent follow-ups. Cover attach/detach retry,
concurrent changes, identity mismatch, target deletion/recreation and in-flight work.

### `SESSION_ATTACHMENT_SURFACES` — use attachments from agents, CLI and app

**Blocked on `SESSION_ENV_ATTACHMENTS`.** Expose the reviewed discovery, attach/detach, target
selection and status through the owning service's generated contracts, Agentplane tools and CLI,
and the app/frontend where relevant. A Session with no Environment remains visible and usable for
supported work; per-target failures and unsupported native attachment operations are explicit.
Preserve authentication and avoid an app-only backend dependency. Exercise an ordinary central
workflow with two Environments and a native workflow with its required placement; no attachment
must be inferred from a mutable name or made a prerequisite for Session reads.

### `SESSION_ATTACHMENT_HISTORY` — record attachment transitions in History

**Blocked on the reviewed and implemented `SESSION_ENV_ATTACHMENTS` transition source.** Deliver
time-qualified attachment and detachment evidence to History with a recoverable handoff, retaining
exact identities, actor attribution and historical read authority. History remains independently
readable if the live Session or Environment is absent. Test retries, delayed delivery, deletion and
replay without inventing cross-store atomicity. This is the operator-requested eventual correctness
work, not a prerequisite for the initial zero-Environment Session path or attachment UI.

### `SESSION_LIFETIME_IMPLEMENTATION` — implement reviewed Session and inbox lifetimes

**Blocked on `SESSION_AND_INBOX_LIFETIME`; an independent-principal implementation also needs
`SESSION_IDENTITY_CONTRACT`.** Dispatcher Session retention after required-Environment deletion is
implemented in #14314. Implement the remaining operator access after account disappearance and
coordinated inbox, History and receipt behavior at their authoritative owners. Do not silently delete
authored or unread data, invent automatic reattachment or equate app archive with Session deletion.
If explicit deletion disposes of command receipts, implement the reviewed retention and bounded
reconnect contract before removing them. A native-only safe slice need not wait for central multi-attachment
support; central lifecycle changes use the central no-Environment path. Test lost replies, deletion
races, inaccessible principals and same-name replacements.

### `CENTRAL_SESSION_NOTIFICATIONS` — inboxes for Sessions without Environments

**Blocked on `SESSION_AND_INBOX_LIFETIME`, `SESSION_IDENTITY_CONTRACT`,
and `CENTRAL_SESSION_WITHOUT_ENVIRONMENT`.** Make subscription, inbox and notice authorization and
delivery work for a central Session with no Environment, under the reviewed identity and lifetime
contracts. `CENTRAL_SESSION_INPUT_METADATA` separately carries authorized annotations through the
sampler to History when that feature is implemented. Cross-principal grouping additionally uses `SESSION_ACCESS_GROUPING_DESIGN`; a safe
same-principal slice can proceed independently. The Notification worker checks the Session's recorded
Identity and complete placement, then submits by Session ID; an unplaced Session's notice remains
pending and retries. Do not fabricate a Sandbox or silently retire a live Session's inbox because an
attachment is absent. Preserve pending messages, idempotency, source authorization, explicit acknowledgement
and unattended wake where an actual Environment must be woken. Update affected backend, app and
frontend consumers together; test no-Environment delivery, attachment loss and reconnect.

**Operator clarification, 2026-11-15:** Notifications must not require a Sandbox or an attachment
for a Session to subscribe, own/read/acknowledge its inbox or receive notifications. Removing the
Sandbox checks for subscription/access and keying inboxes by Session are implemented
([Notification Service](../notification_service/README.md)); this node keeps what a Session with no
Environment adds. No fabricated Sandbox, arbitrary first attachment or new single-attachment requirement.
Environment UID fences remain where a native placement or explicitly scoped operation needs them;
they must not become mandatory for central Session notifications. **Acceptance:** a provider-only
central Session with zero attachments subscribes, collects events, reads/acknowledges its inbox and
receives a delivered notice across restart; detaching its last Environment does not retire the
inbox or block those operations. Preserve wrong-Identity and stale-Identity denial.

### `CNPG_FREE_K3S_INTEGRATION_TIER` — add a narrow database-independent integration tier

**Blocked on scoping flows that actually have no execution-database dependency.** Add a K3s integration tier for
independently usable, loosely coupled Agentplane components; it is not a full-stack parity
environment or a replacement for CNPG. Exercise only flows that can run without the removed
dependencies. The tier may omit Notifications, History, Electric, the CNPG operator and PostgreSQL,
without requiring those absent services' behavior in this suite or implying that every other service
is database-free.

Cover Pod and VM runner flows, adding test ingress where a selected flow needs it; VM flows use the
k3s stack's VM rendering (KubeVirt under emulation, the Bazel-built test guest). Keep this test target separate from runtime database removal;
scope its included flows, omitted services and remaining state owners explicitly. The mandatory
Dispatcher database means the tier cannot exercise Dispatcher admission without PostgreSQL; select
independently usable direct Runtime or other flows and retain their real authorization contracts.

The existing `agentplane/system_tests/k3s` suite already runs without the CNPG operator, Cilium or cert-manager (and KubeVirt outside its VM rendering),
against one plain PostgreSQL Deployment and the Sessions and Environments services. This node is about removing the PostgreSQL dependency
itself. New sender grants remain a separate decision.

### `DELIVERY_DESTINATION_AUTHORITY_DESIGN` — assign Session send authority and internal fences

**Decision: extend send authority beyond the implemented Session-ID path.** Dispatcher owns
Session placement and command admission. It authorizes direct callers and delegated effective
Identities and keeps separately scoped grants for the App, Notifications and Actions. Wake has its
own authority. The Environments proxy grants native Session mutations to the Sessions service SVID
and fences the recorded Sandbox destination against the exact current workload and Runtime boot.
See the [Dispatcher method rules](../session_dispatcher/dispatcher/SPEC.md#method-scope),
[managed deletion and receipt behavior](../session_dispatcher/dispatcher/SPEC.md#possible-command-forwarding-and-deletion-outcomes),
and [Runtime proxy contract](../environments/runtimeproxy/SPEC.md#environment-runtime-proxy-authorization).

The [Session command authority plan](session_send_authority.md) tracks how to extend that boundary.
Compare source-side checks, centralized admission, and layered enforcement. Define Session read,
send and wake grants separately. Preserve actor and effective caller, prove authority at admission,
and persist the exact placement before acceptance or wake. If placement can change, the receiver
must validate a generation or equivalent fence before admitting delayed work. A subscription grants
source access, and its scope does not decide command send or wake authority. Generalized sender
grants and queued-work revocation are tracked by `DISPATCHER_SEND_AUTHORIZATION_GENERALIZE`.

**Exit:** a reviewed authority and race contract for `DELIVERY_DESTINATION_CONTRACT`. Cover
unavailable placement, same-name replacement, queued work during revocation or deletion, and
recovery after an unknown outcome. Central Session authority remains a separate decision.

### `COMMAND_ADMISSION_REFUSAL_DESIGN` — distinguish refusal from unknown admission

**Decision; unscheduled.** Dispatcher currently treats a terminal error from a delivery attempt as
`UNKNOWN`: the error may describe that attempt while an earlier attempt with the same command ID
could still be admitted. The November 2026 staging model-switch incident produced ten such receipts;
the sampler journal and traces showed those particular commands were refused before admission, but
the stored RPC errors alone do not establish that for arbitrary commands.

**Recommendation for review:** let the admission authority durably decide admission or refusal for
the exact Session, command ID and original command. Under its admission lock, return an existing
admission or refusal before evaluating current policy; reject the same ID with different content.
Persist a terminal refusal before returning a typed non-OK RPC detail, so old Dispatcher clients
remain conservative. A retry after configuration repair uses a new command ID. Start with selected
central-sampler validation failures; decide separately which native Runtime refusals have equivalent
journal authority. Keep transient errors, ambiguous commits and bare RPC failures unknown.

The decision must fence delayed and mixed-version writers: a refusal is not proof if an earlier
request can later admit the ID. Review storage enforcement and rollback, decision retention, and how
Dispatcher establishes continuity of the original admission ledger across placement changes or
restores. Preserve an existing admission and distinguish journal acceptance from completed effects.
For historical `UNKNOWN` receipts, review reconciliation using exact positive evidence and, if
appropriate, an atomic no-delivery closure against late arrivals; a missing record or error string
alone is not proof. Preserve the original reason and condition any receipt update on its revision.

**Question for review:** adopt durable per-command refusals and the proposed typed error contract,
or choose another receiver-owned mechanism that provides the same late-arrival and rollback proof?
**Exit:** a reviewed contract and implementation/rollout boundaries; cover the race where a refusal
wins before a delayed earlier attempt, the reverse admission-wins case, lost replies, conflicting
payloads, ambiguous commits, mixed writers and changed admission authority.

### `DELIVERY_DESTINATION_CONTRACT` — send commands to a Session ID

**In progress.** Dispatcher submit/model/effort/interrupt requests address the Session by UUID;
Dispatcher resolves placement and retains exact UID fencing in accepted work and execution. App and CLI
commands and Notification's notice submissions use Session ID alone. Session Actions still pass an
expected Sandbox UID for their existing grants. Accepting a new inbox message still checks the target
Sandbox for Notification's inbox-send policy. Environment lifecycle and execution APIs name their
actual Environment.
Preserve the retained receipt and exact-incarnation fence across Environment deletion.

Keep command IDs, no-replay recovery, sender/receiver and wake authorization, and explicit unknown,
failed and suspended outcomes. Cover unavailable owners, same-name replacement, concurrent deletion,
conflicting retries and callers with distinct privileges. Follow the reviewed
[command authority](session_send_authority.md), including queue-acceptance versus Runtime-admission
semantics and the enforced Gateway proxy boundary. Review remaining Action-facing grant/address contracts and rollout; retain History and actual harness
stores. This does not decide central attachment policy.
[Address inventory](session_environment_model.md#session-address-inventory).

### `SESSION_AND_INBOX_LIFETIME` — decide Session and inbox lifetimes

Managed-agent edits, removal, environment loss and recovery are consolidated in
[persistent agents](persistent_agents.md#authority-wake-and-lifetime); the wider central/deletion
decision below retains its existing owner and gate.

**Decision; Dispatcher row retention (#14314) and Notification's inbox retention after a missing or
replaced Sandbox are implemented; the rest is drafted for review in
[#15436](https://github.com/agentydragon/ducktape/pull/15436) (central-session-design, 2026-11-10):
[lifetime contract](https://github.com/agentydragon/ducktape/blob/448cd6eafce4427af1eb3baa801bc318578d4daa/agentplane/plans/central_session_contract.md#3-session_and_inbox_lifetime-what-outlives-an-environment-and-explicit-deletion).**
Recommended there: retain inbox, subscriptions and Session row until an explicit `DeleteSession` on
SessionDispatcher, scoped first to central Sessions and native Sessions whose required Environment is
deleted (the sampler seals and exports before dropping; Notification retires the inbox on
`NOT_FOUND` and keeps entries operator-readable under existing retention; History is never deleted);
central notices deliver through `SubmitInput` with zero attachments; a Session stranded by a
same-name Identity replacement stays operator-only. Decide command-receipt retention for explicit
Session deletion in the same review. Waits on the operator's answer to that section's question.
[#14314](https://github.com/agentydragon/ducktape/pull/14314)
retains the Dispatcher Session row and exact placement for native and current central V1 Sessions
after the required Sandbox UID is deleted. Cleanup tombstones the deleted UID and exposes
`required_environment=DELETED` separately from materialization: `WAITING` becomes `FAILED`,
`IN_PROGRESS` becomes `UNKNOWN`, and accepted or terminal evidence remains. A replacement cannot
receive the retained Session's work. Central V1 still requires its exact executor Environment;
retaining its Session row does not provide environment-free admission or an alternate target.
Notification's worker keeps the inbox and subscriptions live while its Dispatcher Session is missing,
unplaced or owned by another Identity, and retries on `unavailable_destination_retry_s`; it addresses the
current Session by ID with no exact-UID wait, and explicit retirement stays an API call.
Those transitions are not a cross-service transaction. The central Session lifetime direction is indefinite, and app archive is separate. The
selected Dispatcher identity policy is to forget a Session only upon a future explicit Session
deletion; that API is not implemented. The treatment of other services' data remains open.

#15444 treats every retained Session naming an Identity as a cleanup reference, including one on
the deleted Sandbox; #15445 refuses a rebind when any retained Session on that Sandbox names a
different Identity incarnation. Without a terminal Session projection or deletion API these checks
can conservatively prevent cleanup or rebinding. Settle which retained Sessions still constrain
each action and how they are retired in this lifetime decision and `SESSION_LIFETIME_IMPLEMENTATION`;
do not treat app archive as finish or discard retained History to unblock either action.

The selected remaining policy is to keep the inbox and subscriptions until explicit Session
deletion, even when a required Environment disappears, which Notification now does. Do not silently bind a
replacement or claim lost native state is recoverable. The intended inbox behavior is to keep
collecting authorized events and messages while delivery is unavailable and preserve unread contents,
as implemented for the current UID-pinned destination. Revisit sustained collection cost only if
evidence warrants it. App archive remains separate. Explicit Session deletion is not implemented; whether it deletes
inbox contents or History remains deferred, and inbox/subscription behavior after deletion needs
design.

**Operator follow-up, 2026-11-21: resolve stranded notifications.** A retained native Session whose
required Sandbox is deleted can keep an active inbox and subscriptions while notice delivery retries
indefinitely; [#18323](https://github.com/agentydragon/ducktape/pull/18323) explains the failure in
the app but does not resolve it. Reconsider the selected retention and collection policy above:
investigate whether Notification should consult SessionDispatcher and stop notice attempts, cancel
subscriptions, or take another action when the Session is gone or cannot run its harness. Distinguish
confirmed deletion or permanent placement loss from temporary unavailability, and define authority,
recovery, races and operator visibility before changing behavior. Preserve unread entries and History;
do not silently discard data or treat app archive as deletion. A central sampler Session can eventually
run and receive notices with no Sandbox attached, so missing attachment alone must not disable its
inbox or subscriptions. Coordinate the decision with `SESSION_LIFETIME_IMPLEMENTATION` and
`CENTRAL_SESSION_NOTIFICATIONS`, and with the proposed lifetime contract in #15436.

No cleanup path for a stranded inbox, 2026-11-26: `RetireInbox` admits only the inbox owner (the
Session's Sandbox Identity) and the operator-reader services are read-only, so once the owner is
gone nothing short of a database edit stops the retries. Staging then had two inboxes
(`94515635-d0eb-4f72-a702-9dcf74eeb8d7`, `778b996c-273a-4de7-966f-7ff99b622ba9`) logging
`DispatcherNotFoundError` every retry interval. The resolution needs an operator-authorized retire,
or retirement driven by confirmed Session deletion; it is one of the cross-service ownership seams in
Identity and agent teardown (#20202).

While a Session's recorded `Identity` is absent, the operator's direction is human-operator-only
inspection and administration through an operator-authenticated entry point, such as the app, and
explicitly privileged service credentials. Under the selected name-based principal model, an
authorized same-name replacement is the same principal; its new UID must not inherit stale
credentials or in-flight operation fences. Current implemented UID-pinned access remains until the
coordinated contract cutover.
Define the precise backend permissions and narrowly scoped service reconciliation separately.
Decide the owning signals and reconciliation, pending messages and subscriptions, History handoff,
and explicit-deletion, reattachment and same-name-replacement races. Preserve exact UID fences,
unread data, no-replay recovery and authorized unattended wake; suspension and temporary
unavailability are not Session deletion. Environment deletion already preserves accepted commands
and receipts. Decide what command evidence remains readable after explicit Session deletion and how
a bounded watch reconnects if receipts expire. Coordinate data ownership and store boundaries with
`SESSION_DATA_OWNERSHIP_DESIGN`. See the [Session and inbox lifetime discussion](session_environment_model.md#decision-session-and-inbox-lifetimes-after-environment-deletion).

**Exit:** a reviewed lifecycle and authority contract for both Session kinds, with owners and
cross-service failure behavior identified before implementing central Session inbox lifetime changes.
This decision does not block independently safe native-only addressing cleanup.

### `SESSION_HISTORY_PRESENTATION` — represent Sessions without a required Sandbox

**Zero-Environment presentation follows `CENTRAL_SESSION_WITHOUT_ENVIRONMENT`; multiple-attachment
presentation also needs `SESSION_ENV_ATTACHMENTS`.** History's direct Session reads already use
Session ID. Retain exact Environment references as provenance, filters and access-control evidence.
Adapt summary/projection, app Thread views, grouping, readiness controls and Environment-filtered
lists for central Sessions without an Environment, then for multiple attachments. Historical reads
must not depend on a live directory. `SESSION_ATTACHMENT_HISTORY` separately tracks time-qualified
attachment transitions and their recoverable handoff; its completion does not gate the first UI.
Preserve retained history, UX visibility and exact UID attribution. The
[app's listing follow-up](../app/TODO.md#session-and-environment-listings) covers bounded pagination
and archived counts under that model. [Address inventory](session_environment_model.md#session-address-inventory).

### Service API transport tracks

One track per service. Tracks are independent except where an edge is drawn, and graph position
claims no priority. Each migration keeps its own caller inventory, authorization and
behavior-preservation review, tests, rollout and rollback plan; none is a repo-wide migration. The
contract is [HTTP/JSON facades](../docs/grpc_services.md#httpjson-facades); what is still open is in
[generated HTTP facades](grpc_http_facade.md). The order of the ports belongs to the Go rewrite.

```mermaid
flowchart LR
    APP_API_GRPC_FACADE[Selected, unscheduled: uniform app gRPC APIs with generated HTTP facades]
    WEB_PUSH_SERVICE[Decision, unscheduled: Web Push as its own sender]
```

### `APP_API_GRPC_FACADE` — uniform app gRPC APIs with generated HTTP facades

**Selected by the operator, 2026-10-31; unscheduled.** Migrate the app-owned browser APIs from
handwritten HTTP contracts to protobuf-defined gRPC services with generated HTTP/JSON facades.
Generate TypeScript request/response messages and codecs from the same protos; remove the replaced
OpenAPI-derived contracts, mirrored wire types and handwritten route implementations as callers move.
UI-specific view models and adapters may remain.

[AppService](../app/app_service.proto) owns the typed contracts for frontend configuration, System
status, Sandbox form reads, Thread metadata, command-notice acknowledgements and Session creation.
Sandbox policy grant/revoke uses the Policy facade, and runner diagnostics use the Environment facade.
Inventory the remaining app routes, including the typed Thread metadata watch's HTTP adapter and the
Sandbox console stream. Electric synchronization, login/OAuth redirects, static assets, health
probes and OTLP telemetry use protocol-specific routes.

Session-create follow-ups: type the override map while preserving omitted, null and empty semantics.
The server uses grpc-gateway; the browser still spells out the URL. See
[generated HTTP facades](grpc_http_facade.md#open).

**Prerequisites:** no dependency on a service rename.
Before each independently reviewable migration, identify the owning service and preserve the app's
client-only boundary; backend responsibilities still in the app belong with their backend authority.
The app serves the human frontend; agents and services call owning backends through native gRPC.
For each remaining wrapper, explicitly retain, move or remove its checks according to ownership.
Historical outcome reconciliation does not require a History preflight on command submission.
Preserve authorization, CSRF at the browser proxy,
error semantics, client-chosen Session identity and unknown-outcome recovery, and stream
retry/reconnect behavior. Keep the current same-origin app proxy; direct browser access and
per-operator delegated attribution remain separate decisions.

**Exit:** scoped application RPCs use generated gRPC/HTTP contracts and generated TypeScript codecs;
replaced route/type-generation paths are removed, and remaining protocol-specific HTTP surfaces are
listed with reasons. Focused API/browser tests cover ordinary calls, authorization denials,
idempotency and ambiguous outcomes, and stream reconnects. Each migration records its rollout and
rollback; update the app's contract documentation as behavior changes.

### `WEB_PUSH_SERVICE` — Web Push as its own sender

**Decision, unscheduled (operator, 2026-10-20 07:41 UTC: "eventually we might want to think about moving it out. It
just so happens that right now agentplane only sends web push notifications about actions that need approval. But I
can foresee wanting to send other web pushes").** Web Push delivery (operator subscriptions, VAPID settings, the sender)
lives in the Action Service because an Action awaiting approval is the only message today. It does not belong to the
Notification Service, which serves agent Session inboxes, a different system. If other messages come, a Web Push
sender of its own would own the subscriptions and take messages from their producers. The sender touches only its
own tables and the request event stream, so the move is clean. This remains a separate decision;
the MCP edge extraction leaves Web Push in Actions.

### `HARNESS_IDENTITY_DESIGN` — decide private Session-to-store and native mapping

**Current destination and process boundary (operator direction updated 2026-11-02).** Dispatcher
command requests address a globally unique Session UUID and resolve placement; broker APIs may
still carry an exact Sandbox target as an authorization precondition. Opening still names an exact
Sandbox. See the [Session/environment model](session_environment_model.md). Runtime retains Session
and native identity without exposing a store field; selecting a default store by requested harness
kind remains a candidate policy.
Process allocation/routing stays internal; there is no create-Session-on-process API or refusal.
Process incarnations may be diagnostic objects, never durable addresses. Neither the number of
stores/processes per Runner, at-most-one process per store nor shared-store concurrent safety is
selected.

**Remaining native mapping decision:** use the globally unique public Session UUID with an immutable
required Sandbox placement for a native Session and a private selected harness store. Reserve the public ID durably before native creation. When native creation occurs, record the
harness-chosen native ID before reporting native creation as complete; the earlier logical opening
does not require native creation. An identical retry resolves by public ID, while reuse against a
different destination/spec conflicts. A queued opening is not proof of native creation. If native
creation succeeds but its ID is lost before mapping commits, inspect native store evidence or a
proven harness idempotency mechanism; if identity cannot be proved, report unresolved creation and
do not blindly create again. Specify how the reservation, mapping and native evidence survive
Runner/process crashes. Native ID is scoped to the store and never substitutes for the public ID.

Define runner-owned store create/reuse without ID recycling, Session reservation and internal
resume, per-store versus per-Session options, store retirement and explicitly authorized data deletion.
Persist store harness type and exact Sandbox UID
binding; process death and Sandbox suspension do not delete data. Moving a store to another Sandbox
requires a new binding and authorization. Until proven safe, prevent concurrent writes to one store.
A reachable runner returns a terminal missing-target outcome for the public Session; offline
queued acceptance cannot prove membership. Native child IDs are harness-chosen: if exposed, register
stable runner-chosen public mappings with store and parent identity before publication, and separate visibility
from independent control. Child projection remains [deferred](task_freezer.md#native_subagent_threads--as-linked-agentplane-threads); use the
[merged child characterization](../native/docs/child_sessions.md) for process capability. The operator's private remote-control RE finding
reports outbound HTTP session-start polling with one Claude process launched per Session speaking
RemoteIO, characterized as essentially the existing stream-json stdio protocol. This is an
attributed finding, not a runtime process-count test or proof that Claude can never multiplex by another
route. Use one Claude process per root Session and Codex's demonstrated shared app-server as current
adapter baselines, without exposing process routing or imposing one process per store. No
one-versus-many store or shared-store safety decision is made here.

Feed the reviewed process-diagnostic shape into
[`RUNNER_HARNESS_OBJECTS_DESIGN`](#runner_harness_objects_design--decide-process-diagnostics-and-placement-policy).
The current destination and authority contracts remain in force while their remaining questions are
reviewed; private runner mapping can be implemented independently. Neither design node authorizes runtime changes.

### `RUNNER_STORE_SESSION_IDENTITY` — implement durable store and public Session mapping

**Blocked on review of the private mapping contract (`HARNESS_IDENTITY_DESIGN`).** Extend the
existing durable public Session reservations and persisted native IDs with runner-owned durable
store IDs and exact Sandbox UID binding. Use a new migration rather than editing landed state formats.
Implement adapter-internal process selection, restart/resume and store write fencing; a process
incarnation must not enter a durable destination. Preserve the current per-Session state and authored
native data through migration. Cover same-ID/same-spec retries, conflicting reuse, crashes before
native creation, after native creation but before mapping, and after mapping but before response.
Where the native create result cannot be reconstructed, expose unresolved creation and preserve
orphaned native data for reconciliation instead of duplicating it. Exercise Codex and Claude adapter
paths without assuming Claude multiplexing. Specify atomic caller/proto and pinned runner/VM image
rollout before changing live addresses.

### `RUNNER_IDENTITY_LEGACY_CLEANUP` — retire only obsolete identity assumptions

**Blocked on `RUNNER_STORE_SESSION_IDENTITY` rollout; public address cleanup remains with
`DELIVERY_DESTINATION_CONTRACT` and its own caller gates.** Audit running/saved SandboxTemplates, pinned runner
wheels and every caller before removing old per-Session-process assumptions and stale docs/protos.
Do not delete retained runner stores, native conversations or History. Confirm exact UID/Session
routing to the private store, suspended-queue retry and restart on the deployed images before retiring
old paths.

## 14. Identity and Kubernetes accounts cutover

This section tracks the remaining Identity-related design, implementation and deployment evidence;
actor provenance and Kubernetes account grants remain separate from service authentication. The
[identity plan](identity_and_kubernetes_accounts.md) owns the current contract, and the accepted
Sandbox deployment is recorded in the [live evidence](../docs/sandbox_identity_cutover_2026_11_12/README.md).

The target is one identity source with three credential forms (operator decision, 2026-11-10;
[plan](identity_and_kubernetes_accounts.md#target-one-identity-three-credential-forms)):
SVIDs over mTLS, the MCP edge's OAuth tokens, and Kubernetes' own authentication toward the Kubernetes
API alone. Agentplane receivers reject Kubernetes bearers; components authenticate with service SVIDs
([authentication contract](../docs/workload_authentication.md#service-authorization-and-attribution)).

**Remaining work.** The standing-workload proxy decision-log observation remains open.
Cross-Identity inbox target semantics and Identity `CreateSession` remain separate items. The
[`environment` rule](../docs/adr_environment_rules.md)'s other operations are in its
[plan](environment_rules.md).

```mermaid
flowchart LR
    STANDING_WORKLOAD_IDENTITY[Standing workloads run as Identities; proxy observation remains]
    APP_IDENTITIES_UI[Paused: remaining identity slices in the app]
    IDENTITY_OWNERSHIP_DESIGN[Decision: Identity binding, store, delete rule and agent teardown]
    IDENTITY_READERS_BEHIND_POLICY_SERVICE[In flight: contract the Kubernetes Identity readers]
    RECORD_COLOCATION_DESIGN[Unscheduled: which records share a database or service]
    POLICY_SUBJECTS_IDENTITY_ONLY[Unscheduled decision: retire Sandbox-scoped grants and ServiceAccount subjects]
    IDENTITY_OWNERSHIP_DESIGN -. binding shape .-> POLICY_SUBJECTS_IDENTITY_ONLY
    POLICY_SUBJECTS_IDENTITY_ONLY -. launcher grant shape .-> ORCHESTRATION_V1_BACKEND_GRANTS
    IDENTITY_OWNERSHIP_DESIGN -. cross-store invariants .-> RECORD_COLOCATION_DESIGN
    SESSION_SUBAGENT_DELEGATION[Design: a Session creates and messages a sub-Session]
    SESSION_ACCESS_GROUPING_DESIGN --> SESSION_IDENTITY_AUTHZ[Cross-Identity inbox target, grant and migration]
    SESSION_IDENTITY_AUTHZ --> SESSION_CROSS_IDENTITY_AUTHORITY[Deferred: acting on another Identity's Sessions]
    CROSS_IDENTITY_SESSION_BINDING[Deferred: binding a Session to another Identity's environment]
    WAKE_ON_DELIVERY_DESIGN[Deferred decision: send to a suspended Session, Session and Sandbox states, wakeOnSend]
    IDENTITY_SYSTEM_TESTS[Remaining automated identity-boundary acceptance]
    SANDBOX_IDENTITY_CHANGE_CHECKPOINT[Deferred: restart after Identity change]
```

### `STANDING_WORKLOAD_IDENTITY` — standing workloads run as Identities

**Source complete; one deployment-specific observation remains.** Standing Haku workloads and the
`haku-ci` relay now use Identity-subject `WorkloadCredential`s and PolicyBindings. Their Pods still
run as their component ServiceAccounts for Kubernetes and network-policy purposes; those accounts
and their attribution are retained. The staging proxy decision-log observation that the relay's
Identity is the subject of the expected egress decision remains open. This is an observation gate,
not a claim that rollout is blocked or that the source migration remains unfinished. **Exit:** the
existing staging decision log records the expected Identity decision, or the operator records an
explicit waiver.

### `IDENTITY_OWNERSHIP_DESIGN` — who owns an Identity and what binds to it

**Decision; operator request, 2026-11-26.** Started from an Identity delete the app offered and the
Policy Controller refused; the operator asked for a sane teardown sequence, which widened into Identity
ownership. [The plan](identity_ownership.md) covers four linked questions: how Sessions and Sandboxes
bind to an Identity (operator: a Session and every Sandbox attached to it share one Identity) and how
a rebind moves it; where the Identity record lives (operator: an Agentplane-owned record is "a pretty
clear case"); when an Identity may be deleted (operator: eventually the Policy Controller refuses while
a Sandbox or Session names it) and a controller-published refusal now; and the agent teardown order.
Shares the Session-release question with
[`SESSION_ARCHIVE_CONTRACT`](#session_archive_contract--what-archiving-a-session-means) and the store
question with
[`KUBERNETES_DECOUPLING_DESIGN`](#kubernetes_decoupling_design--how-far-to-decouple-from-kubernetes).
**Exit:** the operator decides the plan's five questions; follow-ups become nodes.

### `IDENTITY_READERS_BEHIND_POLICY_SERVICE` — only PolicyService touches Identity CRs

**In flight, 2026-11-26 (this agent session).** Every service reads Identities through a shared
`WatchIdentities`-fed client that refuses when not current, instead of Kubernetes informers and
per-request GETs; `orchestration_v1` writes through PolicyService. Pays off whether or not the
Identity record later leaves Kubernetes. Done: the client with read-through on a miss, and eight
services plus the Dispatcher wake path on it in staging. Next: per-service contract, the Sandbox
Controller reconciler and the `orchestration_v1` writer. State and remaining steps: [Identity ownership § readers](identity_ownership.md#readers-behind-policyservice).
**Exit:** only the Policy Controller holds `identities` RBAC, and the verifier contract documents
watch-propagated revocation.

### `POLICY_SUBJECTS_IDENTITY_ONLY` — retire Sandbox-scoped grants and ServiceAccount subjects

**Unscheduled decision, raised by the operator 2026-11-26.** Direction: PolicyBindings name only
Identities. Today three other shapes remain:

- **Sandbox-scoped operator grants** (`GrantSandboxPolicyBinding`): owned by one Sandbox incarnation, but
  the subject is the Sandbox's Identity by name. Any Sandbox or Session sharing that Identity gets the
  grant while the Sandbox lives. The RPC comment still describes a ServiceAccount check.
- **Launcher grants** (`orchestration_v1`, `<sandbox>.launcher`): a Sandbox-owned PolicySet whose rules are
  pinned to that Sandbox's UID, bound to the launcher as a ServiceAccount or an Identity.
- **ServiceAccount subjects** (`Subject{namespace, name}`): ServiceAccount callers and the
  `WatchPolicyBindings` `subject` selector.

What exactly replaces each is TBD. Inventory first: every writer and reader of each shape, its live
staging objects, and what a caller loses if the shape goes. Then decide per shape: an Identity grant
with an expiry or owner, a rule scoped by Sandbox or Session instead of a subject, or drop it. Remaining
ServiceAccount callers need an Identity first (`STANDING_WORKLOAD_IDENTITY`). Coordinate the launcher
grant with `ORCHESTRATION_V1_BACKEND_GRANTS` and `SESSION_ACCESS_GROUPING_DESIGN`, and the owner/lifetime
question with `RECORD_COLOCATION_DESIGN`.
**Exit:** operator decision per shape, recorded with the inventory; implementation and contract nodes
follow from it.

### `RECORD_COLOCATION_DESIGN` — which records share a database or service

**Unscheduled, raised by the operator 2026-11-26.** Decide which Agentplane records live in the same
database, and behind the same service, so that invariants spanning them can be checked in one
transaction instead of straddling authorities. Seams seen so far:

- Session deletion vs its Notification inbox and subscriptions: nothing retires an inbox whose owner is
  gone, so retries go on forever (`SESSION_AND_INBOX_LIFETIME`, stranded inboxes 2026-11-26).
- Teardown order across Identity, Sandbox and Session, and the delete rule's reference check, which
  spans Kubernetes and the Dispatcher non-transactionally (`IDENTITY_OWNERSHIP_DESIGN`).
- "X is owned by Y" edges: Identity → PolicyBindings and Sandbox → launcher grants by Kubernetes
  garbage collection; Session → inbox, History and receipts across three services' databases.
- Read-your-writes across services: a watch-fed reader misses a just-created Identity (#20298).

For each edge, choose either to put both records in one store with one owner (transactional), or a named
cross-service protocol: owner-driven cascade with fencing, tombstones, or an outbox. Do not move any
record before deciding. Feeds `KUBERNETES_DECOUPLING_DESIGN`; the reference inventory in
[Identity ownership](identity_ownership.md#follow-ups) is its first input. Related:
`NOTIFICATION_SERVICE_SPLIT`.
**Exit:** a table of record kinds → owning service and store, the ownership edges between them, and
the mechanism chosen per edge.

### `IDENTITY_SYSTEM_TESTS` — finish automated identity-boundary acceptance

**Remaining coverage: cascading deletion of owned account grants and credentials.** The
[automated acceptance record](../docs/sandbox_identity_cutover_2026_11_12/README.md#automated-acceptance)
covers the egress hop, platform route, Kubernetes use-rule refusal and isolation, account expiry
and native-role revocation, Identity-owned PolicyBinding cleanup, and Firecracker relay
authentication. Exercise the remaining ownership chain using real Kubernetes garbage collection.
Preserve unrelated ServiceAccount grants and retained History. This is independent of
`STANDING_WORKLOAD_IDENTITY`'s deployment observation. **Exit:** one deterministic test or
existing passing evidence showing expiry and Identity deletion remove their respective owned
authorization objects.

### `SANDBOX_IDENTITY_CHANGE_CHECKPOINT` — safely restart a VM after Identity change

**Deferred; serving behavior remains fail-closed.** A suspended Firecracker launcher whose Sandbox
Identity changes is held with its checkpoint as `RunnerCredentialMismatch`. The controller SPEC
records that discarding the checkpoint and cold-starting under the new Identity is not implemented.
Design and test a safe transition that cannot thaw old in-memory work as the new Identity and
preserves the workspace and durable data. This task is independent of the completed workload
Identity cutover and must retain the current hold until the transition is proven. **Exit:** the
launcher discards only the old checkpoint, starts with the new credential, and coverage proves the
old checkpoint is never resumed as the replacement Identity.

### `APP_IDENTITIES_UI` — identities in the app

**Paused.** The app lists Identities, their references, origin, bindings, expiries and reachable
accounts. PolicyService watches Identities and refuses unsafe deletion (#15151). The Thread form
shows the existing Sandbox’s Identity (#15443). Operators can request Identity cleanup on Sandbox
deletion (#15444), rebind a stopped Sandbox with placed-Session checks (#15445), and launch a
Sandbox with a new or existing Identity (#20089).
[Policy resource watches](../policy_controller/SPEC.md#policy-resource-watches) ·
[Thread creation](../app/frontend/SPEC.md#thread-creation) ·
[Sandbox deletion and cleanup](../app/frontend/SPEC.md#sandbox-deletion-and-identity-cleanup)

Remaining UI slices can proceed independently of the completed workload cutover.

- Give permission requests an approval card with choices to create a temporary account, widen the
  shared account, or deny.
- Offer Identity cleanup on Thread finish once `SESSION_AND_INBOX_LIFETIME` and
  `SESSION_LIFETIME_IMPLEMENTATION` define the backend operation. Default cleanup to selected
  only when creation evidence identifies the Identity the app minted for that launch. Today only
  Flux origin is recognizable. `SESSION_MANAGED_IDENTITY` must stamp Session-created Identities
  so `identities/origin.ts` can distinguish them.
- On Connection consent (`/#/connection-enrollments/{handle}`) and OAuth client rebind, let the
  operator choose an eligible live Identity in the policy namespace. **View grants** should show
  its bindings. The Identity page should list its bound Connections alongside Sandboxes and
  Sessions (operator, 2026-11-09). [Current Connection flow](../app/README.md)
- Choose a retained source and retention policy for the Identities an environment previously ran
  as. Its current Sandbox-owned WorkloadCredential and revoked records give an incomplete history.

**Exit:** cover the BFF routes, capture each screen in visual tests, and exercise refusal to delete
a referenced Identity in the browser. [Operator UI design](identity_and_kubernetes_accounts.md#operator-ui)

The merged #15444 and #15445 slices have no recorded k3s system test or post-Flux staging proof as
of 2026-11-10. #15445's browser test exercises a running refusal by `EnvironmentService` and a
stopped patch against the API server, not a Kubernetes admission refusal. When verifying a staging
rollout, check the applied app/Gateway revisions and, with disposable resources, the stopped and
running rebind cases and the credential-refused cleanup/retry path. Identity-system acceptance owns
cutover scenarios; missing live evidence alone is not a separate gate when suitable automated
evidence exists.

### `SESSION_SUBAGENT_DELEGATION` — a Session creates and messages a sub-Session

Identity `CreateSession` is admitted only for an `environment_hosted` Session under a current
`bind_session` grant ([Dispatcher SPEC](../session_dispatcher/dispatcher/SPEC.md#session-binding));
central placement stays refused. Existing operator-provisioned same-Identity child steering
and the approved `orchestration_v1` broker flow continue to work; this node is separate from the
completed Sandbox workload cutover.

**Design, not started (operator, 2026-11-10).** Decide how a Session asks Agentplane to create a
child Session and then message it. Without that grant Dispatcher refuses an Identity caller at
`CreateSession` (`PermissionDenied`); privileged platform components retain their creation path,
and an Identity can read, steer, interrupt and cancel its own Identity-recorded Sessions. Build on the existing
[delegated authorization](../docs/delegated_authorization.md) contract and the messaging path that
landed in #15437 and #15438. The Sandbox launch Identity sequence is settled in the identity plan
and does not determine the child-Session design. The environment rule's `bind_session` operation
covers the target relation for native `CreateSession`, including environment-hosted child use;
parent authorization and environment-free Session creation remain separate designs. Coordinate the
separate environment-free creation design with `CENTRAL_SESSION_CREATION_CONTRACT`. **Exit:** a reviewed child-Session authority and delegation design
with its implementation slices; no authority change is selected by this tracking update.

### `SESSION_IDENTITY_AUTHZ` — finish cross-Identity inbox send targeting

Inbox ownership and History workload reads now use the recorded Session Identity. Notification's
same-Identity send path also passes without a grant. Cross-Identity sends still require
`session_inbox_access` (`notification_service/internal/rpc/messages.go`), whose current binding is on
the sender and whose targets are Sandboxes/tags; component senders and their ServiceAccount
provenance remain supported. The target/group model and grant lifetime remain unresolved under
[`SESSION_ACCESS_GROUPING_DESIGN`](#session_access_grouping_design--access-groups-across-independent-sessions-and-environments).
Preserve the current enforcement while that design is reviewed. Broader cross-principal History
reads remain held by `THREAD_READ_POLICY_DESIGN`. **Exit:** resolve target/group ownership, target
tag semantics and launcher-grant lifetime under `SESSION_ACCESS_GROUPING_DESIGN`, or explicitly
decide to retain Sandbox/tag targets. Then track the consumer/schema and grant migration (or
deliberate retention) as source work with tests; correcting this plan alone does not close the node.

### `SESSION_CROSS_IDENTITY_AUTHORITY` — acting on another Identity's Sessions

**Deferred (operator, 2026-11-11).** Under the [`environment` rule decisions](../docs/adr_environment_rules.md#decisions),
authority over an existing Session is implied only for a caller of the Session's own Identity;
every other caller is refused. An orchestrator that wants to submit input to, read, attach
environments to or detach them from Sessions of another Identity (worker Sessions it did not create
itself) needs a rule naming that Identity. The likely shape is a `session` rule targeting Sessions by
their Identity (`identities: [{name: worker}]`), never by creator, with no operation that changes a
Session's Identity. Picked up when an orchestration use needs it. **Exit:** a decided design and the
PRs it implies.

### `WAKE_ON_DELIVERY_DESIGN` — whether delivering Session work wakes its environment

The scheduled-agent use case is consolidated in
[persistent agents](persistent_agents.md#authority-wake-and-lifetime). Cron adds no wake authority;
this deferred replacement-policy decision is not released by that proposal.

**Deferred (operator, 2026-11-11); questions added by the operator 2026-11-16.** The
[`environment` rule](../docs/adr_environment_rules.md#decisions) keeps only `resume`, an explicit request; it has no
`wake` operation. Today a delivery wakes a suspended Sandbox through the implied same-Identity path
(`identityWakeAssessment`) or a configured broker role (`wake_callers`, `wake_service_callers`;
Action-attributed sends use the Actions service's broker grant until
[`ACTION_WAKE_AS_IDENTITY`](#action_wake_as_identity--authorize-action-originated-automatic-wake-as-caller-identity)),
gated by `spec.wakeOnSend` and wake budgets. Direction: the sender's right to deliver is already a
grant, so whether delivery starts the Sandbox is the recipient's choice, a property of the Sandbox or
of the Session living in a harness inside it, rather than a sender-side "X may wake S" rule. A
per-sender wake rule is not planned (YAGNI, operator 2026-11-11); add it only if a use appears.

Questions for the operator (2026-11-16):

- **Send contract.** The operator's candidate: a send grant is held at Session level ("may send to
  Session X") and implies reviving that Session's Sandbox as needed. Constraints from the current
  code: lifecycle policy already tells an idle-suspended Sandbox from a requested stop
  (`requestedStop`), and a requested stop wins over automatic wake today; one Sandbox can host several
  Sessions, so waking it for one wakes them all; wake budgets are reserved per waking caller
  (ServiceAccount, Identity or broker service). Session-scoped send grants are also open under
  [`SESSION_ACCESS_GROUPING_DESIGN`](#session_access_grouping_design--access-groups-across-independent-sessions-and-environments).
- **State machine.** One model of Session and Sandbox states and the transitions between them (send,
  idle suspension, requested stop, resume, deletion), including central Sessions with no environment,
  which have no Sandbox to revive.
- **`wakeOnSend`.** Whether to keep the per-Sandbox wake-on-message boolean at all.

Decide which object carries the wake choice (if any remains), how budgets bound a permitted sender,
and what replaces the broker role. **Exit:** a decided design and the PRs it implies.

### `CROSS_IDENTITY_SESSION_BINDING` — binding a Session to another Identity's environment

**Deferred (operator, 2026-11-11).** A Session–environment binding requires the Session's Identity
to equal the environment's bound Identity, with no policy override
([admitting a binding](../docs/adr_environment_rules.md#decisions)). A later rule may allow a binding across
Identities. It needs its own model of what each side may see and do: the environment's Identity
would run work directed by a conversation carrying another Identity's history. Picked up only when a
use needs it. **Exit:** a decided design, or a recorded decision not to allow it.

## 15. Cleanups

Deletions and simplifications that exist only because of a transition, a migration, a superseded
design or a track that has landed: dead code, met gates, compatibility shims, stale names, redundant
tests and docs. Census of 2026-11-10 over the `CLEANUP(added …)` tombstones, component `TODO.md`
files, `TODO` comments, sections 13 and 14, the identity plans, open PRs and the freezer; it absorbs
the 2026-10-10 legacy burn-down and the 2026-10-21 census of the lanes. **Unblocked**
has no gate, or a gate met on devel; **Blocked** names the node, PR or merge it waits for;
**Future** waits on an undecided design or a track not started. Each entry says what goes, where,
the gate and the source; a removal a track's own node lists is linked, not copied, and feature or
design nodes stay in their sections. Not cleanups: fakes kept to inject faults the real API server
cannot produce (named at the fake); the delta settlement flag (operator, 2026-10-13 PDT);
upstream-gated tombstones with a verifiable condition in code (`keepingOffset` in `electric_sync.tsx`
against `@electric-sql/client` 1.5.28, the jsdom pragma in `markdown.test.tsx`). Doing one still
needs the gate observed on devel and every reader found. The service-SVID authentication cutover is complete. Method-specific component authority uses exact
service SVID admission; Kubernetes API credentials and historical account attribution remain separate.

```mermaid
flowchart LR
    ACTION_BOX_RENAME[Unscheduled: Action boxes stop being called sandboxes]
    PROTO_PACKAGE_ALIGNMENT[Future: align protobuf packages with their directories]
    RUNNER_LAYERING_VM[Paused: layered VM guest, section 7] --> K3S_TEST_GUEST_RETIRE[Blocked: delete the k3s test guest]
    HISTORY_SERVICE_RENAME[Decision, section 8] -. takes its name .-> HISTORY_DATABASE_RENAME[Deferred: rename the sandbox_service database and role]
```

### Unblocked

Dispatchable now. "Unscheduled" is the operator's priority statement, not a gate.

#### Sessions and Environments cutover leftovers

- **Sessions image rollout.** Once the image containing the [renamed Sessions executable](https://github.com/agentydragon/ducktape/pull/19313)
  reaches `agentplane-staging-delivery`, confirm the Deployment is Ready and a Dispatcher RPC succeeds.
- **Transitional Sessions Pod label.** After the [Service selector change](https://github.com/agentydragon/ducktape/pull/19319)
  reaches `agentplane-staging`, verify that the live Service selects `app.kubernetes.io/name=sessions`
  and its Ready EndpointSlice Pod UIDs match the ready name-selected Pods. Then remove
  `app.kubernetes.io/component=sessions` from the separately owned `agentplane-staging-delivery`
  Pod template, regenerate its manifest and recheck endpoints after the Pods roll.

#### `ACTION_BOX_RENAME` — Action boxes stop being called sandboxes

**Unscheduled; operator, 2026-10-11 PDT: a later TODO.** Rename what calls an Action Service box a
sandbox, so it no longer reads as a Session Sandbox: the `sandbox__*` Actions,
`action_service/sandbox/`, the `agentplane-sandbox` templates and labels, and
`cluster/cdk8s/agentplane/command_sandbox.py`. The `sandbox__*` tool names are what agents and
external Connections call, so renaming them is a wire change. Boxes and Sandboxes are to converge
into one object type
([`SANDBOX_BOX_CONVERGENCE`](task_freezer.md#sandbox_box_convergence--sandboxes-and-action-boxes-as-one-object-type)),
which would settle the name instead; check that before dispatch.

### Awaiting operator review

- **`SAMPLER_STABLE_PROVIDER_PREFIX`** ([#16401](https://github.com/agentydragon/ducktape/pull/16401)):
  a sampler rollout must not break the request prefix an
  existing conversation already sent. #15591 edited the `write_stdin` schema text inside the
  sampler's `exec` tool, and Anthropic then rejected every older Thread's signed thinking ("The
  `tools` list differs from the one this block was created with"), because the tool list and
  instructions are rendered from current code on every request and staging sets
  `prefix_mismatch_behavior: error`. Those Threads cannot continue. **Operator, 2026-11-14 UTC:**
  "older threads can stay broken"; the fix only stops future prompt changes from breaking Threads,
  with no repair or migration of existing ones.

  Provider rules, from current documentation (2026-11-14):

  - **Anthropic Messages.** A thinking block's signature binds the top-level `system`, the `tools`
    set (name-sorted, so reordering is safe) and every earlier message. Editing them is a 400 when
    enforced (accounts created on or after 2026-08-31, or any request setting
    `thinking.block_binding.prefix_mismatch_behavior`); `drop_block` drops that block and every later
    one. Append-only forms: a mid-conversation `role: "system"` message for instruction changes;
    `tool_addition`/`tool_removal` blocks in such a message (beta
    `mid-conversation-tool-changes-2026-07-01`, tools declared up front with `defer_loading`); and,
    under beta `inline-tools-2026-09-15`, a `tool_addition` carrying a full definition that replaces
    a same-named tool's definition from that point on, with `tools[]` left as first sent.
  - **OpenAI Responses** ([preserved reasoning](https://developers.openai.com/api/docs/guides/preserved-reasoning)).
    The prefix includes `tools`, `instructions`, `text.format` and earlier input items. A change is
    not rejected: reasoning items after it are silently dropped (reported in
    `input_transformations`), with no parameter to change that. Append-only forms: an
    `additional_tools` item to add tools, `tool_choice.allowed_tools` to restrict them, a
    `configuration_update` item for effort or output schema, and an appended developer message for
    instructions.

  Design: persist the rendered tools and instructions with each conversation as its frozen
  baseline. On each request, compare it with what current code renders, and express any difference
  through the provider's append-only form at the next turn boundary. A difference with no such form
  on that route waits for an explicit boundary (new Thread, compaction). Never edit the baseline;
  keep `drop_block` off. Test by rendering a stored conversation under a changed tool description
  and asserting the provider accepts the continuation and keeps its reasoning (empty
  `input_transformations` on both APIs). Covers the Responses and Anthropic Messages paths.

### Blocked

Grouped by the node, PR or merge each waits for.

#### By gate

- **`STANDING_WORKLOAD_IDENTITY` (source complete; one observation pending):** the standing records
  and bindings name Identities. Preserve the standing Pods' ServiceAccounts as their Kubernetes and
  network-policy identities. The remaining check is the staging proxy decision log naming the
  `haku-ci` relay's Identity for the expected egress decision; this does not imply that rollout is
  blocked today.
- **`DELIVERY_DESTINATION_CONTRACT` (in progress):** the browser's legacy command-destination records
  (`LocalCommand` with `target` null or `sessionAddressed` false, `app/frontend/sessions/local_commands.ts`)
  are retired once every retained record is Session-addressed; preserve unconfirmed saved input and
  its Sandbox UID fence, and never replay an uncertain command. Source: [app TODO](../app/TODO.md).

#### `K3S_TEST_GUEST_RETIRE` — delete the k3s test guest

**Blocked on `RUNNER_LAYERING_VM`.** The system tests' `_vm` targets boot a Bazel-built test guest
under `system_tests/vm/guest`, a second implementation of `agentplane/env/images/vm.nix`'s guest boot
contract (disks by serial, workspace format, harness and tool paths); the two share the static
runner/slice units and native supervision backend. Delete it once the production guest is Bazel-built
and boots in seconds under emulation, and point the k3s VM scenarios at that guest.

### Future

The gate is a decision not yet made or a track not started; nothing here is dispatchable.

#### `PROTO_PACKAGE_ALIGNMENT` — align protobuf packages with their directories

**Future; operator, 2026-11-12: revisit after the source moves.** Inventory Agentplane-owned
`.proto` package declarations against their filesystem locations, including the History read API.
Decide a consistent naming scheme and document deliberate exceptions; moving a file does not by itself require a wire rename.
A package rename changes fully qualified message and service names, including gRPC method paths and
`Any` type URLs, even if field numbers stay the same. Before each proposed migration, inventory
clients, persisted or queued payloads, descriptors, reflection, authorization rules and mixed-version
rollouts; define compatibility, data handling and rollback, with tests for old/new interoperability.
Dispatch reviewed migrations separately, rather than coupling them to package-directory moves.

#### Decisions and tracks

- **`CENTRAL_SESSION_CREATION_CONTRACT` ([#15436](https://github.com/agentydragon/ducktape/pull/15436),
  recommendation A):** `central_sampler_v1` is retired in the cutover that adds the `central_sampler`
  placement: `CentralSamplerV1Placement` in `proto/session.proto`,
  `session_dispatcher/internal/placement/placement.go`'s `CentralSampler`,
  the `central_sampler_v1` cases in `app/testing/client.py` and
  `session_dispatcher/dispatcher/testing_client.py`, `sampler/testing.py` and
  `system_tests/scenarios/central_sampler.py`; each V1 row becomes a `central_sampler` Session with
  one `executor` attachment. The plan file `central_session_contract.md` burns down with its three decisions.
- **The observation naming layer** (`Event.observation`, Runtime's observation handling, History's
  `ReadSessionObservations` and `ReadNativeFrames`, the debug UI): squash to "Event" if the operator
  agrees it is not a deliberate distinction. Source: [frontend TODO](../app/frontend/TODO.md#thread-view-ux).
- **One result shape for executors:** the MCP edge's per-executor result rendering
  (`mcp_edge/internal/mcpserver/results.go`) goes if the Sandbox executor answers as an MCP tool. Source:
  [Action Service TODO](../action_service/TODO.md#executors-that-answer-as-mcp-tools).

#### Authentication: waiting on a decision

- **Pod-bound checks per route** ([identity plan open question 1](identity_and_kubernetes_accounts.md#open-questions-and-tricky-bits)):
  Dispatcher and the lifecycle manager no longer gate privilege on `IsPodBound`, which has no
  caller left. Once every workload is an Identity SVID with a
  per-incarnation credential, each route decides whether a Pod proof still adds anything; the
  checks that do not are deleted then.

#### `HISTORY_DATABASE_RENAME` — name the database for its owner

**Deferred by the operator; not dispatchable until selected.** The old Sandbox Service is gone, but
the `sandbox_service` database, role and `postgres-sandbox-service` Secret remain in use by the
History Service. This is a naming cleanup, not permission to recreate or drop the database. Rename
the database and role in place (`ALTER DATABASE`/`ALTER ROLE ... RENAME`) and have the CNPG
`Database` and managed role adopt the new names; recreating the database under a new name would copy
the log. Verify the History Service and Electric reconnect with no data movement. Update the
`history_service/internal/migrations/migrations.go` and `cluster/cdk8s/agentplane/history_service.py` docstrings.

## 17. Two binaries and the Go layout

The [two binaries](../docs/adr_two_binaries.md), Sessions and Environments, serve under their
canonical names. The
[Go reorganization plan](https://agentydragon.github.io/ducktape/agentplane-organization/) holds the
evidence and line-level steps for every track below; its stages are ordered by certainty. Stages 1,
2 and 4 are done. The Nov 22 [published plan](https://github.com/agentydragon/ducktape/blob/gh-pages/agentplane-organization/go-plan.json)
has empty `ready_filesystem_moves` and `next_prs` lists. Its follow-on inventory accounts for
the execution backend rename in [#19100](https://github.com/agentydragon/ducktape/pull/19100).
The remaining opportunities have specific gates.

- Compare one set of Sessions configuration examples against Python authoring and Go startup:
  omitted defaults, invalid nested delegation and credential-safe errors. This is a usable
  input to the settings decision. The [protocol-shapes audit](https://agentydragon.github.io/ducktape/agentplane-protocol-shapes/)
  compares 15 corresponding top-level key names. Strict YAML loading already exists.
- For `orchestration_v1`, trace the approved launch and its rollback. It still writes Identity and
  launcher grants directly. Review who may create the child Identity and grant lifecycle rights
  before migrating callers and removing the adapter. The Notification split, History journals,
  settings ownership and protobuf wire-name migration each retain their own decision.
- In the [test architecture report](https://agentydragon.github.io/ducktape/agentplane-test-architecture/),
  the required KubeVirt suite still included App at the evidence revision.
  [#19148](https://github.com/agentydragon/ducktape/pull/19148) has since removed that dependency.
  Replay representative App schema, shared-renderer, native-provider and guest-only changes against
  the current selector before splitting another fixture. Measure matched warm and cold required
  feedback in the [existing qualification tracker](https://github.com/agentydragon/ducktape/issues/16099).

| Plan stage                      | DAG node                                                                                  |
| ------------------------------- | ----------------------------------------------------------------------------------------- |
| 3. Agreed moves                 | `orchestration_v1` retirement is `SESSION_TOOL_SURFACE_CONSOLIDATION`                     |
| 5. Waiting on service decisions | `NOTIFICATION_SERVICE_SPLIT`, `JOURNAL_REPRESENTATION_DESIGN`, `SERVICE_SETTINGS_RETHINK` |
| 6. Deferred                     | `PROTO_PACKAGE_ALIGNMENT`                                                                 |

```mermaid
flowchart LR
    ENVIRONMENT_KEEP_ALIVE_HOLDS[Proposed design: keep-alive holds]
```

### `ENVIRONMENT_KEEP_ALIVE_HOLDS` — keep a Sandbox awake until a time

**Proposed design; operator, 2026-11-14 05:18 UTC:** "I'm sure you can find a good solution. I think
something like "please keep this sandbox alive for me until time X"'would be probably reasonable."
Today only the Runtime extends awake time. Proposal (Go plan, keep-alive): one hold per Sandbox and
holder with an absolute expiry; while the latest unexpired hold is in the future the controller
starts no idle suspend and reports holders on the Sandbox's status. Holds never wake, a requested
stop wins, expiry only suspends, the controller re-evaluates on hold changes and at the expiry
without polling, and whoever may resume a Sandbox may hold it. **Exit:** a reviewed record shape and
authority, then the controller change.
