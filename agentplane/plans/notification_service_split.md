# Notification Service split: seam considered on 2026-11-17

**Not released.** Operator, 2026-11-17 03:59 UTC: "for now we keep all this in one service, because i
don't quite like the form of this seam yet - but we keep it open for future considerations and if
there's cleanups to apply on the current form, we can do those". This file keeps the seam sketched
that night, 03:34–03:59 UTC, as considered material for `NOTIFICATION_SERVICE_SPLIT`. None of it is
decided. The `inboxes` rename was tied to the split and is not released either. The direction it
answers, given on 2026-11-14, is quoted in the DAG node.

## Components

- **Inbox service** (today's `notifications`):
  - Owns inboxes, entries, messages and notices.
  - Owns subscription records: selector, expiry, quota, version and cancellation.
  - Keeps the Actions source and notice delivery to the Dispatcher.
  - Its 15 workload RPCs and their callers stay unchanged: the `agentplane notifications` CLI, the
    App's browser facade and system tests.
- **GitHub service**:
  - Owns the signed webhook (`POST /v1/webhooks/github`, same URL) and the GitHub App credentials.
  - Owns the 17 `github_*` tables, in its own database.
  - Keeps GitHub cursors and snapshots keyed by subscription ID.
  - Decides admission and does the matching.

## New RPCs

- **`GitHub.AuthorizeSubscription`**
  - Request: the subscriber `agentplane.Identity` (the exact incarnation the inbox service
    authenticated), the `session_id` and a `Selector`.
  - Response: empty.
  - It persists nothing. A refusal answers `PERMISSION_DENIED`; a malformed selector answers
    `INVALID_ARGUMENT`.
- **`Inboxes.WatchSourceSubscriptions`**
  - Streams one source kind's subscriptions: a snapshot, a caught-up marker, then upserts.
- **`Inboxes.AppendEntries`**
  - Carries matched subscription IDs, the existing typed event identity and the payload.
  - It is accepted only from that source's SVID, and only for live subscriptions of that source's
    kind.
  - It dedupes on event identity, so one event makes one entry per inbox, and the `entry` table is
    unchanged.
  - Matching subscriptions, not inboxes, means a source can write only where someone subscribed to
    it.
- **A per-subscription source-health report**, so `GetSubscription` still shows source failures.

The shared SQL transaction between GitHub facts and inbox wakes becomes an idempotent append. The
GitHub service advances its cursor only after the append is acknowledged.

## Selector

The inbox API keeps a closed `Source` oneof, with one variant per provider. Each variant is a message
the provider owns. For example, `agentplane.github.Selector` is today's `GitHubSource` with the same
field numbers, so the wire does not change.

The inbox service picks the provider by the variant and never reads the fields; the provider decodes
strictly. There is no `Any`, JSON or string selector.

## Who may subscribe to what

Today GitHub admission checks only that the service's GitHub App can read the repository.
Caller-specific rules are the deferred `SUBSCRIPTION_AUTHORIZATION_DESIGN`.

The sketch adds a new PolicySet rule type, `github_subscription`:

- `owner` alone covers any repository under that account.
- `owner` plus `repository` covers one repository.
- It is bound to an Identity through a PolicyBinding.

The GitHub service evaluates it in `AuthorizeSubscription`. It watches PolicySets and PolicyBindings
the way `internal/inboxaccess` watches send grants, and it keeps the App-reach check. When a grant is
revoked, the service stops appending and reports the subscription as access denied.

## Sequence: X subscribes to a PR, a webhook arrives, X is told

1. X's CLI calls `CreateSubscription`.
2. The inbox service checks the Session with `SessionDispatcher.GetSession`.
3. The inbox service checks that the Runtime holds the Session, with `EnvironmentRuntime.ListSessions`
   through the Environments proxy.
4. The inbox service calls `GitHub.AuthorizeSubscription`.
5. The inbox service stores the record.
6. The GitHub service sees the record on `WatchSourceSubscriptions`.
7. GitHub posts the webhook to the GitHub service, which checks the signature, dedupes and stores it.
8. The GitHub service matches the delivery by subject, filters, fork heads and access fences.
9. The GitHub service calls `AppendEntries`.
10. The inbox service dedupes the entry and schedules a notice.
11. The inbox worker reads `HistoryService.ReadSessionEvents`, then calls
    `SessionDispatcher.SubmitInput`.
12. The Dispatcher delivers the notice, waking a suspended Sandbox because
    `spiffe://agentplane/notifications` is in `wake_service_callers`.
13. X reads the entry with `ReadInbox` and acknowledges it with `AcknowledgeInbox`.

Today steps 4 to 9 run in one process. `fanout.Coordinator.Ingest` marks every inbox following the App
as due. Each due subscription is then processed separately: `Provider.Process` and
`Coordinator.Deliveries` find its deliveries, and `Coordinator.Record` appends them and advances the
cursor in one transaction.

## Alternatives considered

- **Provider-owned subscription records.** Admission rules come out the same. Every provider would
  repeat the Session and Identity checks, and would reimplement expiry, renewal, cancellation, quotas,
  the subscription watch and the cancelling of its subscriptions when an inbox retires. The CLI and
  the App would call each provider. `AppendEntries` would then address inboxes, so the inbox service
  could not tell whether anyone had subscribed. This shape wins only if providers need different
  lifecycles, not just different admission rules.
- **Inbox-side matching.** This would pull GitHub logic into the inbox service: subjects, fork heads,
  access fences and refreshes.

## Rollout sketched

1. A seam and names design.
2. Add the source-actor RPCs, with no callers yet.
3. Put Actions on the seam, migrating its cursor out of `subscription`.
4. Build the GitHub service binary, moving each file once; it is not deployed yet.
5. Grants and the new database first. Once the image is pinned, cut over: copy the GitHub data,
   repoint the webhook route, and turn GitHub off in `notifications`. Rollback is to repoint the
   route and turn GitHub back on.
6. A separate data-drop PR removes the GitHub tables, columns and foreign keys.

## Rename idea

`inboxes`, to match `sessions` and `environments`. It would touch:

- the Kubernetes names (Deployment, Service, ServiceAccount, settings ConfigMap, webhook HTTPRoute,
  platform route);
- `spiffe://agentplane/notifications`;
- proto package `agentplane.notification_service` and service `Notifications`;
- the `agentplane notifications` CLI;
- the `AGENTPLANE_NOTIFICATIONS_` settings prefix.

The logical database `notifications` would keep its name, because database renames are deferred.

## Open when this is picked up again

- The operator did not like the form of the seam yet.
- Shared or provider-owned subscription records.
- Whether Actions stays in the inbox binary or the Action Service appends its own results.
- Whether the GitHub data is copied or rebuilt from the API at cutover.
- The names.
