# Notification Service: remaining work

Actions and GitHub sources are shipped. Staging App **5188971** is enabled and the operator has
installed it on all their repositories. Real branch CI events and a PR comment were verified through
inbox and harness on 2026-10-04; see the [acceptance record](../notification_service/docs/staging_github_acceptance.md)
for evidence and limits. Basic implementation, provisioning and live delivery are no longer pending.

The [service README](../notification_service/README.md), HTTP OpenAPI and source discovery document
implemented behavior. The [task DAG](task_dag.md) owns work status and dependencies; this file tracks
remaining acceptance and deferred decisions.

## Registration and credential preparation

Registration, credentials, staging ingress and installation are complete. The environment-wide
**agentplane-staging** App is distinct from the MCP OAuth App. Any authenticated agent can currently
subscribe to App-accessible repositories, including private ones; this shared access policy remains
intentional. Operator confirmation of installation on all repositories is not a per-repository audit.
See [App setup](../notification_service/README.md#github-app-setup) for configuration.

## Remaining live verification

- [ ] Verify listener/service restart recovery and same-delivery-ID redelivery deduplication.
      Overlapping-subscription deduplication was proved live; it is not a webhook-redelivery test.
- [ ] Verify failed-delivery visibility and the operator/API redelivery procedure. GitHub does not
      automatically retry failed webhook requests; durable recovery starts only after receipt commit.
- [ ] Audit remaining event/permission coverage, including PR lifecycle/reviews, pushes/ref changes,
      installation lifecycle, revoked access and fork-head correlation. An uninstalled fork is not
      covered merely because its base repository is installed. Successful ducktape subscriptions do
      not prove access to every installed repository or all supported event kinds.
- [ ] Complete live negative ingress checks (unsigned request rejected; private workload API paths
      not publicly routed). HTTPRoute acceptance, exact-path configuration and successful signed
      delivery are verified, not a substitute for these negative probes.

## Next: event-driven Actions consumption

Replace the notification source's five-second Action-history polling in a separate implementation PR:

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

- More conservative, potentially turn-aware notice gating; the rule is still TBD:
  [`NOTIFICATION_TURN_GATING`](task_dag.md#notification_turn_gating--avoid-notices-piling-up-before-processing).
- Brief cursor-only notices backed by shared instructions, conditional on Claude/Codex mock-LLM
  compaction/resume evidence: [`NOTIFICATION_COMPACT_NOTICES`](task_dag.md#notification_compact_notices--shared-instructions-and-brief-cursor-hints).

- Home Assistant entity/event subscriptions:
  [`HOME_ASSISTANT_NOTIFICATIONS`](task_dag.md#home_assistant_notifications--entity-and-event-subscriptions).
- Extract genuinely shared source wiring as concrete implementations accumulate, not a speculative
  framework: [`NOTIFICATION_SOURCE_WIRING`](task_dag.md#notification_source_wiring--extract-shared-wiring-as-sources-accumulate).

- Recurring scheduled/cron notifications with durable scheduling and explicit missed-tick behavior:
  [`CRON_NOTIFICATIONS`](task_dag.md#cron_notifications--scheduled-notifications-for-agents).

- Structured notification-message provenance for eventual compact frontend rendering:
  [`NOTIFICATION_PRESENTATION`](task_dag.md#notification_presentation--structured-metadata-and-compact-notification-rendering).
  Preserve full agent-facing text and raw evidence; never identify notices by text prefix alone.

- Agent-visible Kubernetes rollout monitoring, potentially as a notification source:
  [`KUBERNETES_MONITORING`](task_dag.md#kubernetes_monitoring--agents-observe-rollout-progress-and-outcomes).
  Backend ownership, authorization and the watch API remain design choices.
- Authenticated discovery of App-accessible repositories and source capabilities:
  [#8981](https://github.com/agentydragon/ducktape/issues/8981). Distinguish accessible repositories,
  active subscriptions and healthy delivery without exposing other agents' subscriptions.

- Bootstrap GitHub head/fork associations, then maintain them from durable webhooks instead of
  refetching current heads on every matching pass. Keep authorization/revocation checks separate;
  define recovery for missed deliveries and out-of-order head changes before removing API refreshes.

- Consider removing `lifetime_days`. Prefer no automatic expiry; if retained, make it opt-in with
  agent warning/expiry-notification semantics.
- Add command-scoped admission/confirmation/failure tracking through Sandbox Service, resumable by
  command ID and backed by the runner's journal, so notifications need not follow conversation content.
- Consider automatic Action following or a submission convenience flag, backed by durable authorized
  intent and reconciliation rather than a best-effort second request.
- Consider narrower GitHub repository/event grants instead of shared access to every App installation.
- Bound raw GitHub receipt retention without breaking subscription boundaries, association evidence or delivery-ID deduplication.
- Additional sources/scopes: personal GitHub Notifications API, issue/repository subjects, workflow-specific
  filters, tags/releases and deployment/environment subscriptions. No promise of complete historical replay.
- Notification-triggered provisioning/resume, offline-delivery guarantees and wake budgets.
- Runner-hosted MCP context, per-session identities, cross-account delivery and successor-session retargeting.
- Proper runner RPC authentication/TLS; the authenticated service APIs do not resolve the runner-leg gap.
