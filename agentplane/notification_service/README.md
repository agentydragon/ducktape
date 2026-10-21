# Notification Service

Standalone Actions and GitHub App subscriptions and durable session inboxes. The service reads Actions history,
reads destination Sandboxes from their Sandbox resources, validates Sessions through the read-only Runner Gateway,
sends every runner notice through the Session Dispatcher, and reads command receipts from the Session's runner
entries in the History Service. It does not depend on the integration app or connect directly to runners.

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

Agent entry point: [`/llms.txt`](docs/agents/llms.txt), with linked subscription, inbox-handling and
GitHub workflows served under `/docs/agents/`, and `/openapi.json` as the HTTP reference. Update the guides
when those contracts change.

- [API, authorization, persistence and delivery semantics](docs/api.md)
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
service-account tokens, and Kubernetes read access to Sandboxes. Preserve existing staging data. Verify an Action
subscription through a newly opened harness after rollout. Existing sessions retain their original prompt.

A schema change is a Goose file in `migrations/notifications/`. The baseline, `00001_baseline.sql`, takes over a
database that the retired Alembic history left at revision `0010_drop_write_only_columns` and refuses one behind it;
the migrator keeps the advisory lock key the Alembic runner took.

The service reads the settings file and environment variables of [Configuration](#configuration). It takes no
flags, and no `log_levels`; logs are JSON on stderr. `request_timeout_s` (default 15) bounds a call's backend work,
and `watch_lease_s` (default 300) is how long a watch lasts before it ends with `reconnect_required`. GitHub is on
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

`actions` contains `url` and `token_file`; `runner_gateway` uses `control_target` and `token_file`
for per-Sandbox runner live-state checks. `history_service` and `session_dispatcher` each contain a `target` and
`token_file` (`request_timeout_s` is optional for both). The Dispatcher setting is required. Each service checks its own token audience,
so each token file holds a token projected for that service. Token paths refer to rotating projected ServiceAccount tokens, not static secrets. Actions rereads its
token file for each request. Set `AGENTPLANE_NOTIFICATIONS_DATABASE_URL` through a Secret-backed
environment variable rather than putting the DSN in the ConfigMap.

The service persists a notice's stable command ID before calling `SubmitInput`. A queued response means
the Dispatcher owns the retry; it is not a runner-admission receipt. The service reads admission and
confirmation from History Service, and a Dispatcher error never falls back to a direct Runner Gateway send.

The deployment mounts the settings from its ConfigMap. Roll the application image and configuration
together because their structure is versioned with the code.

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

`github.Client` in `github/client.go` owns the configured endpoint, App JWTs, the installation-token cache and
typed REST resource methods. The notification provider owns webhook verification, durable access/subject
observations, leases, backoff scheduling and matching. Client calls surface rate limits immediately; they do not
sleep/retry inside a lease. `github.New` validates the settings and the App private key and makes no request.
