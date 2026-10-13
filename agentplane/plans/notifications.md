# Notification Service: remaining work

The [task DAG](task_dag.md) owns outstanding work and dependencies. This file records only
remaining acceptance and future decisions. For implemented behavior and deployment, see the
[service README](../notification_service/README.md), [App notification diagnostics](../app/README.md#notification-diagnostics),
and the [staging acceptance record](../notification_service/docs/staging_github_acceptance.md).

## Current priority and service boundary

Keep delivery workers in the notification service. Separate worker deployment is not planned
unless worker deaths cause actual operational trouble; fix specific failures in place first.
See the conditional [`NOTIFICATION_WORKER_ISOLATION`](task_freezer.md#notification_worker_isolation--separate-notification-api-and-delivery-workers)
item. GitHub receipt retention is also deferred, not a current priority. Neither item should be
presented as the immediate next project merely because the webhook work is complete.

## Subscription authorization and Action approval

**Proposed design gate; no Action/API or broader grants are selected yet.** Subscribing is an
information-access operation, not just routing a notice to an inbox. As sources expand, users may
want broad automatic allowance within approved scope and explicit approval for additional access.
Compare an Action-backed subscribe API with direct notification-policy enforcement; reuse the
existing Action approval/auto-allow mechanism if it fits rather than creating a second approval
service. The DAG separates `SUBSCRIPTION_AUTHORIZATION_DESIGN` from implementation.

An illustrative Action would take a typed source/target/filter, explicit destination, bounded
lifetime and stable request key. Source-specific policy can auto-allow an operator-granted scope,
require approval for another delegable scope, or deny access; approval cannot conjure provider
credentials or rights the approver lacks. These are design examples, not existing catalog entries
or grants. Review who administers policies and the operator's delegation boundary.

Separate these responsibilities:

- **Creation approval:** is this caller allowed to establish this exact subscription? If an Action
  waits for approval, creation has not happened. Define idempotent execution/recovery so a lost result
  cannot create duplicate subscriptions. Bind approval to immutable normalized arguments; expanding
  scope must require a fresh authority check rather than reuse an old decision.
- **Continuing source permission:** Notification Service must enforce the selected scope when
  observing, matching and delivering, including renewal, expiry and revocation. Decide whether
  creation records a bounded delegated grant or relies on a current standing policy, how promptly
  revocation applies, and what an approval's lifetime means for a long-running subscription.
- **Destination authority:** the caller must be allowed to route that content to the selected
  inbox/session; ownership of an inbox is not permission to read every source. Subscription approval
  does not confer send/read/create/wake rights on other agents or make source payloads instructions.
- **Management and retained data:** define who can list/read/change/cancel a subscription and how
  revoked grants affect retained entries. Do not promise that cancelling can recall already-delivered
  content. Narrow updates/cancellation should not accidentally require repeated human approval.

If Actions executes creation, Notification Service still owns the durable subscription, workers and
inbox entries. Verify the original requesting principal, approved parameters and authority using an
explicit trusted service contract—not a caller-supplied identity header or the executor's broad SA.
Normal direct subscribe/update APIs must enforce the same gate, or they become a bypass. Workloads
must not obtain unrestricted access by selecting a different ingress path. Avoid approving each
individual event after creation unless a specific product requirement justifies that burden.

### Kubernetes case to review

Specify allowed namespace, resource kind/name/UID (and name-reuse behavior), selectors, event types,
fields and payload content. Status-only rollout observation is not automatically permission to read
full objects, Pod logs, Secret references or arbitrary cluster Events. Decide whether the source
uses the caller's existing Kubernetes read rights or an explicitly delegated observation grant, how
it checks them under a privileged watcher, and how list/discovery/filtering avoid disclosing forbidden
resources. Do not equate access to a kube-admin Action with permission for an unlimited durable watch.

Review at least three request examples: narrow pre-authorized namespace/workload observation;
additional delegable scope needing operator approval; and forbidden scope. Pin behavior under
resource replacement, changed grants, expiry and reconnect without relying on client-side filtering.
The future Kubernetes source must depend on the selected authorization design/enforcement; provider
implementation remains a separate choice. Ordinary existing GitHub reconciliation can proceed under
its current access checks without waiting for a new general subscription-policy framework.

Acceptance uses deterministic policy/service tests for auto-allow, pending/denied approval, scoped
execution, direct-API bypass prevention, updates, renewal and revocation, plus one bounded deployed
identity/policy check for the selected integration. This design adds no new service, grants no rights
by itself and does not require a real cluster/provider outage.

## Missed GitHub updates and shepherding reliability

**New open design/implementation work, not a claim that the source of misses is established.**
The operator reports possible missed GitHub updates; webhook-driven PR shepherding must not stall
silently. The DAG separates `GITHUB_NOTICE_RELIABILITY_DESIGN`, `GITHUB_NOTIFICATION_RECOVERY` and
`GITHUB_SHEPHERD_GUIDANCE`. This does not reopen the completed durable-refresh implementation or
waived live overlap/backoff acceptance. Existing refresh of subscribed resources is not by itself a
promise to synthesize notifications for changes whose webhook never arrived.

First distinguish absent upstream webhook delivery from local receipt/matching, retained inbox entry
or harness-notice delivery gaps using available evidence. Do not infer an upstream miss merely from
an idle conversation or manufacture an upstream delivery receipt for an API observation.

Review these alternatives with the operator:

- **Service reconciliation:** recommend evaluating a bounded periodic authoritative-state check
  per active shared subject, reusing durable GitHub observations, refresh leases/backoff and access
  validation. Wake workers on webhook hints but do not rely exclusively on those hints. Persist
  observed revision, subscription coverage and emitted inbox updates consistently so crashes/races
  cannot silently advance past an undelivered observation. Reuse normalization/shared refresh;
  do not poll separately for every overlapping subscription or add another service.
- **Agent fallback:** explicitly document best-effort delivery and provide a bounded authoritative
  recheck schedule for shepherding, optionally using cron notifications. Define who owns the timer,
  when checks stop, and what happens when the harness is unavailable. A prompt to “remember to poll”
  without a runnable mechanism is not eventual delivery. Scheduled notices still cannot wake a
  stopped harness under the current contract.
- **Combination:** state reconciliation can close common missed-state updates while agents still
  verify the current head/check/review/merge state before declaring a PR ready or done. No method
  should treat silence or a stale check payload as evidence of current completion.

Choose the promised scope before implementation: current-state convergence for selected PR/CI fields
is smaller than delivery of every intermediate webhook event. A state that changes and reverts
between polls can be invisible; reconstructing comments/reviews or transitions may need paginated
resource history, explicit coverage and stable dedup keys. Do not claim complete event replay from
a snapshot API. Preserve subscription event filters/creation boundaries and access revocation.
Reconciled observations must identify their origin rather than masquerade as genuine webhooks.

Specify staleness targets, active-subscription eligibility, rate-limit/backoff behavior and visibility
when a target cannot be met. Pick a durable scheduling/checkpoint scheme and atomic or replay-safe
emission rule. A general cron product is not required for a service's next-refresh deadline; if agent
reminders are selected, add only the scheduler dependency actually needed. Keep processing inside
the current notification service. Notification-owned reconciliation has no inherent archive dependency.

Acceptance: suppress a webhook in a controlled test peer and show the selected mechanism discovers
a relevant current-state change within its configured check interval; test restart and webhook/poll
races for no duplicate/lost observation. Test changed PR head/check states and access/backoff using
the existing client seam. Do not induce real GitHub failures or revive the waived broad live matrix.
Document current limits and the final authoritative-state check agents should perform regardless.

## Proportional verification

The broad live event/permission/filter matrix moves to the
[freezer](task_freezer.md#broader-verification-inventory), revisited when enabling a new path or
investigating a concrete gap. Successful deliveries do not establish every installation's coverage,
but absence of those anecdotes is not a release gate. Prefer focused automated regressions.

The bounded security check remains in
[`NOTIFICATION_INGRESS_BOUNDARY`](task_dag.md#notification_ingress_boundary--bounded-public-route-security-check):
unsigned public webhook requests and public attempts at private API routes must be refused.
Invalid-signature service tests already exist; this remaining check is route wiring, not an induced
GitHub outage. Hosted builds, preset use and waived backoff/refresh-reuse exercises stay closed.

## Deferred: event-driven Actions consumption

If idle polling becomes a priority, replace the notification source's five-second Action-history polling in a separate implementation PR:

- Add a read-authorized SSE change feed backed by the Action Service's existing committed-event
  notifications. Its current SSE endpoint is operator-only; notifications must not gain operator
  authority to consume updates. No cross-service database access.
- Use one shared feed per notification-service replica, rather than one waiting connection per
  subscription. Treat feed messages as invalidations, not as another authoritative event log.
- On startup, reconnect, subscription creation and invalidation, drain the canonical Action event API
  from each subscription's persisted `actions_after_sequence`. Commit progress with inbox entries.
- Register the stream before catch-up reads and fence invalidations racing with processing. Waiting
  for source changes must not occupy an inbox delivery worker or hold its lease.
- Reconnect with refreshed projected credentials and bounded error backoff. Keep delivery retries and
  retention scheduling, but remove periodic Action-history reads while idle.
- Test reconnect/missed-signal recovery, concurrent events and subscription creation, ownership checks,
  and that idle subscriptions neither poll history nor prevent unrelated inbox delivery.

## Deferred decisions and follow-ups

- Home Assistant entity/event subscriptions:
  [`HOME_ASSISTANT_NOTIFICATIONS`](task_freezer.md#home_assistant_notifications--entity-and-event-subscriptions).
- Extract genuinely shared source wiring as concrete implementations accumulate, not a speculative
  framework: [`NOTIFICATION_SOURCE_WIRING`](task_freezer.md#notification_source_wiring--extract-shared-wiring-as-sources-accumulate).

- Recurring scheduled/cron notifications with durable scheduling and explicit missed-tick behavior:
  [`CRON_NOTIFICATIONS`](task_freezer.md#cron_notifications--scheduled-notifications-for-agents).

- Structured notification-message provenance for eventual compact frontend rendering:
  [design and acceptance](notification_presentation.md);
  [`NOTIFICATION_PRESENTATION`](task_dag.md#notification_presentation--compact-notification-rendering).
  Preserve full agent-facing text and raw evidence; never identify notices by text prefix alone.

- Agent-visible Kubernetes rollout monitoring, potentially as a notification source:
  [`KUBERNETES_MONITORING`](task_freezer.md#kubernetes_monitoring--agents-observe-rollout-progress-and-outcomes).
  Backend ownership, authorization and the watch API remain design choices.
- Authenticated discovery of App-accessible repositories and source capabilities:
  [#8981](https://github.com/agentydragon/ducktape/issues/8981). Distinguish accessible repositories,
  active subscriptions and healthy delivery without exposing other agents' subscriptions.

- Consider removing `lifetime_days`. Prefer no automatic expiry; if retained, make it opt-in with
  agent warning/expiry-notification semantics.
- Notice delivery without reading conversation history:
  [`NOTIFICATION_COMMAND_OUTCOME_DESIGN`](task_dag.md#notification_command_outcome_design--notice-delivery-without-reading-conversation-history).
- Consider automatic Action following or a submission convenience flag, backed by durable authorized
  intent and reconciliation rather than a best-effort second request.
- Consider narrower GitHub repository/event grants instead of shared access to every App installation.
- Deferred, not a current priority: bound raw GitHub receipt retention without breaking subscription
  boundaries, association evidence or delivery-ID deduplication. See
  [`NOTIFICATION_GITHUB_RETENTION`](task_freezer.md#notification_github_retention--bound-github-delivery-receipt-storage).
- Additional sources/scopes: repository subjects, workflow-specific
  filters, tags/releases and deployment/environment subscriptions. No promise of complete historical replay.
- Notification-triggered provisioning/resume, offline-delivery guarantees and wake budgets.
- Runner-hosted MCP context, per-session identities, cross-account delivery and successor-session retargeting.
