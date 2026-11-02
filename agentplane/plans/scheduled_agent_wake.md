# First durable scheduled-agent workflow

Proposal refreshed for operator review, 2026-11-02. `SCHEDULED_AGENT_WAKE_DESIGN` owns the
decision; runtime implementation and activation remain blocked on review.

## Recommendation and alternatives

Start with an operator-managed Kubernetes CronJob and a small delivery command calling the
existing Session Dispatcher. The initial daily CI agent is an already-opened central Session;
the timer sends input under that Session's execution account. Keep schedule ownership in GitOps
and command admission in Dispatcher. No new Session CRD, agent-role framework, or delivery queue
is needed for this scope. Another scheduler can use the same occurrence/delivery contract;
Kubernetes is the deployment adapter, not the definition of a scheduled agent.

| Option                                                      | What it provides                                                                               | What it adds / limits                                                                                                                         |
| ----------------------------------------------------------- | ---------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| Existing scheduler plus authorized input, recommended first | Durable schedule object, explicit timezone, restart/retry controls and normal Session receipts | Operator-managed schedules; normal input may join an active turn and does not enforce task completion                                         |
| Notification-owned schedule source                          | Agent API ownership, inbox delivery and a place for coalescing/expiry policy                   | New source/storage/API and authority rules; existing subscriptions are Actions/GitHub sources with expiring lifetimes, not standing schedules |
| New dedicated scheduler service                             | Independently managed schedule API and workers                                                 | Another owner/deployment without a demonstrated need in the daily workflow                                                                    |

Choose Notification-owned scheduling now if agent-created schedules, durable coalescing or strict
task-level non-overlap are required for the first milestone. Those are real backend capabilities,
not semantics a thin CronJob adapter can claim. Notification reorganization is not a prerequisite
for either option.

## Concrete daily CI configuration to review

Illustrative values, not an activation request:

| Field                | Proposed value / meaning                                                                     |
| -------------------- | -------------------------------------------------------------------------------------------- |
| Cadence              | `0 9 * * *`, `timeZone: Etc/UTC`                                                             |
| Destination          | One existing central Session UUID and exact Sandbox namespace/name/UID                       |
| Identity             | Candidate: Session execution SA; choose caller and token audience before activation          |
| Schedule identity    | An explicit stable schedule UUID retained across ordinary configuration updates              |
| Evaluation interval  | `[scheduled_for - 24h, scheduled_for)`, expressed as UTC instants                            |
| Late-start window    | `startingDeadlineSeconds: 21600`; older missed slots are skipped                             |
| Delivery lifetime    | `activeDeadlineSeconds: 900`, `backoffLimit: 6`                                              |
| Delivery-job overlap | `concurrencyPolicy: Allow`; duplicate occurrences deduplicate at Dispatcher                  |
| Agent-work overlap   | Submit normal input, which may steer an active turn; do not request interrupt or replacement |
| Initial state        | Suspended until Session, authority, model, tools and notification destination are configured |

Kubernetes scheduling can produce duplicate or missing Jobs. Its scheduled-timestamp annotation
identifies the intended slot; timezone, late-start deadline and Job concurrency are separate
controls. Job concurrency does not govern an agent turn after input admission. These facts follow
the [Kubernetes 1.35 CronJob contract](https://v1-35.docs.kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/).

The daily task asks the agent to inspect CI latency for that interval, select an evidence-backed
improvement, delegate an independently useful part when appropriate, and open a focused PR or record
why no change is warranted. Same-principal Session-spawning tools are implemented; whether a natural
prompt reliably uses them remains an optional usability follow-up. Existing CI skills/tools and
caller-held credentials supply that work; the scheduler does not analyze CI or certify a PR's
correctness.

## Occurrence identity and payload

An occurrence is `(schedule_id, scheduled_for_utc)`, independent of Job/Pod names, attempts,
delivery timestamps and sampler replicas. Derive one UUID command ID deterministically from that
pair under a fixed Agentplane namespace. Configuration revisions do not create a new identity for
the same slot. A manual extra run uses an explicit separate occurrence ID; it is not a retry.

Freeze the destination, Session, command ID and rendered input for an occurrence. Job attempts must
read the same immutable input/configuration revision. Do not mount a mutable current template and
rerender it under an existing command ID. The input carries its occurrence ID, scheduled time and
evaluation interval as descriptive data; none is caller authority or an agent-declared task result.

The [`SessionDispatcher` contract](../runner_gateway/dispatcher/session_dispatcher.proto)
`SubmitInput` RPC treats `command_id` as the idempotency key across command RPCs: a
retry with the same target, Session, operation and payload returns the existing `Dispatch`; reuse
for different work is rejected. `GetDispatch` reads that receipt under the Session owner's
authorization. The contract distinguishes queued, delivering, runner-admitted, harness-confirmed,
failed, cancelled and unknown states. The current Dispatcher has no writer for
`HARNESS_CONFIRMED`, so runner admission is not proof that input reached the model or that the task
completed. Use `sessions observe-input` and retained History for those later facts. Job status and
logs supply bounded scheduling diagnostics; a missing Job is not proof an occurrence ran. This
option does not promise a permanent ledger of skipped slots, or retention beyond the owning
services' existing lifecycle contracts.

## Delivery path and exact backend mapping

The proposed one-shot CLI operation accepts:

```text
deliver-scheduled-input(
  schedule_id: UUID,
  scheduled_for: UTC timestamp,
  target: {namespace, name, uid},
  session_id: UUID,
  immutable_input: string
) -> {occurrence_id, command_id, dispatch}
```

This is a proposed command, not an existing tool. Its owning shared client uses the generated
`SessionDispatcher.SubmitInput` and `GetDispatch` RPCs ([Dispatcher contract](../runner_gateway/dispatcher/README.md#contract)) and the existing
[`grpc/bearer.Dial`](../grpc/bearer/bearer.go) client. That client rereads the projected
ServiceAccount token on each call and does not retry an RPC automatically. The Job must mount a
token accepted for the Dispatcher audience under the selected caller identity; it does not export
a token into an agent prompt or use the Actions service's privileged identity. Existing CLI/MCP
callers can submit the same deterministic command ID through their current surfaces, without the
Kubernetes adapter.

For the Kubernetes adapter, obtain the Job's intended timestamp from
`batch.kubernetes.io/cronjob-scheduled-timestamp`. It is a Job annotation, not automatically a Pod
Downward API field. The adapter reads its actual Job, so the proposed grant is namespace-scoped
`get` on `jobs`, without `list`, `watch` or write. This reads complete Job objects: native
[RBAC resource/name rules](https://kubernetes.io/docs/reference/access-authn-authz/rbac/)
cannot restrict that grant to one annotation or a schedule label, and future Job names are not
a static `resourceNames` list. This additional read permission needs operator selection; it is
not inherited from Session access. If that scope is unacceptable, choose a scheduler backend
that supplies the slot directly. Never infer Session authority from Job metadata. If the intended
timestamp is unavailable, fail visibly instead of using wall-clock time as a new ID.

After a lost `SubmitInput` reply, reconcile with `GetDispatch` before acting on the outcome. If a
retry is warranted, reuse the identical command ID and payload; do not generate a new ID because a
read failed or a reply was lost. Dispatcher admission and native command deduplication retain their
existing recovery rules; this layer must not clear blocked Sessions or replay uncertain
provider/tool effects.

## Authority, environment wake and overlap

Dispatcher verifies the caller, Session execution-principal UID and exact Sandbox UID on every
normal Session call. A recreated ServiceAccount, replaced Sandbox or foreign Session does not
inherit the previous identity's authority. Select the caller identity and token audience explicitly;
a schedule label or Job identity does not grant Session access.

The recommended target is a central Sampler Session. Its initial open requires a ready Sandbox, but
later commands bypass execution-Sandbox readiness and wake ([central Sampler contract](../central_sampler/SPEC.md));
delivering scheduled input to an already-open central Session does not need an environment-resume
request. The useful CI work may still need filesystem or `exec` access. Before such an operation, the
agent can inspect the exact environment and its current eligibility, then use the existing explicit
resume path when allowed ([`EnvironmentService.ResumeEnvironment`](../sandboxes/environments/environment_service.proto),
[Session delegation guide](../action_service/docs/agents/session_delegation.md)).
The backend, resume API and caller tools are implemented, and the supporting Gateway and app images
are observed 2/2 in staging as of 2026-11-02. Resume acceptance is not readiness; refresh the same
target before using executor-dependent operations.

For any executor-dependent scheduled target, automatic wake is separate from input admission:
`wakeOnSend` enables the attempt, and the shared lifecycle service authorizes the live execution SA
against its current `resumePolicy` or checks a separately configured restricted broker grant
([Dispatcher contract](../runner_gateway/dispatcher/README.md#contract)). The schedule grants neither
permission. Decide the caller and wake behavior separately before extending this workflow beyond
the central Session.

For the initial central Session, normal input can join an active turn at a sampling boundary:
[`Store.DrainSteeringInput`](../central_sampler/store.go) records it in that turn after unresolved
tool intent is settled. It does not necessarily wait for the active turn to finish. The delivery
helper sends no interrupt or replacement command. This is ordinary Session input: it may steer the
active turn; it is not a task-level queue that waits for the Session to become idle and then starts
an independent run. The Dispatcher queue covers delivery to the Runner, not task completion or
non-overlap. Check a native harness's input semantics before extending this workflow to it. A Job
succeeds when Dispatcher admits input, not when a harness consumes it, a turn ends, a task succeeds
or a PR merges.

An indefinitely blocked Session can accumulate daily inputs. Dispatcher's queued-command cap
limits only commands it has not delivered to the Runner; it does not bound input already handed to
the sampler. This proposal adds neither durable coalescing nor a task-level backlog limit. Include
slot timestamps so a later turn can discard obsolete assignments, and expose admission exhaustion
as delivery failure. If idle-only delivery, bounded task backlog or strict non-overlap is required,
select a durable occurrence/admission policy before implementation; a read-then-send idle check
would race.

Stopping the CronJob prevents future occurrences, not accepted input or already running work.
Cancel an already queued input through `CancelDispatch`; interrupting a running turn requires its
current exact turn ID and separate explicit intent. Never implement overlap by killing the agent.

## Restart, edits and observability

- Scheduler or Job restarts reuse the intended slot and command ID. Multiple attempts/Jobs may
  exist; their accepted command remains one logical input within current retention.
- Sampler rolls do not change the Session or occurrence identity. Existing sampler recovery owns
  continuation; scheduling is not a recovery mechanism for unknown native effects.
- Editing the schedule changes future Jobs. Existing Jobs retain their frozen input. Conflicting
  reuse of a command ID is an operator-visible configuration error, not permission to resubmit.
- Read Job failure/admission status, then `GetDispatch`/`sessions observe-input` for delivery and
  correlated turn facts. Read retained Session output for the actual CI result. Do not infer task
  success from a completed delivery Job or a normal turn-completion status.
- Propose 14 days of Job diagnostics, subject to operator sizing. Session/input evidence follows
  existing retention. A later product view can join these sources without becoming their owner.

## Work after selection

Implement the occurrence/delivery helper, a GitOps CronJob using typed cdk8s constructs, its scoped
token/network configuration and clear operator diagnostics. Prepare the central Session with
platform guidance, model, existing CI access and delegation tools; no Session is created by a tick.
Activation of the daily agent is a separate reviewed configuration change.

Exercise duplicate Jobs/retries, exact payload conflicts, lost replies, service unavailability,
revocation/account replacement, paused scheduling, queue exhaustion and the selected missed/overlap
behavior with deterministic tests. Run the delivery path with the integration app unavailable.
The daily CI capstone separately proves a useful agent run. Natural-prompt use of the implemented
Session-spawning tools is an optional usability follow-up; scripted admission coverage does not
establish agent usefulness.

Review outcome needed: choose CronJob-first versus Notification-owned scheduling; accept or change
the six-hour late-start window and skip policy; decide whether ordinary input's active-turn steering
semantics are acceptable; accept or reject the namespace-scoped Job `get`; and select the real
cadence, timezone, caller/token audience and exact Session/Sandbox destination before activation.
Confirm that scheduled delivery itself does not resume the central Session's environment, and that
the agent may use the existing explicit-resume tools after checking eligibility if its CI work needs
filesystem or `exec` access. Strict task-level non-overlap/coalescing or agent-managed schedules
require replanning the backend slice rather than silently expanding this proposal.
