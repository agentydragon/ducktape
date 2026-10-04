# Notification Service

Standalone Actions-first subscriptions and durable session inboxes. No integration-app API, database,
process, or issued identity is required. The service calls Actions for canonical event history and
Sandbox Service for destination validation and runner commands/receipts. It never opens a runner socket.

## Agent API

Workload bearer authentication uses the existing TokenReview foundation. Owner authority is the
verified ServiceAccount, shared by its workloads. Every creation specifies `destination_ref`
(namespace/name/UID) and `session_id`; inbox and subscription IDs identify subsequent operations.

- `GET /v1/providers`: provider schema discovery (Actions only).
- `POST /v1/subscriptions`: immutable Action request/filter and `idempotency_key`; identical retries return
  the same subscription, conflicting reuse returns 409. Replay defaults to sequence zero.
- `GET /v1/subscriptions[?after_id=…]`: ordered pages of 128; continue after the last ID.
- `GET`, `PATCH`, `DELETE /v1/subscriptions/{id}`: inspect, renew with an expected version,
  or idempotently cancel. Cancelling never cancels an Action or erases accepted entries.
- `GET /v1/inboxes`: the account's qualified session inboxes.
- `GET /v1/inboxes/{id}/entries?after_cursor=0&limit=128`: non-destructive, contiguous paging, prefix
  metadata, and the latest notice's admission/confirmation state. Payloads are stored Action event
  snapshots, not fetched at read time or promoted to operator instructions.
- `PUT /v1/inboxes/{id}/acknowledgement` with `through_cursor`: monotonically acknowledge the handled
  prefix. Future cursors are refused. Delivery and reads never acknowledge.
- `DELETE /v1/inboxes/{id}`: explicit retirement; no future matching or delivery. An already submitted
  runner command cannot be withdrawn. Missing endpoints or transient lookup failures are not retirement.

`idempotency_key` is caller-chosen and unique within an inbox (owning ServiceAccount, qualified sandbox
incarnation, and runner session), not across the entire sandbox. The server-generated subscription `id`
is used for subsequent GET/PATCH/DELETE. Reusing a key with different creation parameters returns 409;
reusing it after cancellation returns that cancelled subscription rather than creating a replacement.

Subscription creation and views use a provider-discriminated `source`; currently only `actions` is
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
fields are rejected. Discovery and OpenAPI describe the implemented variants; no GitHub support is
advertised yet. Inbox cursors, acknowledgement, and runner notices remain provider-neutral.

Platform prompts recommend a short synchronous wait for immediate Actions, subscriptions for approval
waits or parallel work, and resuming dependent work only after checking authoritative results. They
include worked subscribe/read/ack examples and the explicit destination identifiers. Automated notices
are not human instructions; agents acknowledge handled progress explicitly and cancel completed
subscriptions without withdrawing Actions or retiring the session inbox.
Prompt changes apply to newly opened sessions; existing runner sessions keep their immutable specs.

## Persistence and recovery

The service has its own PostgreSQL database and role on the shared cluster. Migrations use the shared
image-coupled Alembic runner and advisory migration lock. The app archive and runner volumes are untouched.

A short inbox row lock serializes cursor allocation, payload insertion, overlapping subscription
matches and source-checkpoint advancement. Provider identities are deduplicated per inbox. Reads
expose a committed prefix, not a sequence whose transactions can commit out of order.

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

V1 limits: 64 inboxes per account; 64 subscriptions per inbox including cancelled records; 10000
source-event identities per inbox lifetime; 128 entries per read/poll/replay step; subscriptions last
7 days by default and can be renewed up to 30 days at a time. A full inbox stops source progress with
an observable error rather than dropping events. Provider payloads expire after 30 days, preserving
identity tombstones to prevent replay duplicates. `expired_through` reports the resulting prefix gap
without advancing acknowledgement. Explicitly retired inboxes are purged after 30 days. After purge,
a new explicit subscription can establish a fresh inbox epoch; there is no implicit successor routing.

## Authorization and deployment

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
→ Actions/Sandbox Service/PostgreSQL/API server, and no notifications → runner path.

Bazel targets: `:server`, `:migrate`, `:image`, `:migration_image`; settings use
`AGENTPLANE_NOTIFICATIONS_*` (`main.py`). Deployment adds only its own database/role and service resources.
New images must be published and Flux image-policy tags selected before activating their rollout;
`unset` image pins are bootstrap placeholders, not runnable tags. On merge, the devel image-publishing
workflow builds/publishes the server and migration images; Flux must select both real tags. Until that
happens the new Deployment cannot become ready. Then verify the migration init container, `/readyz`,
and an Action subscription through a newly opened harness. Existing sessions need no migration and
keep their original prompt. No live migration/reset is part of this PR.

TODO: Add command-scoped delivery tracking through Sandbox Service, backed by the runner's existing
canonical command/events. Notifications needs admission, harness confirmation, and failure outcomes
for its own command ID, not conversation content. It should be possible to resume observing that ID
after a disconnect without maintaining a notification-owned runner-journal checkpoint. The tail
checkpoint above is the bounded-scope fix for now; this follow-up adds no second event authority.

Deferred: GitHub/webhooks, automatic subscriptions, cross-account delivery, per-thread credentials,
notification-triggered provisioning/resume, and proper runner RPC authentication/TLS.
