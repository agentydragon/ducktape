# Notification Service API

The workload API is defined by [`notifications.proto`](../notifications.proto); its HTTP form is
served at `/openapi.json`. The operator API is defined separately by
[`notifications_operator.proto`](../notifications_operator.proto). Setup and configuration belong
in the [service README](../README.md); internal delivery and storage design is in
[architecture](architecture.md).

## Agent API

Workload calls require a Pod-bound Kubernetes ServiceAccount bearer token with the configured
audience. Owner authority is the verified ServiceAccount, shared by its workloads. Every creation specifies `destination_ref`
(namespace/name/UID) and `session_id`; inbox and subscription IDs identify subsequent operations.
The caller's inboxes and subscriptions are those opened for the Sandbox that runs as its account now: a Sandbox
recreated under a deleted one's name sees none of the deleted one's, and an account no Sandbox runs as has none.

- `POST /v1/subscriptions`: immutable provider-owned source/filter and `idempotency_key`; identical retries return
  the same subscription, conflicting reuse returns 409. Actions replay defaults to sequence zero;
  GitHub starts after the latest committed webhook at creation.
- `GET /v1/subscriptions[?after_id=…]`: ordered pages of 128; continue after the last ID.
- `GET`, `PATCH`, `DELETE /v1/subscriptions/{id}`: inspect, renew with an expected version,
  or idempotently cancel. Cancelling never cancels an Action or erases accepted entries.
- `GET /v1/inboxes`: the account's qualified session inboxes.
- `GET /v1/inboxes/{id}/entries?after_cursor=0&limit=128`: non-destructive, contiguous paging, prefix
  metadata, and the latest notice's admission/confirmation state. Payloads are stored provider event
  snapshots, not fetched at read time or promoted to operator instructions.
- `PUT /v1/inboxes/{id}/acknowledgement` with `through_cursor`: monotonically acknowledge the handled
  prefix. Future cursors are refused. Delivery and reads never acknowledge.
- `DELETE /v1/inboxes/{id}`: explicit retirement; no future matching, messages or delivery. An already
  submitted runner command cannot be withdrawn. Missing endpoints or transient lookup failures are not retirement.
- `POST /v1/messages`, `GET /v1/messages/{id}`: send a message to a Session's inbox, and follow one the caller
  sent ([Messages](#messages)).

`idempotency_key` is caller-chosen and unique within an inbox (owning ServiceAccount, qualified sandbox
incarnation, and runner session), not across the entire sandbox. The server-generated subscription `id`
is used for subsequent GET/PATCH/DELETE. Reusing a key with different creation parameters returns 409;
reusing it after cancellation returns that cancelled subscription rather than creating a replacement.

Subscription creation and views use a `source` that is a oneof: `actions` or `github`. The common envelope
owns destination, session, idempotency, and lifetime. Actions owns its request ID and starting sequence.
The HTTP form of the gRPC-Gateway writes JSON field names in camelCase and 64-bit integers as strings, and
accepts either spelling in requests ([`notifications.proto`](../notifications.proto) is the contract):

```json
{
  "destinationRef": { "namespace": "agentplane-staging", "name": "sandbox-name", "uid": "sandbox-uid" },
  "sessionId": "session-id",
  "idempotencyKey": "follow-action",
  "source": {
    "actions": { "requestId": "d49b85b5-849f-4e7d-a644-d4a8b8c16127", "afterSequence": 0 }
  }
}
```

The returned `source` is the immutable subscription specification, including its original starting
sequence, not a moving worker checkpoint. Inbox entries use a separate `event` identity, also a oneof; one
event can match multiple subscriptions. Payload content remains provider-defined:

```json
{
  "cursor": "1",
  "event": {
    "actions": { "requestId": "d49b85b5-849f-4e7d-a644-d4a8b8c16127", "sequence": 1 }
  },
  "payload": { "sequence": 1, "state": "decision_pending", "at": "2026-10-04T12:00:00Z", "actor": null },
  "subscriptionIds": ["ecf2527e-12cc-4506-b21c-7f82669466ce"]
}
```

Pull-request subscriptions include the Agentplane-generated `mergeability_changed` selector by
default. Selecting it explicitly replaces the defaults and accepts no `actions` filter. The service
records an initial observation only when the pull request is conflicted, then records later changes
to either mergeable or conflicted; it creates no inbox entry while the result is pending. The first
conflicted event may use a cached current observation made before subscription creation, so its
`observedAt` can predate the subscription. The service
monitors both the pull-request head and base, and retries pending mergeability checks, so an agent can
end its turn after confirming the subscription and reading current state. These entries use
`event.githubMergeability` with `appId`, `repositoryId`, `pullRequestNumber`, and a durable globally
increasing `sequence`, not a GitHub webhook delivery UUID. Their payload uses camelCase fields
`repository`, `pullRequestNumber`, `baseRef`, `headSha`, `baseSha`, `previousMergeability` (`UNKNOWN` for the initial observation), `mergeability` (`MERGEABLE` or `CONFLICTING`), and `observedAt` (timestamp).

A `source` with neither field set is rejected as `INVALID_ARGUMENT`. A service without the GitHub App refuses
a `github` source with `PERMISSION_DENIED`; no route lists enabled providers. Inbox cursors,
acknowledgement, and runner notices remain provider-neutral.

Agent workflows are served through [`/llms.txt`](agents/llms.txt). Notices identify the inbox
and its preparation-time acknowledgement/coverage cursors; reading and acknowledging the inbox
remain explicit client operations. A retried notice retains its original text.

## Messages

A workload sends a message to a Session's inbox when a `session_inbox_access` rule of a PolicySet bound to its
ServiceAccount allows `send_message` to the Session's Sandbox. A rule targets Sandboxes of its PolicySet's
namespace by name, optionally pinned to one incarnation by `uid`, by inbox tag (`inboxTags`), or all of them with
`allSandboxes`; grants combine additively. A target without `uid` follows a Sandbox deleted and recreated under its
name. A tag target matches the Sandbox's `spec.inboxTags` as the service reads it at acceptance, so removing the tag
stops new sends to it, and a deleted Sandbox matches nothing. The service
refuses sends as `UNAVAILABLE` until its permission view is initialized, or while it is stale.
A grant or revocation takes effect on each replica as that replica observes it. Revocation stops new sends
only; accepted messages stay in the inbox. The recipient needs no subscription or consent of its own.

A send names the Sandbox (namespace and name; `uid` optional) and the Session ID, as a subscription does. The
service pins the UID of the Sandbox it finds and refuses a missing Sandbox, one outside its namespace or another
incarnation than a given `uid` as `NOT_FOUND`; it never retargets. A suspended Sandbox accepts messages; its
notice waits until the Sandbox runs again unless wake-on-send is enabled and authorized for this
service. The inbox belongs to the Sandbox ServiceAccount for that Session. Creating a recipient inbox
requires retained Session history associated with that Sandbox; missing or unrelated history returns
`NOT_FOUND`. This association checks the Sandbox name, so a Session of a deleted predecessor with
the same name can pass. Sending to an existing inbox does not repeat the history check.

A message is an inbox entry like any other, with the same cursors, notices, acknowledgement, payload expiry and
purge. Its event identity is the message ID and the sender ServiceAccount the service authenticated; its payload
is the `body` and the optional `from_session`, the sender's unverified label for the Session it writes from. The
idempotency key is unique per sender within the inbox: an identical retry answers with the accepted message,
also after its payload expired, and other content under the key is `ALREADY_EXISTS`. Bodies are UTF-8, at most
`message_body_limit_bytes`, with an absolute maximum of 256 KiB. A sender may have
`quotas.unacknowledged_messages_per_sender` messages in an inbox beyond its acknowledged cursor; messages do not
count towards `quotas.entries_per_inbox`.

`GET /v1/messages/{id}` answers only the sender, whatever its current grants, with three independent facts from
the inbox's cursors: `covered` (a notice named the message), `acknowledged` (the acknowledged prefix reaches it)
and `inbox_retired`. It shows no cursors or other entries. Another account's message, an unknown ID and a
message whose inbox was purged are all `NOT_FOUND`.

## Delivery, retention and quotas

Entries form a committed contiguous prefix and are deduplicated by provider event identity within
an inbox. One entry can match multiple subscriptions. Reading, notice preparation and notice
confirmation never acknowledge entries; only explicit acknowledgement advances that prefix.
Notice admission and harness confirmation are distinct states, and neither proves that an agent
handled the entries. Unacknowledged entries do not cause repeated reminders. Notification delivery
does not promise exactly-once native execution.

Provider payloads expire after 30 days while their event identities remain deduplicated.
`expired_through` reports the resulting prefix gap without advancing acknowledgement. Retired
inboxes are purged after 30 days. After purge, a new explicit subscription can establish a fresh
inbox; there is no implicit routing to a successor Sandbox or Session.

Subscriptions default to seven days and can be renewed for up to 30 days at a time. Configured
[quotas](../README.md#resource-quotas) bound retained inboxes, active subscriptions, source-event
identities and unacknowledged messages. A full inbox stops source progress with an observable
error. Lowering a quota blocks new allocations without deleting retained data. Cancelled and
expired subscriptions remain inspectable and idempotently replayable without consuming active
capacity. Renewing an expired subscription needs an active slot; renewing an active one does not
consume another slot. Replaying a creation key returns its retained subscription without
reactivating it, even at the limit.

## Source authorization

An Actions subscription must name a request owned by the authenticated subscriber; ownership is
checked at creation and during processing. A subscription grants no ability to cancel, decide or
execute that Action. Agent tokens are not retained or forwarded to source services.

Any authenticated workload may subscribe to repositories accessible through the service's GitHub
App, including private repositories. Inbox ownership still scopes reads and management. Access
revocation or a changed installation/repository identity stops new matching; delivered entries
remain readable. A changed identity requires explicit subscription recreation.

## GitHub webhook ingestion and matching

`POST /v1/webhooks/github` authenticates GitHub deliveries with `X-Hub-Signature-256`, not a
workload bearer. It requires a UUID `X-GitHub-Delivery`, `X-GitHub-Event`, and a signature over the
exact request body. Successful acceptance returns HTTP 202 with `accepted: true` and `duplicate`.
An identical retained App/delivery-ID retry returns `duplicate: true`; conflicting content under
the same retained ID returns 409. A signed ping returns 202 without creating an activity event.

Invalid signatures return 401; invalid delivery IDs, malformed or unsupported events return 400;
oversized bodies return 413. Saturated ingress returns 503 with `Retry-After: 5`. Acceptance means
the delivery is durably stored, not that it matched a subscription, reached a runner or was
acknowledged. Rejected deliveries create no inbox entries. Installation lifecycle events are not
PR activity. Upstream signatures and installation tokens are never returned to agents.

An ordinary issue can be followed with `subject={"issue":{"number":123}}`. Its defaults
are all `issues` lifecycle actions and ordinary `issue_comment` events; explicit action filters
can narrow these. Issue subjects do not accept CI events or PR numbers; PR comments remain PR
events rather than ordinary issue events.

`workflow_job` payloads contribute their repository-qualified `head_sha` and optional branch
reference. They can match PR/branch/commit subscriptions that explicitly select `workflow_job`,
including an action filter such as `completed`. A job has no authoritative PR-number list:
PR matches use observed head associations rather than inferring a PR from its branch name.
Delivered job payloads contain the accepted job/run IDs, steps, status and conclusion.

GitHub subscriptions consider accepted deliveries after their creation boundary. Catch-up,
late PR/commit association matching and delivery-ID deduplication are bounded by the configured
[raw delivery retention](../README.md#raw-github-delivery-retention). A delivery that expires before
matching is no longer available for catch-up. Redelivery after expiry is accepted as new, but an
inbox already containing the event still deduplicates it. Delivered inbox payload copies have
their own 30-day lifetime.

PR, issue, exact branch and fixed-commit subjects use native GitHub event/action names. Default CI means completed
`check_run` and `status`; `check_suite`, `workflow_run` and `workflow_job` require explicit selection. PR comments/reviews,
branch push/create/delete, and immutable commit matching are distinct. Branch deletion does not cancel
following that name. CI matching uses current heads, retained PR/branch-to-SHA associations, explicit
upstream subject references, and currently accessible installed PR forks. Empty PR arrays are supported
by SHA correlation. A later observed association can make an earlier retained CI delivery eligible.
Already delivered event identities remain deduplicated, including after inbox payload expiry.

Association evidence may predate the subscription, but delivered receipts must follow its creation
boundary. This is not complete reconstruction of uncaptured heads or events from an uninstalled fork.
Payload SHA, not inbox order, identifies the revision involved.

Event and action filters are unordered sets. Reordering or repeating identical selectors does not
change subscription identity; responses use canonical ordering.
An installation-lookup 404 leaves a PR fork uncovered until a subsequent subject repair discovers
coverage. Authentication errors, suspended installations and refresh backoff are exposed through
shared access/subject observations in subscription introspection.

Transient/rate-limit failures retry with backoff. Access and subject observations expose `error`
and absolute `retry_at` values; these are not runner-delivery deadlines. A cancelled subscription
can still show shared observations maintained for another active subscriber.

Accepted webhooks remain durable within the raw receipt retention window while a source is backing off. New ingress does not shorten that
source's retry deadline; workers resume matching retained receipts after the deadline, including
after restart, and clear the error on success. No client-side resubscription or reconciliation is
needed for this recovery. Webhook deliveries GitHub never successfully submitted are outside that
guarantee. A changed installation/repository identity requires explicit subscription recreation.

## Operator notification diagnostics

`NotificationsOperator` is a gRPC-only, read-only API for the configured
`operator_reader_account` in the service's namespace, using a Pod-bound token with the service's
audience. The service exposes no HTTP routes for this API. It returns no provider
payloads, idempotency keys or notice text and grants no agent inbox authority.

- `WatchSandboxNotifications` takes namespace, Sandbox name and current UID. It reports inboxes
  (including retired ones), subscriptions, source failures, entry counts and latest notice state.
- `WatchSessionNotices` takes at most 200 Session IDs. It reports non-retired inboxes in the
  caller's namespace: `queued` entries not yet covered by a notice, `unacknowledged` covered
  entries, latest notice progress/error, wait reason and estimated `eligible_at` while debouncing.

Streams open with a snapshot and send another when displayed state changes, including changes
at debounce or subscription-expiry deadlines. A snapshot differing only in `observed_at` is not
sent. Reconnecting starts from a fresh snapshot; streams do not replay historical updates.
Reading acknowledges nothing. Planned renewal emits `reconnect_required`; bare EOF is a failure.

Sandbox snapshots expose `unannounced_count` beyond covered, acknowledged and expired cursors;
`pending_acknowledgement_count` includes covered entries still awaiting acknowledgement.
`notice_due_at`, `quiet_until` and `max_wait_at` apply while waiting on debounce. Awaiting
confirmation or retrying a delivery instead exposes that wait reason. `next_work_at` and
`next_source_check_at` are scheduling observations, not promises of notice delivery.

PR observations retain GitHub's lifecycle states `OPEN`, `CLOSED` (without merging), and `MERGED`
separately from mergeability states `MERGEABLE`, `CONFLICTING`, and `UNKNOWN`. Only open PRs generate
mergeability notifications or schedule retries for `UNKNOWN`; terminal lifecycle states do neither.

## Current source-processing observations

Subscription reads and operator snapshots expose `last_success_at`, `error_kind`, `error`,
`error_since`, and `error_observed_at`. Failure kinds are `rate_limited`, `unavailable`,
`access_denied`, `source_changed`, and `processing_error`. Success clears the failure fields and
advances `last_success_at`, which is null before the first successful processing pass.

Repeated failures of one kind preserve `error_since` and update their details and observation
time. A different kind starts a new `error_since`. Subscription `retry_at` is set for a current
failure on an active subscription. Cancelled and expired subscriptions retain their observations
without an active retry. A successful pass does not prove complete webhook coverage, current
remote authorization or runner delivery. Observation changes do not create inbox entries or
acknowledge anything.

GitHub subscriptions additionally expose access and subject observations in `github.access` and
`github.subject`, separately from subscription processing. Actions subscriptions return no
GitHub status. Access observations include validation time, expiry and `currently_valid` at the
time of the read; that is not a promise about later delivery. Observations include failure facts,
retry and refresh-lease deadlines. `github.subject.retryAt` also gives the next refresh time while
mergeability is pending; this does not imply a failure or a pending inbox notice. Subject refreshes
monitor both PR head and base changes and retry pending mergeability checks. They can produce
mergeability observations but do not recover missing webhook activity.
