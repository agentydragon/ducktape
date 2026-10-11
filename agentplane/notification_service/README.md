# Notification Service

Standalone Actions and GitHub App subscriptions and durable session inboxes. The service reads Actions history,
looks up destination Sandboxes in the Sandbox Service, sends runner commands through the Runner Gateway, and
reads command receipts from the Session's runner entries in the History Service. It does not depend on the integration app or connect
directly to runners.

Agents subscribe with an explicit destination/session and an idempotence key, then retrieve stored
payloads when notified. Reads are non-destructive; acknowledgement explicitly advances the handled
contiguous prefix. Unacknowledged entries do not cause repeated reminders.
For UID-pinned destinations, workers retire an inbox only after two `GetSandbox` lookups
confirm that its managed Sandbox is missing or has a different UID, separated by the
Pydantic-configured `stale_inbox_confirmation_s` grace period. Matching UIDs (including
suspended Sandboxes) resume delivery; owner mismatches and transport failures never
confirm retirement. Retirement cancels subscriptions and stops retries but retains entries
and notices for normal retention and inspection; it never acknowledges them or rebinds
an inbox to a replacement Sandbox.

- [API, authorization, persistence and delivery semantics](docs/api.md)
- [Staging GitHub acceptance record](docs/staging_github_acceptance.md) — live CI/comment delivery and its limits.
- [Plans and deferred work](../plans/notifications.md)
- `/openapi.json` and authenticated `GET /v1/sources`: schemas and available sources.

## Running and deployment

Bazel targets: `:server`, `:migrate`, `:image`, `:migration_image`. The service needs its own PostgreSQL
database, Actions, Sandbox Service, Runner Gateway and History Service endpoints, and projected
service-account tokens.

Run the image-coupled migration before starting workers. Use published server/migration image tags
and preserve existing staging data. `/healthz` reports process liveness; `/readyz` checks workers and
PostgreSQL and the LISTEN connection. Verify an Action subscription through a newly opened harness after rollout. Existing
sessions retain their original prompt.

## Configuration

Set `AGENTPLANE_NOTIFICATIONS_CONFIG_FILE` to a Pydantic-validated YAML file; explicit missing files and
unknown keys fail startup. Environment variables override YAML using `__` for nested fields.
[settings.py](settings.py) defines the configuration fields and their descriptions.
Configuration changes require a service restart.

`actions` contains `url` and `token_file`; `sandbox_service` contains the Sandbox Service `target`, the
Runner Gateway `gateway_target` and `control_target`, and the `token_file` both services accept.
Token paths refer to rotating projected ServiceAccount tokens, not static secrets. Actions rereads its
token file for each request. Set `AGENTPLANE_NOTIFICATIONS_DATABASE_URL` through a Secret-backed
environment variable rather than putting the DSN in the ConfigMap.

The deployment mounts validated settings from its ConfigMap. Roll the application image and configuration
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

The secrets are `SecretStr` fields and are validated before HTTP startup. Nested environment settings
contribute configuration even if YAML has `github: null`; remove both to disable the source. No fixed
installation ID or OAuth client secret is needed.

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

The optional `github` Pydantic settings also own `api_url` (default `https://api.github.com`),
`request_timeout_s` (default 5, positive and finite), and `api_version` (default `2022-11-28`).
Set them in deployment YAML or override them with
`AGENTPLANE_NOTIFICATIONS_GITHUB__API_URL`, `AGENTPLANE_NOTIFICATIONS_GITHUB__REQUEST_TIMEOUT_S`,
and `AGENTPLANE_NOTIFICATIONS_GITHUB__API_VERSION`. Restart the service after changes.
HTTP(S) mock endpoints and REST path prefixes such as `/api/v3` are supported. URLs with
embedded credentials, query strings or fragments are rejected; redirects remain disabled.
The configured endpoint receives App JWTs and installation tokens: only use trusted endpoints,
and use fixture credentials for local mock services. These are deployment-owned settings,
not subscription-controlled destinations, and do not alter egress authorization.

`GitHubClient` in `sources/github/client.py` owns the async HTTP lifetime, configured endpoint,
App JWTs, installation-token cache and typed REST resource methods. The notification provider
owns webhook verification, durable access/subject observations, leases, backoff scheduling and
matching. Client calls surface rate limits immediately; they do not sleep/retry inside a lease.
The client can accept an injected `httpx.AsyncClient` for transport tests; deployment startup
uses `GitHubClient.open(settings.github)` to validate the key and close the configured transport.
