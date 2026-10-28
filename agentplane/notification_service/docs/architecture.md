# Notification Service internals

Client-visible behavior belongs in the [API reference](api.md); deployment and recovery procedures
belong in the [service README](../README.md). The store owns durable decisions and the worker
coordinates source reads and notice delivery. The integration app is only a client.

## Package boundaries

`model` owns subscription definitions, source-event identities, GitHub facts, selection rules and their
JSON encoding. It has no database, transport or worker dependency. `store` owns PostgreSQL transactions,
claims, refresh leases and durable recording; it consumes these types without defining their vocabulary.

`github/client` owns authenticated REST reads, pagination, response normalization and typed request
errors. It depends on `model` and the shared `github/settings`, not on persistence or scheduling.
The parent `github` package reconciles subscriptions and normalizes webhook deliveries; `webhook`
verifies signatures and handles HTTP ingress. `github/testing` provides the shared fake API used by
client and provider tests. The `worker` package schedules inbox work and delivers notices.

Access, subject, check-run, commit-status and review reads have distinct store completion methods.
Each validates its lease and access fences in the same transaction as its observations and wakeups.
Inbox recording retains its existing transaction boundary for deduplication, entries and checkpoints.

## Storage and scheduling

The service owns a PostgreSQL database and role on the shared cluster. Its image-coupled Goose
migrator keeps the advisory lock of the retired Alembic migrator; see
[`migrations`](../migrations/migrations.go) and the deployment instructions.

An inbox row lock serializes cursor allocation, payload insertion, overlapping subscription
matches and source-checkpoint advancement. Inbox entries hold their own payload copies, independent
of the raw GitHub receipt journal. Event identities survive inbox payload expiry for deduplication.

One PostgreSQL LISTEN connection per replica distributes committed invalidations, never payloads.
Workers drain durable state at startup and after reconnect. Inbox claims, source generations and
fenced commits prevent stale workers from overwriting newer work. Listener loss fails readiness.
Actions history is read through its API, not its database. GitHub refresh deadlines, provider
backoff, delivery retries and retention remain durable or scheduled work even without new ingress.
See [`worker`](../worker/worker.go), [`store`](../store/store.go) and
[`wakeups`](../../postgres/go/wakeups).

## Notice delivery and history

Persist command ID, exact input and coverage before submitting a notice through Dispatcher.
Before the first attempt, checkpoint the runner journal tail and persist the attempt marker.
A crash before that marker permits a newer initial checkpoint; after it, retries must not skip
entries. Missing runner entries must be accounted for by settled streamed-delta ranges.

History Service must reach the runner journal end before a command can be sent: lag delays
submission rather than hiding an admission. Replay verifies the last committed entry. Retries
reuse the command ID; admission is not harness confirmation. Confirmation follows causal
`origin_command_ids`, including coalesced inputs. Invalid runner history quarantines delivery
until explicit retirement. See [`deliver.go`](../worker/deliver.go).

## Shared GitHub state

Repository/subject identity is separate from App/installation/repository access. Names are
observations; numeric repository and installation identities fence access. Subscription bindings
keep both identities, while immutable creation parameters serve idempotency.

Shared access and subject refreshes use leases, persisted backoff and generation fences. Network
calls run outside database locks. Installation lifecycle webhooks invalidate cached grants and
in-flight refreshes. Recording inbox entries rechecks access generations and expiry under the
same lock as invalidation; a lost lease cannot overwrite a newer observation.

Subject-to-revision associations are additive and repository-qualified. They do not grant access
to a fork: matching also requires its validated installation/repository grant. Delivery-subject
links support events without a SHA; later association discovery can select earlier retained CI
receipts. Receipt positions are serialized against subscription creation. Expiring raw receipts
cascades to their subject links without removing independent inbox copies.

Shared failure observations belong to their access/subject records, not copies on every
subscription or inbox history. These are current-state records, not temporal versions. See
[`github_state.go`](../store/github_state.go), [`github_ingest.go`](../store/github_ingest.go),
[`github_deliveries.go`](../store/github_deliveries.go), and the
[schema](../migrations/notifications/00001_baseline.sql).

## Service identities

Workers use rotating, audience-specific projected tokens for Actions, Runner Gateway, Dispatcher
and History Service. Actions service-reader access does not confer operator or execution rights.
Kubernetes grants cover caller TokenReview, destination Sandbox reads and the watched
PolicySets/PolicyBindings used for send authorization. Network and reader allowlists are owned by
[`cluster/cdk8s/agentplane/notifications.py`](../../../cluster/cdk8s/agentplane/notifications.py),
not by the integration app. Operator watch streams use committed invalidations to reread current
state; PostgreSQL NOTIFY is not their replay log.
