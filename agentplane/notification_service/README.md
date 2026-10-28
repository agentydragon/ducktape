# Notification Service

Standalone Actions and GitHub App subscriptions, messages between workloads, and durable session inboxes. The
service reads Action history over the generated ActionService gRPC API, reads destination Sandboxes from their Sandbox resources, validates Sessions through
the read-only Runner Gateway, sends every runner notice through the Session Dispatcher, and reads command receipts
from the Session's runner entries in the History Service. It authorizes messages by the `session_inbox_access`
rules of the PolicySets bound to the sender, which it watches. It does not depend on the integration app or
connect directly to runners.

Agents subscribe with an explicit destination/session and an idempotence key, then retrieve stored
payloads when notified. Reads are non-destructive; acknowledgement explicitly advances the handled
contiguous prefix. Unacknowledged entries do not cause repeated reminders.
For UID-pinned destinations, workers retire an inbox only after two Sandbox lookups
confirm that its managed Sandbox is missing or has a different UID, separated by the
configured `stale_inbox_confirmation_s` grace period. Matching UIDs (including
suspended Sandboxes) resume delivery; owner mismatches and transport failures never
confirm retirement. Retirement cancels subscriptions and stops retries but retains entries
and notices for normal retention and inspection; it never acknowledges them or rebinds
an inbox to a replacement Sandbox.

Agent entry point: [`/llms.txt`](docs/agents/llms.txt), with linked subscription, inbox-handling,
GitHub and messaging workflows served under `/docs/agents/`, and `/openapi.json` as the HTTP reference. Update the guides
when those contracts change.

- [API and caller-visible delivery semantics](docs/api.md)
- [Storage, worker and delivery architecture](docs/architecture.md)
- [Staging GitHub acceptance record](docs/staging_github_acceptance.md) — live CI/comment delivery and its limits.
- `/openapi.json`: the routes and their request and response schemas.

While the History Service is down or rolling, webhooks are still accepted and inbox entries still
stored, but no notice reaches a Session: a delivery reads the Session's receipts from the History
Service first and, failing that, releases its claim to retry later. A delivery also waits while the
History Service lags the runner's journal, so a retry never precedes an admission it has not seen.

## Running and deployment

`:service_image` (binary `:notification-service`) is the service, and `//agentplane/notification_service/migrations:image`
its Goose migrator, which the Deployment's init container runs before the service starts. The service needs its own
PostgreSQL database, Actions, Runner Gateway, History Service and Session Dispatcher endpoints, projected
service-account tokens, Kubernetes read access to Sandboxes, PolicySets and PolicyBindings, and `patch` on
PolicyBinding status. Preserve existing staging data. Verify an Action
subscription through a newly opened harness after rollout. Existing sessions retain their original prompt.

A schema change is a Goose file in `migrations/notifications/`. The baseline, `00001_baseline.sql`, takes over a
database that the retired Alembic history left at revision `0010_drop_write_only_columns` and refuses one behind it;
the migrator keeps the advisory lock key the Alembic runner took.

The generic `github_observation` inbox identity is read-compatible here for the planned CI/check, review
and PR lifecycle recovery writers. [Reader PR #12411](https://github.com/agentydragon/ducktape/pull/12411) must deploy to all replicas
before deploying these writers; retain its image as the data-preserving rollback floor once writing begins.
Do not overlap old GitHub workers with recovery writers during rollout. If rolling back to the reader-only image,
disable the GitHub provider until a recovery-capable image is restored, while retaining inbox reads and delivery.
Never roll back to an older decoder or run a down migration that discards retained notifications.

Entry source identities are being moved from JSONB equality to explicit relational natural keys. The expansion
migration adds nullable columns and the serving writers dual-write them while the existing JSONB unique index
continues to deduplicate. After every writer that predates the dual write has been rolled out, run the migrator
image with `backfill-event-identities --writer-gate=notification-event-identity-dual-write-v1`; it resumes in short `(inbox_id, cursor)` batches, checks for legacy JSON
shapes that collide under the natural key, validates the shape constraint, and builds the unique natural-key
indexes concurrently. A collision stops the command with exact inbox/cursor groups, retaining every original
entry and JSON document for explicit resolution. The command is separate from Pod init, so retained inboxes do
not hold serving readiness while they are scanned. This phase keeps serving deduplication on JSONB. Do not roll
the runtime release that reads and deduplicates by relational keys until the command has completed with phase
`indexed` on every service database, including newly initialized databases; an empty database still needs the
natural-key indexes. The runtime release has no JSONB fallback or per-append readiness probe. Keep the JSONB index
until every old writer has drained and every reader has moved to relational keys.

Native entry content is a separate phase. `00014_entry_native_content_expand.sql` adds nullable relational columns for
Actions events, generated mergeability observations and messages. New writers fill it while retaining the existing
JSON payload for older readers; cleanup clears both physical representations in one transaction. GitHub webhook
payloads remain native JSONB and do not use the native columns. After every writer and cleanup worker runs that
dual-write release, run
`bb run //agentplane/notification_service/migrations/cmd:notifications-migrate -- backfill-entry-content --writer-gate=notification-entry-native-content-dual-write-v1`
once against each Notification database. The command is resumable outside Pod init, walks every entry in native
`(inbox_id, cursor)` primary-key order, and commits short batches bounded by entry count and one MiB of serialized
JSON (a single oversized row is processed alone). It never loads GitHub webhook JSON. It also physically clears
native columns left by an older cleanup writer wherever the legacy payload is already NULL, including entries below
an inbox's `expired_through` cursor. It verifies both conditions over the full table before reporting completion.
Do not switch readers to native-only content until it completes on every database, including newly initialized
databases. During rollout, an older cleanup replica can clear only the legacy JSON column; a future native reader
must treat that SQL `NULL` as expired even if native columns remain, until the all-cleanup-writers gate and backfill
have completed.

To roll back before the content backfill runs, restore the previous service image and leave the additive column in
place; do not run a migration's `Down` section while dual writers remain. If the content command has started or
completed, leave its checkpoint and native columns in place too; the previous image continues to use the retained
JSON payload. If the event identity backfill/index command has completed, leave its columns, checkpoint and
natural-key indexes in place as well. After the rollback rollout settles, verify the previous API's reads, writes,
replay deduplication and readiness. Only after no reader depends on relational keys should a later cleanup drop the
natural-key indexes concurrently, then run the migration Down sections in reverse order. Before a later forward
rollout, drain old writers and check for any unconverted rows; the backfill command stops with their inbox/cursors
instead of silently cutting over.

`00009_inbox_destination_expand.sql` adds nullable relational destination columns while the service continues to
dual-write them and reads legacy `destination_ref` JSON when needed. After every Notification Service writer runs
the dual-write release, run `bb run //agentplane/notification_service/storage_backfill/cmd:notification-destination-backfill -- --writer-gate=notification-destination-dual-write-v1`
once against the service database. The command is resumable, uses short bounded batches, and preserves the JSON
column; it is not part of service startup or readiness.

`00012_subscription_creation_expand.sql` adds a binary typed snapshot and relational source discriminator while
the service continues to dual-write and can still read retained `creation` JSON. After every Notification Service
writer runs the dual-write release, run
`bb run //agentplane/notification_service/storage_backfill/subscription_creation_cmd:notification-subscription-creation-backfill -- --writer-gate=notification-subscription-creation-dual-write-v1`
once against the service database. This resumable command uses short bounded batches, stores its UUID cursor with
each batch, and preserves the JSON creation record as a rollback copy; it is not part of startup or readiness.

`00013_subscription_creation_typed_only.sql` requires the backfill completion marker on a populated database. After
the command reports completion, roll out the native-only release to every reader and writer. It uses only the typed
snapshot and leaves new `creation` JSON rows NULL; the JSON column remains as a rollback copy for rows created by
earlier releases. The migration also replaces JSON-dependent source constraints with typed source constraints.
After every writer runs the native-only release, run
`bb run //agentplane/notification_service/storage_backfill/storage_validate_cmd:notification-storage-validate -- --proof=subscription-creation --writer-gate=notification-subscription-creation-native-only-v1`.
This command validates each typed constraint in its own transaction with a short lock wait and is safe to rerun.
Run it even on an empty database so later writes have validated proofs; a later contract migration may use an
empty-table fast path, but requires all four catalog proofs before dropping the retained JSON column on a populated
database.

Before the native-only release, roll back to the dual-write release while retaining all additive migrations and
columns; it prefers typed snapshots and can read rows written by the native-only release. Do not roll back to an image
that predates typed-snapshot support once `00013` is applied. Keep `00013` applied during code rollback, because its
Down step requires every row to have a JSON creation again. After the rollback rollout settles, verify readiness,
inbox authorization, and read/write behavior. A later contract migration may drop the unused JSON column only after
all readers and writers use typed storage; until then, the native-only migration retains it for the compatible
dual-write rollback.

`00015_inbox_destination_typed_only.sql` requires the inbox-destination backfill completion marker on populated
databases, makes the retained `destination_ref` JSON nullable, and adds a not-yet-validated shape constraint for the
relational namespace, name and UID columns. Roll the native-only reader and writer to every Notification Service
instance only after the `00009` backfill reports completion. The new reader uses only the relational columns, and
new inboxes leave `destination_ref` NULL. Once every writer has the native-only release, run
`bb run //agentplane/notification_service/storage_backfill/storage_validate_cmd:notification-storage-validate -- --proof=inbox-destination --writer-gate=notification-destination-native-only-v1`.
It validates the shape constraint in a separate bounded transaction, including on an empty database. A later
contract migration requires that validated catalog proof before making the relational fields mandatory and dropping
the retained JSON column.

Before the contract migration, roll back only to the typed-capable dual-write release: it reads relational fields
and can still read retained legacy JSON, and it repopulates JSON for new inboxes. Keep the additive columns and
`00015` applied during rollback; its Down step refuses to run while any native-only row lacks JSON. After rollback,
check API ownership, inbox authorization, writes, reads and readiness. Once the contract migration drops JSON, an
older image that depends on `destination_ref` is no longer a supported rollback.

The service reads the settings file and environment variables of [Configuration](#configuration). It takes no
flags, and no `log_levels`; logs are JSON on stderr. `request_timeout_s` (default 15) bounds a call's backend work,
and `watch_lease_s` (default 300) is how long a watch lasts before it ends with `reconnect_required`.
`message_body_limit_bytes` (default 32768, at most 262144, which the database also holds) bounds a message body.
`policy_watch_period_s` (default 60, at least 2) is how long each watch of PolicySets and PolicyBindings stays
open; sends are refused once a watch has missed three. GitHub is on
when any `github.*` setting is set, so a Secret without `github.app_id`, or the reverse, fails startup. A setting
that fails validation stops startup without printing its value.

One listener (`host`, `port`) serves gRPC (`Notifications`, `NotificationsOperator` and `grpc.health.v1`), the HTTP
form of `Notifications` (the routes of [`notifications.proto`](notifications.proto); the operator service has none),
the agent documentation (`/llms.txt`, `/docs/agents/` and `/openapi.json`, the document generated from that proto,
which answer without the database) and, when GitHub is on, `POST /v1/webhooks/github`. Health checks take no bearer: service `""` is readiness (the LISTEN
connection is up and a query succeeds; it fails once the service is told to stop) and `livez` is liveness (the
process answers). Four workers claim inboxes, and one that returns or fails ends the process, logging the types of
its error and never its message.

## Configuration

Set `AGENTPLANE_NOTIFICATIONS_CONFIG_FILE` to a YAML file; a missing file and unknown keys fail startup.
Environment variables override YAML using `__` for nested fields. [settings.py](settings.py) defines the fields the
manifests render and their descriptions, and `config.go` with the settings of its packages reads them.
Configuration changes require a service restart.

`actions` retains its `url` and `token_file` settings, plus optional `request_timeout_s` (default 5).
The URL must be an HTTP(S) origin; Go derives the gRPC host and port from it, using h2c for `http`
and TLS for `https`. `runner_gateway` uses `control_target` and `token_file` for per-Sandbox
runner live-state checks.
`history_service` and `session_dispatcher` each contain a `target` and `token_file`
(`request_timeout_s` is optional for both). The Dispatcher setting is required. Each service checks
its own token audience, so each token file holds a token projected for that service. Token paths refer
to rotating projected ServiceAccount tokens, not static secrets. The Action Service client rereads its
token file for each RPC. Set `AGENTPLANE_NOTIFICATIONS_DATABASE_URL` through a Secret-backed
environment variable rather than putting the DSN in the ConfigMap.

The service persists a notice's stable command ID before calling `SubmitInput`. A queued response means
the Dispatcher owns the retry; it is not a runner-admission receipt. The service reads admission and
confirmation from History Service, and a Dispatcher error never falls back to a direct Runner Gateway send.

The deployment mounts the settings from its ConfigMap. Roll the application image and configuration
together because their structure is versioned with the code.

### Resource quotas

The Notification Service settings accept these deployment-configurable defaults:

```yaml
quotas:
  inboxes_per_account: 64
  active_subscriptions_per_inbox: 64
  entries_per_inbox: 10000
  unacknowledged_messages_per_sender: 128
```

Each value must be positive. Environment overrides use the nested settings delimiter, for example
`AGENTPLANE_NOTIFICATIONS_QUOTAS__ACTIVE_SUBSCRIPTIONS_PER_INBOX=128`. Configure the same
quotas on all replicas. Lowering a quota does not delete retained data; it blocks new allocations
when capacity is exhausted. The inbox quota counts retained inboxes, and the entry quota counts
distinct source-event identities over an inbox's lifetime, including payload-expiry tombstones. The message quota
counts one sender's messages past the inbox's acknowledged cursor.

### GitHub event retention

`github_delivery_retention_days` defaults to 7 and accepts whole days from 1 to 3650, or `null` for infinite retention.
Override it with `AGENTPLANE_NOTIFICATIONS_GITHUB_DELIVERY_RETENTION_DAYS` or deployment YAML.
Use `github_delivery_retention_days: null` in YAML or the literal `null` in the environment variable
to disable cleanup of both raw deliveries and generated mergeability transitions; zero remains invalid. Workers delete up to 1000 expired raw deliveries per cleanup pass, once a minute per worker,
including their IDs, digests and subject links. This bounds source replay, late association
matching and delivery-ID deduplication, including for active subscriptions. Inbox payload copies
keep their independent 30-day retention. The same setting also deletes up to 1000 expired generated
pull-request mergeability transitions and 1000 generated CI transitions and 1000 generated review/lifecycle transitions per pass, including transitions not consumed by an inbox;
current pull-request snapshots remain. Old CI heads without active PR followers are pruned in bounded batches;
current facts for an actively followed head and copied inbox payloads retain their independent lifetimes. Changing this setting does not enable the GitHub provider.

Current check-run and commit-status repair follows `github.freshness_seconds` (default 600 seconds, configurable
from 60 to 3600 seconds via `AGENTPLANE_NOTIFICATIONS_GITHUB__FRESHNESS_SECONDS`). Worker scheduling, GitHub API
or access failures, and rate-limit backoff can extend the interval; GitHub state propagation has no guaranteed
settling time. It observes current facts for followed PR heads, not full event or action history. PR defaults
continue to report completed check-run facts and all commit statuses; an explicitly unfiltered `check_run` filter
reports all observed check-run states. Explicit check-run action filters select actual webhook actions only;
repair does not invent `created`, `in_progress` or `completed` actions. The existing pull-request subscription
command and default filters are unchanged. Branch and fixed-commit CI notifications continue to use raw webhook events. See the
[agent GitHub guide](docs/agents/github.md#follow-github-activity) for identity, payload and filter details.

Migration 00007 copies the landed aggregate recovery tables into native-resource storage, preserving observation
sequences, payloads, subscription boundaries and pending snapshots. It retains the legacy tables and columns;
both legacy and native event logs use GitHub retention. Stop legacy GitHub workers before applying this copy and
starting the new writers, so writes cannot continue in the old tables after the copy. Keep the reader-capable
image available for rollback with its GitHub provider disabled, and preserve the migrated database. Existing
notifications retain their original payload shape; only new facts use the native resource fields.

Migration 00008 adds durable current review refresh state and pull-request review/lifecycle observations. These
records use the same retention setting and do not reconstruct review history or lifecycle transitions missed
between observations.

The migration gives existing deliveries its own timestamp because their actual ingestion times
were not recorded. They first become eligible after the configured retention interval from migration.
Deletes free space for PostgreSQL to reuse; they do not immediately shrink its volume.

### Runner notice debounce

The deployment ConfigMap sets the `notice_debounce.quiet_seconds` and
`notice_debounce.max_wait_seconds` policy. Per inbox, wait for the quiet window after the newest
unannounced entry, subject to the maximum wait from the oldest unannounced entry. Set
`quiet_seconds: 0` to disable batching delays. Both settings accept fractional seconds; the
maximum wait must be positive.

They apply to all sources, not individual subscriptions. Environment overrides use e.g.
`AGENTPLANE_NOTIFICATIONS_NOTICE_DEBOUNCE__QUIET_SECONDS`.

Only preparing new runner notices is delayed: payload persistence and inbox reads remain immediate.
The deadline is derived from durable entry timestamps and scheduled through the existing PostgreSQL
wakeups/deadlines, so restart or replica failover does not restart the window. An already prepared
notice keeps its command ID, content and coverage; retries are not debounced. Acknowledged/expired
entries do not need a new notice, and unacknowledged entries do not cause repeated reminders.
The maximum bounds the batching delay, not source processing, runner outages or delivery retries.

## GitHub App setup

GitHub is disabled when `github` is absent from both YAML and environment. To enable it, put the public
`github.app_id` in YAML and supply these Secret-backed environment variables:

- `AGENTPLANE_NOTIFICATIONS_GITHUB__PRIVATE_KEY`: PEM App private key.
- `AGENTPLANE_NOTIFICATIONS_GITHUB__WEBHOOK_SECRET`: random webhook signing secret, at least 16 characters.

The private key and the webhook secret are validated at startup and never printed. Environment settings turn the
source on even if YAML has no `github`; remove both to disable it. No fixed installation ID or OAuth client secret
is needed.

Staging is enabled and the operator has installed the App; see the
[live acceptance record](docs/staging_github_acceptance.md). Testing remains disabled.
For another environment, register a separate App, supply its credentials through encrypted deployment
configuration and `secretKeyRef`, and expose only `/v1/webhooks/github` through HTTPS ingress with
matching network policy and TLS. Never expose the workload API publicly. Verify real App delivery
through inbox and harness rather than relying solely on configuration or a successful ingress response.

GitHub issue subscriptions require the App installation to grant Issues read permission; generated
installation tokens request it alongside Metadata, Contents and Pull requests read. Enable
`issues` and `workflow_job` webhook events when configuring the App. Workflow-job notifications
are opt-in; ordinary issue subscriptions default to lifecycle events and comments. See the
[ingestion and matching contract](docs/api.md#github-webhook-ingestion-and-matching).

Current PR check-run and commit-status repair use independent installation-token profiles requesting Checks read
and Commit statuses read respectively. These permissions are not added to the ordinary notification-read token
profile. If an installation cannot grant one repair profile, that refresh reports the access error while the other
resource and ordinary webhook processing continue.

The service also refreshes currently exposed non-pending PR review facts on an independent durable deadline and
observes PR lifecycle state through the existing subject refresh. Lifecycle changes do not require a second API read.
These are current-state observations, not a replay of all review or lifecycle events. An `APPROVED` review is one
review fact, not an aggregate approval result or an evaluation of repository review rules. See the
[agent GitHub guide](docs/agents/github.md#follow-github-activity) for payload fields and scope.

### GitHub API transport overrides

The optional `github` settings also own `api_url` (default `https://api.github.com`),
`request_timeout_s` (default 5, positive and finite), and `api_version` (default `2022-11-28`).
Set them in deployment YAML or override them with
`AGENTPLANE_NOTIFICATIONS_GITHUB__API_URL`, `AGENTPLANE_NOTIFICATIONS_GITHUB__REQUEST_TIMEOUT_S`,
and `AGENTPLANE_NOTIFICATIONS_GITHUB__API_VERSION`. Restart the service after changes.
HTTP(S) mock endpoints and REST path prefixes such as `/api/v3` are supported. URLs with
embedded credentials, query strings or fragments are rejected; redirects remain disabled.
The configured endpoint receives App JWTs and installation tokens: only use trusted endpoints,
and use fixture credentials for local mock services. These are deployment-owned settings,
not subscription-controlled destinations, and do not alter egress authorization.

`client.Client` in [`github/client`](github/client/client.go) owns the configured endpoint, App JWTs, the installation-token cache and
typed REST resource methods. The notification provider owns webhook verification, durable access/subject
observations, leases, backoff scheduling and matching. Client calls surface rate limits immediately; they do not
sleep/retry inside a lease. `client.New` validates the settings and the App private key and makes no request.

## GitHub webhook troubleshooting

HTTP 400/401 rejection logs include a parsed delivery UUID (or null for a malformed/missing ID),
a JSON-escaped event header capped at 64 characters, and a stable reason: `invalid_delivery_id`,
`invalid_signature`, `unsupported_event`, `missing_repository`, `payload_validation`, or the
fallback `invalid_delivery`. A payload validation log carries `problem`, which names the fields and array positions of the schema
that failed and quotes nothing of the body. The raw payload, the signature and the secret are never logged. Event headers are untrusted labels,
not evidence of signature verification. Rejections return generic HTTP responses and do not create durable receipts or inbox entries.

GitHub does not automatically retry failed webhook requests: failures/timeouts require redelivery.
Durable recovery starts at the committed receipt, not at the start of the HTTP request.
If GitHub reports a failed delivery, an App operator should inspect the webhook's Recent Deliveries
in GitHub App settings and redeliver that delivery (or use GitHub's App delivery-attempt API
with App authority). Within the configured raw receipt retention window, a retry of an already committed receipt returns 202 with `duplicate: true`;
a retry of an uncommitted receipt returns 202 with `duplicate: false` after commit. Then check
subscription/inbox state separately: HTTP acceptance does not prove runner delivery or agent
acknowledgement. Do not replay with a new delivery ID or assume GitHub retries automatically.
