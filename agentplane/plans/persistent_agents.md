# Persistent agents and scheduled wake

“I want to run a persistent agent in Agentplane, configured in manifests/config.” Declare its
instructions, execution Identity, Session placement, subscriptions and notice cadence; inspect and
pause it without keeping the integration app running. Ad hoc manual agents continue to work without
managed desired state. Persistent means durable intent, not a continuously running harness.

**Unscheduled proposal for when the operator wants it; no implementation released.** Supersedes the scheduled-wake
proposal in [#13552](https://github.com/agentydragon/ducktape/pull/13552). The operator's
[2026-11-17 direction](task_dag.md#scheduled_agent_wake_design--choose-the-durable-scheduled-wake-contract)
combines cron subscriptions, declarative agents and notice frequency. Notifications stays one service.

## User-facing configuration and ownership

Example intent: a CI-maintenance agent receives a daily `09:00 UTC` tick and GitHub CI events,
with at most one new inbox notice submission per ten minutes. The agent reads the accumulated
entries and decides what work remains. A tick is an observation to process, not proof of a completed job.

| Declarative scope                                     | What it ensures                                                                                                                    | Tradeoff                                                                                           |
| ----------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| Managed subscriptions on an existing Session          | Required subscriptions remain configured and renewed; the recipient cannot remove them.                                            | Small boundary, but someone must first create and retain the Session and recover failed placement. |
| Managed agent including its Session and subscriptions | The configured agent exists through restarts; its Session, instructions, placement and subscriptions have one desired-state owner. | Meets the whole user story; needs explicit create/adopt, edit, pause and deletion semantics.       |

Recommend the whole-agent scope. A subscription-only scope remains useful for an operator-managed
schedule on a manually created Session; whole-agent adoption must be explicit. Configuration has
three candidate paths; none is selected:

| Configuration option             | Benefit                                                                             | Cost/boundary                                                                                                                            |
| -------------------------------- | ----------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| **Flux + CR + controller**       | Familiar declarative apply, drift reconciliation and operator-visible desired state | New resource and reconciler contract; Kubernetes holds only small, rarely changing intent                                                |
| Notifications-native config file | Small path for managed subscriptions; deployment config already exists              | Subscription-only unless Notifications gains cross-service agent reconciliation; file edits require rollout today                        |
| API-only with a GitOps client    | Reuses backend authorities without a new CRD                                        | The client must provide durable mapping, deletion/drift handling and restart reconciliation; a one-shot script cannot ensure persistence |

**Operator, 2026-11-17 13:11 UTC:** "i guess one option might be something like manifest -> flux ->
k8s object -> operator/controller -> agentplane subscription object". This is the first option,
not a decision. Recommend evaluating it for whole-agent intent, with the controller applying
Notifications subscriptions and Dispatcher Session reservations through their APIs.

The [standing Kubernetes boundary](../docs/adr_sandbox_session_control.md) keeps per-Session data
out of control-plane etcd. A declared agent/subscription CR would hold bounded desired configuration:
references, schedule, cadence, pause and initial instruction reference. Keep Session records,
commands, event payloads, cursors, notices, delivery attempts, tick checkpoints and reconciliation
ID mappings in service Postgres. CR status may expose a small reconciliation condition, not a
per-Session activity feed. Even this desired-state exception needs the operator's explicit decision;
it does not license mirroring operational state into Kubernetes.

This retains `DECLARATIVE_SESSIONS_DESIGN`'s Kubernetes-owned desired state and Dispatcher-owned
existence/placement. Adopt UI/API-created objects only by explicit reference and ownership transfer;
do not match by labels or names. Reconcile only declared mutable fields, report immutable drift,
and leave unmanaged objects alone.

| Reconciler home              | Assessment                                                                                                                                                                            |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Sandbox Controller           | Already reconciles environment desired state, but [owns Sandbox lifecycle](../environments/README.md), not agent existence; coupling would make agents depend on an environment owner |
| Policy Controller            | Already watches Identity/policy resources, but [owns grants and credentials](../policy_controller/README.md); agent lifecycle would mix permission management with its consumers      |
| New managed-agent reconciler | Clear cross-service intent owner and separate service SVID/grants; adds a deployment and controller operations                                                                        |

Recommend a separate reconciler if CRs are selected; adding one does not split Notifications.
It writes as its own service SVID, never borrowing the operator's credential or impersonating the
agent workload. Its service identity is distinct from the configured execution `Identity`.
Notifications currently admits no such mutation actor. Dispatcher creation/placement and any
effective-Identity delegation need reviewed method grants; neither existing controller's Kubernetes
RBAC supplies them. The exact management/delegation contract remains an authority decision below.

Proposed intent fields: stable managed-agent key, execution `Identity` reference, `SessionSpec`,
initial instructions/reference, required subscriptions, minimum notice interval and operator pause.
The example uses `daily-ci-maintainer`, cron `0 9 * * *` in `UTC`, selected GitHub CI events and `10m`.
These are proposed fields, not an existing API. Pause stops scheduled production and new notices.

The proposed reconciler owns desired-state application and records stable Session UUIDs and
subscription creation keys. Dispatcher remains authoritative for Session existence, execution
Identity and placement; Notifications owns subscription records, inbox entries and notice delivery;
History and the Session host retain their current execution/history responsibilities. Identity stays
a security principal: [`IdentitySpec`](../policy/api/v1alpha1/types.go) contains a description, not
harness or environment configuration. Reference it; do not add agent lifecycle to it.

## Existing contracts and required additions

| Owner                        | Exists today                                                                                                                                                                             | New for this proposal                                                              |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| Notifications sources        | [Leased, durable source work](../notification_service/internal/sources/worker.go), Actions and GitHub providers, event deduplication and atomic inbox/checkpoint writes                  | Cron provider, schedule revision/checkpoint and tick payload                       |
| Notifications inbox/delivery | [Explicit acknowledgement, immutable notices and receipt reconciliation](../notification_service/docs/api.md); [quiet-window batching](../notification_service/internal/inbox/status.go) | Managed mutation restrictions, per-Session interval and persisted scheduling state |
| Session Dispatcher           | [Session reservation, idempotent creation, exact placement and live Identity checks](../session_dispatcher/dispatcher/SPEC.md)                                                           | Reviewed access for the desired-state reconciler; managed lifecycle semantics      |
| Identity/policy              | [Identity-bound credentials and separate method/delegation grants](../identity/auth/README.md)                                                                                           | No Identity lifecycle changes; any reconciler authority needs explicit grants      |

Today subscriptions default to seven days and renew for at most thirty days at a time. A controller
must renew before expiry and report a failed renewal; declaring persistence does not make an existing
subscription permanent. Recipient cancellation and inbox retirement are currently available. A
managed flag alone would not prevent either: Notifications must enforce the selected management
authority on cancellation, updates, renewal, retirement and any alternate mutation path.

## Cron source, recovery and overlapping work

Add a cron source inside Notifications' existing source workers. It emits structured inbox entries
with schedule identity/revision, scheduled UTC instant and any coalesced missed range/count. Use a
stable event key derived from the subscription, revision and due instant; commit entries and the
checkpoint together under the source lease. Duplicate workers/retries must reproduce the same key.
Schedule edits create a revision effective from a recorded instant; do not replay the old revision.
Default to UTC; accepting named timezones requires a defined daylight-saving policy first.

After downtime, recommend one catch-up entry summarizing missed ticks, then resume the regular
schedule. Persist the covered range so another restart cannot produce another catch-up for it.
Expired, canceled or explicitly paused subscriptions produce no ticks for their inactive interval.
An inbox quota failure leaves source progress pending and visible; the next successful processing
coalesces the overdue range. Do not turn an outage into an unbounded replay storm.

All sources still feed the same inbox and notice batching. A tick arriving during a turn uses ordinary
`SubmitInput`: it may be queued or steer the running turn under the host's existing behavior
([native contract](../env/runtime/SPEC.md), [central steering](../central_sampler/internal/residentruntime/session/turn.go)).
Do not interrupt or create a parallel Session for each tick. This is not a guarantee to wait until the
current job finishes. If a job needs nonoverlap, the agent must reconcile durable job state keyed by
scheduled instant; inbox acknowledgement and harness confirmation do not establish job completion.

Source deduplication and command idempotency do not make external agent effects exactly once.
Notifications already persists the notice command ID, retries that command, and reconciles admission
and harness confirmation from History. Reads and preparation do not acknowledge entries;
unacknowledged entries alone do not generate reminders. Preserve those contracts across restarts.

## Per-Session notice interval

The existing quiet window batches new unannounced entries; new arrivals extend it until the maximum
wait from the oldest entry. Library defaults are two seconds quiet and ten seconds maximum wait;
[staging configures sixty and 120 seconds](../../cluster/cdk8s/agentplane/staging.py). Neither is a
rate cap. Unconfirmed notices and unavailable destinations can delay delivery further.

Propose a configurable interval `I` from **observing the previous notice's confirmation to submitting
another logical notice**, shared by every source in the Session's inbox. With oldest/newest unannounced entry times `a`/`b`,
quiet window `Q`, maximum wait `M`, and the durable time `p` when Notifications observed the
previous notice's harness confirmation:

```text
eligible_at = max(min(b + Q, a + M), p + I)
```

With no previous notice, only the debounce applies. With debounce disabled, pending entries are
immediately eligible subject to the interval. The interval overrides debounce's maximum wait; show
the limiting deadline in inbox status. Source ingestion, reads and acknowledgements continue while
notices wait, and a newly prepared notice covers the accumulated eligible entries.

Recommend persisting `p` with receipt reconciliation and rechecking eligibility before preparing
and first submitting a new notice. Keep the existing unconfirmed-notice barrier. An uncertain reply
retains the immutable command and pre-send attempt marker; reconcile/retry that same command.
[Retries and receipt confirmation](../notification_service/internal/worker/deliver.go) bypass
new-notice debounce/cadence. Using observed confirmation, rather than an old first-attempt timestamp,
prevents an immediate second new notice after a delayed retry. Restart must preserve this gate.

This is conservative submission pacing, not a hard model-spend or harness-receipt guarantee.
It can wait longer while History lags; strict receipt spacing needs a Dispatcher/host contract.
Default to disabled for existing/manual Sessions and recommend `10m` for this managed-agent example.
The recipient may request a longer interval, but cannot lower an operator-set minimum. No priority bypass.

## Authority, wake and lifetime

A cron tick is a Notifications-owned source event with schedule provenance. It does not send as
the operator, the receiving workload or an invented Action request. Notice submission uses
Notifications' service SVID and its existing narrow Dispatcher service grant
([deployment](../../cluster/cdk8s/agentplane/runner_gateway_workload.py)); it still targets the recorded
Session and exact environment UID. Source provenance is not an execution Identity or permission.

Dispatcher Identity creation requires a live `bind_session` grant and permits native placement only;
central creation uses its privileged backend path. The [delegation contract](../docs/delegated_authorization.md)
separates service actor, effective Identity and provenance; no managed-agent provenance profile exists.
`SUBSCRIPTION_AUTHORIZATION_DESIGN` is deferred: source access, managed mutations, renewal and
revocation touch it, but this proposal selects no approval mechanism or policy engine. Configuration
alone grants no additional source access, including for GitHub or a future command watcher.

Receiving a notice and resuming an environment remain separate. Current Dispatcher wake requires
`spec.wakeOnSend` and the lifecycle authority/budgets; requested suspension wins. Actions' wake
broker serves Action-attributed sends, not arbitrary cron authority. Preserve the existing
Notifications delivery path and wake checks. Whether wake becomes a Session recipient preference
is still `WAKE_ON_DELIVERY_DESIGN`, deferred; this plan does not resolve or release it.

Currently both native and central V1 subscription targets require an exact Sandbox-backed placement.
Notifications retains inbox/subscriptions and collects authorized events if that Sandbox disappears,
but waits for its original UID before submitting a notice. Same-name replacement does not inherit
delivery, and same-name Identity replacement does not inherit ownership. Dispatcher retains Session
rows; explicit Session deletion is not implemented. Indefinite managed intent cannot repair a lost
environment or recover lost native state just by recreating a resource.

Recommend retaining the Session/history and reconciling the same IDs after controller restart.
Reapply mutable subscriptions/cadence; do not replay initial instructions or recreate a Session for
ordinary drift. Placement or immutable specification changes need explicit replacement/adoption.
Removing managed intent should pause managed subscriptions/notices and relinquish management while
retaining Session/history, not delete data or resurrect the agent. A durable pause/removal protocol
is new; specify its failure behavior before implementation. Operator stop and resource deletion win
over reconciliation. Environment-free central Sessions and final Session/inbox deletion remain with
the existing lifetime design.

For the CR option, edit reconciles by resource UID/generation with idempotent backend keys. Delete
requires a durable removal handoff before the CR disappears (for example a finalizer): if Notifications
is down, report pending removal rather than claiming delivery stopped. A recreated same-name CR is
new intent, not adoption. Controller outage leaves the last applied backend policy running until its
lease/expiry; report lag after recovery and reconcile current intent, not every intermediate edit.
Service outage preserves IDs and retries the same operation; partial Session/subscription creation
must be visible and resumed without duplication. Removal cannot recall an already submitted notice.

## Consolidated decisions and acceptance

This plan owns `SCHEDULED_AGENT_WAKE_DESIGN` and the managed-agent portion of
`DECLARATIVE_SESSIONS_DESIGN`. `SCHEDULED_AGENT_WAKE` remains blocked. It folds in
`COMMAND_WATCH_SUBSCRIPTION_DESIGN` only at the scheduling/inbox seam: running a command adds
execution location, Identity, authority, output baseline and hung-process limits, all still undecided;
a cron event does not authorize command execution. `SESSION_AND_INBOX_LIFETIME` retains the wider
central/deletion decision, and `WAKE_ON_DELIVERY_DESIGN` retains the deferred wake policy. Their
managed-agent cases are covered here without creating competing designs or implementation nodes.

Before implementation is released, settle the questions below and record concrete config/API owners
and authority. Acceptance should exercise reconciliation with stable IDs, protected-subscription
mutation denial, expiry/renewal failure, coalesced missed ticks, duplicate/restart delivery, notice
interval with quiet windows and uncertain submissions, running-turn input, and requested-stop/UID
fences. No code, rollout or new implementation nodes are selected by this proposal.

## Ranked open questions

1. **Which declarative scope and configuration path?** Default recommendation: a whole managed agent
   via Flux + bounded CR desired state + separate reconciler; compare native config and API/GitOps
   before selecting a schema or the etcd exception. This meets the full user story while preserving
   backend authority; subscription-only management remains an explicit narrower mode.
2. **Who can override managed intent?** Default: only its authorized operator/reconciler may remove
   required subscriptions, retire the managed inbox or lower its notice interval; the recipient can
   read/ack and request less frequent notices. Otherwise cancellation bypasses persistence and cost policy.
3. **What does “at most once” measure?** Default: gate a new notice until `10m` after observing the
   previous confirmation, opt-in for the example, no priority bypass. This conservatively paces new
   notices through retries/restarts; strict harness receipt spacing needs a separate contract.
4. **How are missed and overlapping ticks handled?** Default: UTC, one summarized catch-up, ordinary
   input during a running turn, agent-owned job reconciliation. This avoids replay bursts without
   promising exactly-once work or inferring job completion from receipts.
5. **What survives edits, removal and environment loss?** Default: stable IDs, explicit adoption or
   replacement, pause on removal, retain Session/history and exact UID fences. Reconciliation must not
   erase authored state or silently revive an intentionally stopped/deleted agent.
6. **Which authority and wake contract permits unattended operation?** Default: review the reconciler's
   explicit backend grants; use current Notifications delivery and recipient environment wake checks,
   respecting requested stop. Subscription authorization and replacement wake policy remain deferred;
   declarative intent alone grants neither.
