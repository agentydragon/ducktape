# Notification Service

Notification Service provides Actions and GitHub App subscriptions, workload messages, and durable
Session inboxes. The Session ID keys each inbox and Dispatcher supplies its exact owning Identity name
and UID. Accepted entries remain available across Sandbox incarnations. An admitted Session can
subscribe, read and acknowledge before its host starts.

- [Agent workflows](docs/agents/llms.txt) are embedded in the CLI as `agentplane notifications guide`.
- [API](docs/api.md) covers caller authentication, inbox ownership, message authorization and delivery outcomes.
- [Architecture](docs/architecture.md) covers source scheduling, storage and runner notice delivery.
- [Staging GitHub acceptance](docs/staging_github_acceptance.md) records live delivery and its limits.

## Running and deployment

`:service_image` contains `notification-service`, which applies
[Goose migrations](../postgres/migrate/README.md) before opening listeners or starting workers.
The startup probe waits for initialization. For migration-only startup, pass `--migrate-only` with `AGENTPLANE_NOTIFICATIONS_DATABASE_URL`.
Serving needs a PostgreSQL database, Actions, History and Dispatcher endpoints with client TLS,
Kubernetes reads for WorkloadCredentials, Identities, Sandboxes, PolicySets and PolicyBindings,
and `patch` on PolicyBinding status. Verify an Action subscription through a newly opened harness
after rollout.

Replicas use the environment's rolling profile: existing replicas keep accepting webhooks and serving requests
while replacements apply migrations and start, so every migration must stay compatible with both old and new
writers (expand–migrate–contract; [migration contract](../postgres/migrate/README.md)). A migration that drops
what the previous image reads needs the `Recreate` hold of
[staging data preservation](../docs/staging_data_preservation.md) until serving replicas run and pass their
checks on the new schema.

The CLI reads the Session from `AGENTPLANE_SESSION_ID` or `--session ID`. For an older central
Session whose prompt names `AGENTPLANE_DESTINATION`, pass `--session ID` or open a new Session.
See [CLI credentials and configuration](../docs/agent_cli.md#configuration-and-credentials).
Retry uncertain creations and sends with the original idempotency keys.

The service logs JSON to stderr. `request_timeout_s` (default 15) bounds backend work and each watch
write. Watches revalidate the credential and send heartbeats every 15 seconds.
`message_body_limit_bytes` defaults to 32768 and has a database ceiling of 262144. Sends wait for
healthy PolicySet and PolicyBinding watches and are refused while either watch is unsynced or failing.

The TLS listener (`rpc.listen_address`) serves native `Notifications` and `grpc.health.v1`.
`rpc.tls` supplies the server certificate and key, and `rpc.auth` configures SPIFFE client trust.
`operator_reader_services` grants exact service SVIDs the
[five diagnostic reads](docs/api.md#session-inbox-diagnostics).
Health probes need no bearer. Service `""` reports readiness (the LISTEN connection and a database
query) and becomes unhealthy on shutdown. `livez` checks process liveness. The HTTP listener (`host`, `http_port`) serves only the signed `POST /v1/webhooks/github`
when the provider is enabled. The App owns the browser facade.

An unexpectedly stopped worker ends the process and logs error types without values. During a
History outage or lag, GitHub receipts and inbox entries can still commit. Notices wait for History
to catch up before retries. See the [delivery contract](docs/architecture.md#notice-delivery-and-history).

### Rollback

[Migration 22](migrations/notifications/00022_session_inbox_key_contract.sql) drops the Sandbox key, destination and owner columns that
images predating it read; it deletes no subscription, entry, acknowledgement or notice. An image rolled back alone
would query columns that no longer exist: restore a database backup taken before migration 22 beside the paired
prior API and CLI images (their send and subscribe requests differ by the destination field).

For a partial pre-v13 schema, first finish migrating with the predecessor image. Rollback requires a
native-schema-capable service image. Restore a backup only before admitting
new writes, never over newly accepted work. Keep the Deployment stopped during restore, then verify
readiness, authorization, inbox reads, new writes, expiry and replay deduplication. Staging
verification also includes a fresh Action subscription and a real GitHub webhook delivery;
database checks alone do not prove ingress or runner notice delivery.

## Configuration

Set `AGENTPLANE_NOTIFICATIONS_CONFIG_FILE` to a YAML file; a missing file and unknown keys fail startup.
The file is the sole source for static settings; other environment variables cannot override its values.
[settings.py](settings.py) defines the typed fields and defaults rendered by the manifests, while [`cmd/config.go`](cmd/config.go) and
the settings owned by its packages decode and validate them. The database URL, GitHub private key, and webhook
secret are read explicitly from their Secret-backed environment inputs and are not fields in the static file.
Configuration changes require a service restart.

`actions` retains its `url`, plus optional `request_timeout_s` (default 5).
The URL must be an HTTP(S) origin; Go derives the gRPC host and port from it, using h2c for `http`
and TLS for `https`. A peer `tls` block verifies its configured server SPIFFE URI and CA.
With `cert_file`, `key_file` and `client_uri`, it presents Notifications' exact service SVID and
requires `token_file` to be absent. This block applies independently of the legacy Actions URL scheme. The
separate `session_dispatcher` connection pins
`spiffe://agentplane/session-dispatcher`.
`history_service` and `session_dispatcher` each contain a `target`
(`request_timeout_s` is optional for both). The Dispatcher setting is required. Staging selects
service SVIDs for all three peers, with no bearer fallback: no peer sets `token_file` and the
Deployment projects no ServiceAccount token. A bearer-configured peer instead sets `token_file` to its rotating,
audience-specific projected ServiceAccount token and sends no client certificate. The Action
Service client rereads that token file for each RPC. Set `AGENTPLANE_NOTIFICATIONS_DATABASE_URL` through a Secret-backed
environment variable rather than putting the DSN in the ConfigMap.

Roll the application image and its settings ConfigMap together. The settings schema is versioned
with the code.

### Resource quotas

The Notification Service settings accept these deployment-configurable defaults:

```yaml
quotas:
  inboxes_per_account: 64
  active_subscriptions_per_inbox: 64
  entries_per_inbox: 10000
  unacknowledged_messages_per_sender: 128
```

Each value must be positive and is set in the deployment YAML. Configure the same quotas on all replicas.
Lowering a quota does not delete retained data; it blocks new allocations
when capacity is exhausted. The inbox quota (`inboxes_per_account`) counts retained inboxes per execution
Identity, across Sandbox incarnations, and the entry quota counts distinct source-event identities over an
inbox's lifetime, including payload-expiry tombstones. The message quota counts one sender's messages past the
inbox's acknowledged cursor.

### GitHub event retention

Set both windows under `github`, alongside `app_id` if using the webhook source. Retention
settings also apply when no GitHub App is configured. For example:

```yaml
github:
  fact_retention_hours: 168
  raw_body_retention_hours: 6
```

`fact_retention_hours` defaults to 168 and accepts whole hours from 1 to 87600, or
explicit YAML `null` for infinite fact retention. It bounds replay, late association matching and
delivery-ID deduplication, including for active subscriptions.

`raw_body_retention_hours` defaults to 72 and accepts 0 to 87600 hours, or explicit
YAML `null` for infinite body retention. It cannot exceed fact retention, and null is valid only
when fact retention is also infinite. Staging uses the three-day default. Zero stores no new raw
body and begins expiring existing bodies on the next maintenance pass. Expiring a body preserves
its receipt position, digest, identity, compact fact and subject links. Each pass expires up to 1000
bodies after backfilling any legacy fact needed by matching. Receipt cleanup at the configured fact
retention later deletes the whole receipt and its links; `fact_retention_hours: null`
disables that cleanup while the body setting still applies independently. Inbox payload copies keep
their independent 30-day retention.

The fact setting also deletes up to 1000 expired generated pull-request mergeability transitions,
1000 generated CI transitions and 1000 generated review/lifecycle transitions per pass, including
transitions not consumed by an inbox. Current pull-request snapshots remain. Old CI heads without
active PR followers are pruned in bounded batches; current facts for an actively followed head and
copied inbox payloads retain their independent lifetimes. These settings do not enable the GitHub
provider.

For a rollback across the [hourly settings change](https://github.com/agentydragon/ducktape/pull/20054),
omit both retention keys to use defaults with either binary. Older binaries reject hour keys and
newer binaries reject day keys. Convert custom windows when switching binaries (24 hours per day).

Before shortening or disabling raw body retention, verify every replica can match compact facts.
New readers backfill legacy receipts from retained bodies before matching or expiry. Once a body
expires, images that match from bodies lose access to that receipt's content. The compact-facts
release can still match it. PostgreSQL makes deleted space available for page reuse.

### GitHub source work and inbox delivery

Receipts and subscription creation or renewal trigger GitHub work. Successful subscriptions go
idle. `github.freshness_seconds` (default 600, range 60–3600) bounds observations and access checks
reused by that work. Expiry alone schedules no repair. See
[webhook troubleshooting](#github-webhook-troubleshooting) for redelivery and the
[matching contract](docs/api.md#github-webhook-ingestion-and-matching) for catch-up and retries.

### Runner notice debounce

The deployment ConfigMap sets the `notice_debounce.quiet_seconds` and
`notice_debounce.max_wait_seconds` policy. Per inbox, wait for the quiet window after the newest
unannounced entry, subject to the maximum wait from the oldest unannounced entry. Set
`quiet_seconds: 0` to disable batching delays. Both settings accept fractional seconds; the
maximum wait must be positive.

They apply to all sources, not individual subscriptions, and are set in deployment YAML.

Only preparing new runner notices is delayed: payload persistence and inbox reads remain immediate.
The deadline is derived from durable entry timestamps and scheduled through the existing PostgreSQL
wakeups/deadlines, so restart or replica failover does not restart the window. An already prepared
notice keeps its command ID, content and coverage; retries are not debounced. Acknowledged/expired
entries do not need a new notice, and unacknowledged entries do not cause repeated reminders.
The maximum bounds the batching delay, not source processing, runner outages or delivery retries.

## GitHub App setup

To enable GitHub ingestion, set `github.app_id` and any non-default provider settings in YAML
alongside both Secret-backed environment variables below. Setting a provider option without the
App ID and credentials fails validation. Retention settings alone leave ingestion disabled.

- `AGENTPLANE_NOTIFICATIONS_GITHUB__PRIVATE_KEY`: PEM App private key.
- `AGENTPLANE_NOTIFICATIONS_GITHUB__WEBHOOK_SECRET`: random webhook signing secret, at least 16 characters.

The private key and webhook secret are read explicitly, validated with the static settings at startup, and never
printed. Remove the App ID, provider overrides and Secret references together to disable the
source. Keep any retention settings for existing receipts. Installation IDs come from GitHub.

Staging is enabled and the operator has installed the App; see the
[live acceptance record](docs/staging_github_acceptance.md). Testing remains disabled.
For another environment, register a separate App, supply its credentials through encrypted deployment
configuration and `secretKeyRef`, and expose only `/v1/webhooks/github` through HTTPS ingress with
matching network policy and TLS. The only public path is the signed webhook; never expose the workload gRPC listener publicly. Verify real App delivery
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

See [current source-processing observations](docs/api.md#current-source-processing-observations)
and the [agent GitHub guide](docs/agents/github.md#follow-github-activity) for review and lifecycle events.

### GitHub API transport overrides

The `github` settings also own `api_url` (default `https://api.github.com`),
`request_timeout_s` (default 5, positive and finite), and `api_version` (default `2022-11-28`).
Set them in deployment YAML. Restart the service after changes.
HTTP(S) mock endpoints and REST path prefixes such as `/api/v3` are supported. URLs with
embedded credentials, query strings or fragments are rejected; redirects remain disabled.
The configured endpoint receives App JWTs and installation tokens: only use trusted endpoints,
and use fixture credentials for local mock services. These are deployment-owned settings,
not subscription-controlled destinations, and do not alter egress authorization.

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
with App authority). While its fact receipt is retained, a retry of an already committed delivery
returns 202 with `duplicate: true`, even after its raw body expires;
a retry of an uncommitted receipt returns 202 with `duplicate: false` after commit. Then check
subscription/inbox state separately: HTTP acceptance does not prove runner delivery or agent
acknowledgement. Do not replay with a new delivery ID or assume GitHub retries automatically.
