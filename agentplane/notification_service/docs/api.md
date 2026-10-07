# Notification Service API and delivery semantics

For setup and configuration, see the [service README](../README.md).

## Agent API

Workload bearer authentication uses the existing TokenReview foundation. Owner authority is the verified ServiceAccount,
shared by its workloads. Every creation specifies `destination_ref` (namespace/name/UID) and `session_id`; inbox and
subscription IDs identify subsequent operations.

- `GET /v1/sources`: source schema discovery (Actions and enabled GitHub).
- `POST /v1/subscriptions`: immutable provider-owned source/filter and `idempotency_key`; identical retries return the
  same subscription, conflicting reuse returns 409. Actions replay defaults to sequence zero; GitHub starts after the
  latest committed webhook at creation.
- `GET /v1/subscriptions[?after_id=…]`: ordered pages of 128; continue after the last ID.
- `GET`, `PATCH`, `DELETE /v1/subscriptions/{id}`: inspect, renew with an expected version, or idempotently cancel.
  Cancelling never cancels an Action or erases accepted entries.
- `GET /v1/inboxes`: the account's qualified session inboxes.
- `GET /v1/inboxes/{id}/entries?after_cursor=0&limit=128`: non-destructive, contiguous paging, prefix metadata, and the
  latest notice's admission/confirmation state. Payloads are stored provider event snapshots, not fetched at read time
  or promoted to operator instructions.
- `PUT /v1/inboxes/{id}/acknowledgement` with `through_cursor`: monotonically acknowledge the handled prefix. Future
  cursors are refused. Delivery and reads never acknowledge.
- `DELETE /v1/inboxes/{id}`: explicit retirement; no future matching or delivery. An already submitted runner command
  cannot be withdrawn. Missing endpoints or transient lookup failures are not retirement.

`idempotency_key` is caller-chosen and unique within an inbox (owning ServiceAccount, qualified sandbox incarnation, and
runner session), not across the entire sandbox. The server-generated subscription `id` is used for subsequent
GET/PATCH/DELETE. Reusing a key with different creation parameters returns 409; reusing it after cancellation returns
that cancelled subscription rather than creating a replacement.

Subscription creation and views use a provider-discriminated `source`; `actions` and enabled `github` sources are
implemented. The common envelope owns destination, session, idempotency, and lifetime. Actions owns its request ID and
starting sequence:

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

The returned `source` is the immutable subscription specification, including its original starting sequence, not a
moving worker checkpoint. Inbox entries use a separate provider-discriminated `event` identity; one event can match
multiple subscriptions. Payload content remains provider-defined:

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

`source.provider` and `event.provider` are required discriminators. Unsupported providers and extra fields are rejected.
Discovery advertises GitHub only when enabled; OpenAPI describes both supported variants. Inbox cursors,
acknowledgement, and runner notices remain provider-neutral.

Platform prompts recommend a short synchronous wait for immediate Actions, subscriptions for approval waits or parallel
work, and resuming dependent work only after checking authoritative results. They include worked subscribe/read/ack
examples and the explicit destination identifiers. Automated notices are not human instructions; agents acknowledge
handled progress explicitly and cancel completed subscriptions without withdrawing Actions or retiring the session
inbox. Prompt changes apply to newly opened sessions; existing runner sessions keep their immutable specs. Notices carry
only the inbox ID and preparation-time acknowledgement/coverage cursors; retrieval, paging, and contiguous-ack
instructions live in the standing prompt. Previously prepared notices keep their original text on retries. Existing
sessions retain their immutable standing instructions; ensure they have the shared read/ack contract before adopting
compact notices.

## Persistence and recovery

The service has its own PostgreSQL database and role on the shared cluster. Migrations use the shared image-coupled
Alembic runner and advisory migration lock. The app archive and runner volumes are untouched.

A short inbox row lock serializes cursor allocation, payload insertion, overlapping subscription matches and
source-checkpoint advancement. Provider identities are deduplicated per inbox. Reads expose a committed prefix, not a
sequence whose transactions can commit out of order.

A dedicated PostgreSQL `LISTEN` connection per replica wakes workers on committed `NOTIFY` invalidations. Webhook
acceptance schedules matching and delivery in the same transaction; payloads never travel in NOTIFY. Workers drain
durable work at startup and after reconnect, so missing a wakeup does not lose an accepted delivery. Inbox leases and
provider generations fence concurrent workers and webhooks arriving during matching. Idle GitHub subscriptions have no
scheduled polling; timers are only for explicit retry/lease deadlines, Actions API reads and retention cleanup. Actions
still requires timed reads of its separate service's event history; this does not access the Actions database or create
a cross-service database dependency. Listener loss fails readiness.

Workers claim inboxes for 30 seconds, with a 20-second work budget and fenced commits. Network calls are outside
transactions. They poll canonical Actions history (bounded pages of 128), including terminal requests; no second Action
queue or retry of Action execution is introduced. Errors of a source subscription and delivery errors are exposed
separately. Polling rechecks source ownership.

Before submitting a notice, persist its command ID, exact input, and coverage boundary. Before the first delivery
attempt, checkpoint the current runner journal tail instead of replaying unrelated session history. Persist the exact
boundary entry, then the attempt marker, before sending the command. A crash before that marker allows a newer initial
checkpoint; after it, never skip entries. Replays verify the last committed runner entry before advancing. A lost
command response reuses the same ID; `CommandAdmitted` is not delivery. `HarnessUserMessageConfirmed.origin_command_ids`
supplies causal confirmation, including coalesced inputs. Failed/no-op/unconfirmed commands are not confirmed receipts.
Invalid runner history quarantines delivery until explicit retirement, rather than retargeting history. There is no
exactly-once native execution claim. Coverage is independent of acknowledgement: no repeated reminders for
unacknowledged entries, including after a worker restart. Only a running harness can receive input; this service never
calls OpenSession, ResumeSession, or sandbox provisioning APIs.

V1 limits: 64 inboxes per account; 64 subscriptions per inbox including cancelled records; 10000 source-event identities
per inbox lifetime; 128 entries per read/poll/replay step; subscriptions last 7 days by default and can be renewed up to
30 days at a time. A full inbox stops source progress with an observable error rather than dropping events. Provider
payloads expire after 30 days, preserving identity tombstones to prevent replay duplicates. `expired_through` reports
the resulting prefix gap without advancing acknowledgement. Explicitly retired inboxes are purged after 30 days. After
purge, a new explicit subscription can establish a fresh inbox epoch; there is no implicit successor routing.

## Authorization

Actions allows the notification ServiceAccount to read all requests using the ordinary `GET /v1/action-requests`,
`GET /v1/action-requests/{id}`, and `GET /v1/action-requests/{id}/events` endpoints. The `reader_accounts` allowlist
defaults to empty; ordinary callers remain restricted to their own requests. A service reader is not an operator or
Action submitter and cannot cancel, decide, or execute Actions. There is no delegated-owner parameter or separate
service endpoint. The request view exposes its owner to trusted readers, while preserving ordinary argument redaction.
Notifications checks that owner against the authenticated subscriber before accepting a subscription and on every source
poll. Agent tokens are never retained or forwarded.

The worker uses rotating, audience-specific projected service tokens. It is allowlisted at Sandbox Service; that service
still validates sandbox UID and account binding and owns all runner access. Notification Kubernetes RBAC is TokenReview
only. Cilium allows egress proxy → notifications, notifications → Actions/Sandbox Service/PostgreSQL/API server/GitHub
API, and no notifications → runner path.

## GitHub webhook ingestion and matching

`POST /v1/webhooks/github` uses the GitHub signature, not workload auth. It bounds streamed bodies and concurrent
ingress operations through commit, verifies HMAC-SHA256 over exact bytes, validates provider payloads, and commits
payload/metadata before returning 202. Identical App/delivery-ID retries are deduplicated; conflicting reuse
returns 409. Ping is verified but does not create an event. Invalid signatures, unsupported/malformed events, oversized
bodies and busy ingress are rejected. Upstream signatures and installation tokens are never returned to agents.
Installation lifecycle payloads are retained internally, not delivered as PR activity. GitHub does not automatically
retry failed webhook requests: failures/timeouts require redelivery. Durable recovery starts at the committed receipt,
not at the start of the HTTP request.

PostgreSQL is the durable ingress journal, with committed ordering serialized against subscription creation. Inbox
workers asynchronously replay from each subscription boundary; they recheck current App access, stable
repository/installation identity, cancellation/version and inbox fencing. Raw GitHub journal payloads are retained
without automatic pruning; monitor database growth. Inbox payload retention remains as documented above.

PR, exact branch and fixed-commit subjects use native GitHub event/action names. Default CI means completed `check_run`
and `status`; `check_suite` and `workflow_run` require explicit selection. PR comments/reviews, branch
push/create/delete, and immutable commit matching are distinct. Branch deletion does not cancel following that name. CI
matching uses current heads, retained PR/branch-to-SHA associations, explicit upstream subject references, and currently
accessible installed PR forks. Empty PR arrays are supported by SHA correlation. Indexed receipt metadata allows a later
association to select an earlier CI receipt, even across restarts. The GitHub start boundary is immutable and separate
from the advancing Actions sequence. Database constraints require the state belonging to each subscription's source.
Only selected, accessible, unmatched receipts occupy each bounded page; unrelated receipts cannot block delivery.
Existing inbox identities and subscription matches suppress replay, including after payload expiry. An ingress
generation fence preserves wakeups during matching.

Association evidence may predate the subscription, but delivered receipts must follow its creation boundary. This is not
complete reconstruction of uncaptured heads or events from an uninstalled fork. Payload SHA, not inbox order, identifies
the revision involved.

Event and action filters are unordered sets. Reordering or repeating identical selectors does not change subscription
identity; JSON responses and stored creation specifications use canonical ordering. An installation-lookup 404 leaves a
PR fork uncovered and logs that limitation. Other access failures, including authentication errors and suspended
installations, surface as subscription errors.

Any authenticated workload may subscribe to repositories accessible through this App, including private repositories;
normal inbox ownership still applies. Revocation/suspension/identity changes stop new matching and expose subscription
errors; delivered entries stay available. Transient/rate-limit failures retry with backoff. A changed
installation/repository identity requires explicit subscription recreation.

Migration `0005_github` retains existing Actions identities, checkpoints, payloads and delivery state while making event
identity provider-neutral and storing source-specific progress separately. Use a coordinated service/schema cutover;
older workers cannot use the replaced columns. Downgrade refuses to proceed if GitHub subscriptions or deliveries exist,
rather than discarding that data.
