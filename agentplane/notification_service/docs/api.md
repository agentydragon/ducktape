# Notification Service API and delivery semantics

For setup and configuration, see the [service README](../README.md).

## Agent API

Workload bearer authentication uses the existing TokenReview foundation. Owner authority is the
verified ServiceAccount, shared by its workloads. Every creation specifies `destination_ref`
(namespace/name/UID) and `session_id`; inbox and subscription IDs identify subsequent operations.

- `GET /v1/sources`: source schema discovery (Actions and enabled GitHub).
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
- `DELETE /v1/inboxes/{id}`: explicit retirement; no future matching or delivery. An already submitted
  runner command cannot be withdrawn. Missing endpoints or transient lookup failures are not retirement.

`idempotency_key` is caller-chosen and unique within an inbox (owning ServiceAccount, qualified sandbox
incarnation, and runner session), not across the entire sandbox. The server-generated subscription `id`
is used for subsequent GET/PATCH/DELETE. Reusing a key with different creation parameters returns 409;
reusing it after cancellation returns that cancelled subscription rather than creating a replacement.

Subscription creation and views use a provider-discriminated `source`; `actions` and enabled `github` sources are
implemented. The common envelope owns destination, session, idempotency, and lifetime. Actions owns
its request ID and starting sequence:

```json
{
  "destination_ref": { "namespace": "agentplane-staging", "name": "sandbox-name", "uid": "sandbox-uid" },
  "session_id": "session-id",
  "idempotency_key": "follow-action",
  "source": {
    "provider": "actions",
    "request_id": "d49b85b5-849f-4e7d-a644-d4a8b8c16127",
    "after_sequence": 0
  }
}
```

The returned `source` is the immutable subscription specification, including its original starting
sequence, not a moving worker checkpoint. Inbox entries use a separate provider-discriminated `event`
identity; one event can match multiple subscriptions. Payload content remains provider-defined:

```json
{
  "cursor": 1,
  "event": {
    "provider": "actions",
    "request_id": "d49b85b5-849f-4e7d-a644-d4a8b8c16127",
    "sequence": 1
  },
  "payload": { "sequence": 1, "state": "decision_pending", "at": "2026-10-04T12:00:00Z", "actor": null },
  "subscriptions": ["ecf2527e-12cc-4506-b21c-7f82669466ce"]
}
```

`source.provider` and `event.provider` are required discriminators. Unsupported providers and extra
fields are rejected. Discovery advertises GitHub only when enabled; OpenAPI describes both supported variants. Inbox cursors, acknowledgement, and runner notices remain provider-neutral.

Platform prompts recommend a short synchronous wait for immediate Actions, subscriptions for approval
waits or parallel work, and resuming dependent work only after checking authoritative results. They
include worked subscribe/read/ack examples and the explicit destination identifiers. Automated notices
are not human instructions; agents acknowledge handled progress explicitly and cancel completed
subscriptions without withdrawing Actions or retiring the session inbox.
Prompt changes apply to newly opened sessions; existing runner sessions keep their immutable specs.
Notices carry only the inbox ID and preparation-time acknowledgement/coverage cursors; retrieval,
paging, and contiguous-ack instructions live in the standing prompt. Previously prepared notices
keep their original text on retries. Existing sessions retain their immutable standing instructions;
ensure they have the shared read/ack contract before adopting compact notices.

## Persistence and recovery

The service has its own PostgreSQL database and role on the shared cluster. Migrations use the shared
image-coupled Alembic runner and advisory migration lock. The app archive and runner volumes are untouched.

A short inbox row lock serializes cursor allocation, payload insertion, overlapping subscription
matches and source-checkpoint advancement. Provider identities are deduplicated per inbox. Reads
expose a committed prefix, not a sequence whose transactions can commit out of order.

A dedicated PostgreSQL `LISTEN` connection per replica wakes workers on committed `NOTIFY`
invalidations. Webhook acceptance schedules matching and delivery in the same transaction; payloads
never travel in NOTIFY. Workers drain durable work at startup and after reconnect, so missing a wakeup
does not lose an accepted delivery. Inbox leases and provider generations fence concurrent workers
and webhooks arriving during matching. Idle GitHub subscriptions have no scheduled polling; timers
are only for explicit retry/lease deadlines, Actions API reads and retention cleanup.
Actions still requires timed reads of its separate service's event history; this does not access the
Actions database or create a cross-service database dependency. Listener loss fails readiness.

Workers claim inboxes for 30 seconds, with a 20-second work budget and fenced commits. Network calls
are outside transactions. They poll canonical Actions history (bounded pages of 128), including
terminal requests; no second Action queue or retry of Action execution is introduced. Errors of a
source subscription and delivery errors are exposed separately. Polling rechecks source ownership.

Before submitting a notice, persist its command ID, exact input, and coverage boundary. Before the
first delivery attempt, checkpoint the current runner journal tail instead of replaying unrelated
session history. Persist the exact boundary entry, then the attempt marker, before sending the command.
A crash before that marker allows a newer initial checkpoint; after it, never skip entries. Replays
verify the last committed runner entry before advancing. A lost command response reuses the same ID;
`CommandAdmitted` is not delivery. `HarnessUserMessageConfirmed.origin_command_ids` supplies causal
confirmation, including coalesced inputs. Failed/no-op/unconfirmed commands are not confirmed receipts.
Invalid runner history quarantines delivery until explicit retirement, rather than retargeting history.
There is no exactly-once native execution claim. Coverage is independent of acknowledgement: no repeated
reminders for unacknowledged entries, including after a worker restart. Only a running harness can receive
input; this service never calls OpenSession, ResumeSession, or sandbox provisioning APIs.

Default V1 limits: 64 inboxes per account; 64 active (not cancelled and not expired) subscriptions per inbox; 10000
source-event identities per inbox lifetime; 128 entries per read/poll/replay step; subscriptions last
7 days by default and can be renewed up to 30 days at a time. A full inbox stops source progress with
an observable error rather than dropping events. Provider payloads expire after 30 days, preserving
identity tombstones to prevent replay duplicates. `expired_through` reports the resulting prefix gap
without advancing acknowledgement. Explicitly retired inboxes are purged after 30 days. After purge,
a new explicit subscription can establish a fresh inbox epoch; there is no implicit successor routing.

## Resource quotas

The Notifications Service Pydantic settings accept these deployment-configurable defaults:

```yaml
quotas:
  inboxes_per_account: 64
  active_subscriptions_per_inbox: 64
  entries_per_inbox: 10000
```

Each value must be positive. Environment overrides use the nested settings delimiter, for example
`AGENTPLANE_NOTIFICATIONS_QUOTAS__ACTIVE_SUBSCRIPTIONS_PER_INBOX=128`. Configure the same
quotas on all replicas. Lowering a quota does not delete retained data; it blocks new allocations
when capacity is exhausted. The inbox quota counts retained inboxes, and the entry quota counts
distinct event identities over an inbox's lifetime, including payload-expiry tombstones.

Cancelled and expired subscription records do not consume active capacity; they remain
available for history and idempotent replay. Renewing an expired subscription consumes
an active slot and is subject to the same limit as creating one. Renewing an already
active subscription does not consume another slot. Replaying a creation idempotency
key returns the retained subscription without reactivating it, even at the limit.

## Authorization

Actions allows the notification ServiceAccount to read all requests using the ordinary
`GET /v1/action-requests`, `GET /v1/action-requests/{id}`, and
`GET /v1/action-requests/{id}/events` endpoints. The `reader_accounts` allowlist defaults to empty;
ordinary callers remain restricted to their own requests. A service reader is not an operator or
Action submitter and cannot cancel, decide, or execute Actions. There is no delegated-owner parameter
or separate service endpoint. The request view exposes its owner to trusted readers, while preserving
ordinary argument redaction. Notifications checks that owner against the authenticated subscriber
before accepting a subscription and on every source poll. Agent tokens are never retained or forwarded.

The worker uses rotating, audience-specific projected service tokens. It is allowlisted at Sandbox
Service; that service still validates sandbox UID and account binding and owns all runner access.
Notification Kubernetes RBAC is TokenReview only. Cilium allows egress proxy → notifications, notifications
→ Actions/Sandbox Service/PostgreSQL/API server/GitHub API, and no notifications → runner path.

## GitHub webhook ingestion and matching

`POST /v1/webhooks/github` uses the GitHub signature, not workload auth. It bounds streamed bodies and
concurrent ingress operations through commit, verifies HMAC-SHA256 over exact bytes, validates provider payloads, and commits
payload/metadata before returning 202. Identical App/delivery-ID retries are deduplicated; conflicting
reuse returns 409. Ping is verified but does not create an event. Invalid signatures, unsupported/malformed
events, oversized bodies and busy ingress are rejected. Upstream signatures and installation tokens are
never returned to agents. Installation lifecycle payloads are retained internally, not delivered as PR activity.
HTTP 400/401 rejection logs include a parsed delivery UUID (or null for a malformed/missing ID),
a JSON-escaped event header capped at 64 characters, and a stable reason: `invalid_delivery_id`,
`invalid_signature`, `unsupported_event`, `missing_repository`, `payload_validation`, or the
fallback `invalid_delivery`. Validation logs include the total error count and at most eight
Pydantic error codes with known top-level schema fields. Nested locations, input values, error
messages/context, raw payloads and signatures are omitted. Event headers are untrusted labels,
not evidence of signature verification. Rejections retain their existing generic HTTP responses;
they do not create durable receipts or inbox entries.

GitHub does not automatically retry failed webhook requests: failures/timeouts require redelivery.
Durable recovery starts at the committed receipt, not at the start of the HTTP request.
If GitHub reports a failed delivery, an App operator should inspect the webhook's Recent Deliveries
in GitHub App settings and redeliver that delivery (or use GitHub's App delivery-attempt API
with App authority). A retry of an already committed receipt returns 202 with `duplicate: true`;
a retry of an uncommitted receipt returns 202 with `duplicate: false` after commit. Then check
subscription/inbox state separately: HTTP acceptance does not prove runner delivery or agent
acknowledgement. Do not replay with a new delivery ID or assume GitHub retries automatically.

An ordinary issue can be followed with `subject={"kind":"issue","number":123}`. Its defaults
are all `issues` lifecycle actions and ordinary `issue_comment` events; explicit action filters
can narrow these. Issue subjects do not accept CI events. The issue API also returns PRs, so
subscription authorization and repair reject responses with a `pull_request` marker; comments
with that marker remain PR events, not issue events. The installation token requires `issues:read`.

`workflow_job` payloads contribute their repository-qualified `head_sha` and optional branch
reference. They can match PR/branch/commit subscriptions that explicitly select `workflow_job`,
including an action filter such as `completed`. A job has no authoritative PR-number list:
PR matches reuse validated durable head associations, not a guessed PR from its branch name.
The full accepted job payload (job/run IDs, steps, status and conclusion) remains in the receipt
and delivered inbox entry. Ingress performs no extra GitHub job/run lookup.

PostgreSQL is the durable ingress journal, with committed ordering serialized against subscription
creation. Inbox workers asynchronously replay from each subscription boundary; they recheck current App
access, stable repository/installation identity, cancellation/version and inbox fencing. Raw GitHub journal payloads are retained without automatic pruning; monitor database growth.
Inbox payload retention remains as documented above.

PR, issue, exact branch and fixed-commit subjects use native GitHub event/action names. Default CI means completed
`check_run` and `status`; `check_suite`, `workflow_run` and `workflow_job` require explicit selection. PR comments/reviews,
branch push/create/delete, and immutable commit matching are distinct. Branch deletion does not cancel
following that name. CI matching uses current heads, retained PR/branch-to-SHA associations, explicit
upstream subject references, and currently accessible installed PR forks. Empty PR arrays are supported
by SHA correlation. Indexed receipt metadata allows a later association to select an earlier CI receipt,
even across restarts. The GitHub start boundary is immutable and separate from the advancing Actions sequence.
Database constraints require the state belonging to each subscription's source. Only selected, accessible, unmatched receipts occupy each bounded page;
unrelated receipts cannot block delivery. Existing inbox identities and subscription matches suppress
replay, including after payload expiry. An ingress generation fence preserves wakeups during matching.

Association evidence may predate the subscription, but delivered receipts must follow its creation
boundary. This is not complete reconstruction of uncaptured heads or events from an uninstalled fork.
Payload SHA, not inbox order, identifies the revision involved.

Event and action filters are unordered sets. Reordering or repeating identical selectors does not
change subscription identity; JSON responses and stored creation specifications use canonical ordering.
An installation-lookup 404 leaves a PR fork uncovered until a subsequent subject repair discovers
coverage. Authentication errors, suspended installations and refresh backoff are exposed through
shared access/subject observations in subscription introspection.

Any authenticated workload may subscribe to repositories accessible through this App, including private
repositories; normal inbox ownership still applies. Revocation/suspension/identity changes stop new matching
and expose shared access failures through subscription introspection; delivered entries stay available.
Transient/rate-limit failures retry with backoff. Shared observations expose safe `error` and absolute
`retry_at` values; these are not runner-delivery deadlines. A cancelled subscription can still show
shared observations maintained for another active subscriber.
Rate-limit retries respect both `Retry-After` and an exhausted primary quota's `X-RateLimit-Reset`,
with a minimum 60-second delay when GitHub supplies no later deadline. Workers log the shared entity,
safe failure category and retry delay, never upstream bodies or credentials.

Accepted webhooks remain durable while a source is backing off. New ingress does not shorten that
source's retry deadline; workers resume matching retained receipts after the deadline, including
after restart, and clear the error on success. No client-side resubscription or reconciliation is
needed for this recovery. Webhook deliveries GitHub never successfully submitted are outside that
guarantee. A changed installation/repository identity requires explicit subscription recreation.

Migration `0005_github` retains existing Actions identities, checkpoints, payloads and delivery state while
making event identity provider-neutral and storing source-specific progress separately. Use a coordinated service/schema cutover; older
workers cannot use the replaced columns. Downgrade refuses to proceed if GitHub subscriptions or deliveries
exist, rather than discarding that data.

## Operator notification diagnostics

The integration app (and only the configured `operator_reader_account` in the Notification Service's
namespace) can read `GET /operator/v1/sandboxes/{namespace}/{name}/notifications?uid=…` with a
Pod-bound, audience-specific workload token. This endpoint does not delegate agent inbox authority:
it returns no provider payloads, idempotency keys, notice text or mutation capability. The app verifies
the browser's operator login and resolves the current Sandbox UID before calling it; a reused name
cannot expose another incarnation. Workload `/v1` endpoints remain owner-scoped.

The sibling `GET /operator/v1/sandboxes/{namespace}/{name}/notifications/stream?uid=…`
uses the same authorization and returns snapshot SSE frames. It subscribes to committed
queue wakeups before its initial read; a reconnect always reads durable status anew,
so PostgreSQL NOTIFY is only an invalidation, not a replay log. Debounce deadlines
and subscription expiry also trigger snapshots when their displayed status changes.
The app proxies this stream under the operator session lifetime; the drawer follows
it only while open, retains the last snapshot during an outage, and marks it stale.

The snapshot groups inboxes by session and reports cursor counts, subscriptions, source errors and
the latest notice stage. `unannounced_count` counts retained entries beyond the maximum of covered,
acknowledged and expired cursors; `pending_acknowledgement_count` counts retained, non-expired
entries beyond acknowledgement (including entries already covered by a notice). A prepared or
confirmed notice does not acknowledge an entry. `notice_due_at`, `quiet_until` and `max_wait_at`
are exposed only when a new notice is waiting on the current quiet/max-wait debounce. When a
previous notice awaits confirmation or a delivery error needs retry, the waiting reason replaces
the debounce deadline. `next_work_at` is the worker's next scheduled inbox work, **not** a promise
of notice delivery; `next_source_check_at` is only the individual subscription's reconciliation
schedule. Offline runner states and event-dependent pacing have no guaranteed delivery time.

## Current source-processing observations

Subscription GET/list and the operator stream expose `last_success_at`, `error_kind`, `error`,
`error_since`, and `error_observed_at`. Failure kinds identify a cause: `rate_limited`, `unavailable`,
`access_denied`, `source_changed`, or `processing_error`. Typed exceptions and HTTP status codes
supply the classification; message text is not parsed. A successful committed pass clears all error
fields and advances `last_success_at`. Before the first success, that timestamp is null.

Repeated failures of the same kind preserve `error_since` and update diagnostic details, last
observation and retry deadline. A different kind starts a new `error_since`. Existing opaque errors
are adopted as `processing_error` at migration time; their earlier observation times are unknown.
The database constrains the error vocabulary and requires all four error fields to be jointly null
or non-null. There is no persisted summary health enum.

`next_attempt` is the durable scheduler deadline for retries or normal source work. API `retry_at`
is derived only for a current failure on an active subscription. Cancelled/expired subscriptions
retain their last observations without an active retry. Last successful processing does not prove
complete webhook coverage, current remote authorization, or successful runner delivery.

These updates never append inbox entries, advance cursors or prepare agent notices. The frontend
shows current source observations separately from subscription lifecycle and delivery status.
Shared GitHub access/repair failures belong to their shared records, not duplicated subscription
errors; subscription-owned observations describe that subscription's processing.

## GitHub entity ownership

GitHub subscription bindings reference shared repository-access and subject rows through composite
foreign keys. An App/installation/repository grant is distinct from a repository/subject identity;
the common repository ID prevents a subscription from binding a subject to an unrelated repository.
The immutable creation JSON is the original request for idempotency, not a mutable repository cache.
Repository names are observations, not primary keys. Matching installation IDs remain mandatory.

`github_installation` owns the App/installation identity and access invalidation generation.
`github_repository` owns the numeric repository identity and observed name.
`github_repository_access` owns validation timestamps (`checked_at`, `valid_until`), validation
failure facts, retry schedule and refresh lease for an App/installation/repository grant.
`github_subject` owns repair observations and lease for a PR, branch or commit.
`github_subject_revision` holds additive associations keyed by subject, head repository and SHA;
an association cannot grant access to the head repository.
`github_delivery_subject` links each receipt to its structured subject references through foreign
keys, including comments and branch events without a SHA. The delivery journal has no encoded
subject-string array; direct matching uses the indexed relation. Migration preserves existing
references before removing the old array and its GIN index.

Subscription `last_success_at` and current failure describe that subscription's processing. Shared
access or repair failures belong on the grant or subject. GET/list and operator status expose them
in `github.access` and `github.subject`, independently of the subscription's processing fields.
The `github` response field is required: Actions subscriptions explicitly return `null`; GitHub
subscriptions return the shared observations. Omission is not treated as `null`.
Each observation includes current failure facts, retry deadline and refresh lease deadline. Access
observations also expose validation time, expiry and `currently_valid` as of the read; this is not
a promise about later delivery. No refresh failure or recovery creates an inbox entry.

The normalized binding migration preserves subscription IDs, inboxes, event checkpoints and raw
receipts. It does not claim historical validation times. Reverse migration refuses to discard
shared observations or revision associations.

### Durable GitHub refresh and matching

`github.freshness_seconds` bounds cached access validation and subject repair (default 600 seconds,
configurable from 60 to 3600). Successful processing schedules the earliest access expiry or subject
repair deadline, including when no webhook arrives. A missing webhook is repaired for current
subject metadata, not replayed as an activity event.

Workers claim shared access/subject refreshes through PostgreSQL leases. Other workers reuse the
observation or defer until the persisted retry/lease deadline; no network call occurs under a database
lock. A worker that loses its lease cannot overwrite a newer success or failure. Signed installation
and installation-repository lifecycle events invalidate cached access and in-flight refreshes with
generation fences. Ordinary activity webhooks add repository-qualified SHA associations; delayed
observations cannot erase newer associations.

Matching uses durable observations and makes no GitHub API calls while they remain fresh. Every
inbox append rechecks the access generations and expiry under the same transaction lock used by
invalidation. Stale access fails closed. Fork SHA associations alone never authorize a fork receipt:
its repository and installation must also match a separately validated grant. A missing head
repository in a PR payload does not infer fork identity. Shared failures remain queryable after a
restart and never become per-subscription copies or inbox history.

These are current-state tables, not temporal versions. `github_subject_revision` accumulates observed
membership without ordering or a current-head marker. Retained webhook receipts remain the event
journal; repository names, access observations, refresh failures and leases are updated in place.
