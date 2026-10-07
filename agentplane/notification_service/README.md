# Notification Service

Standalone Actions and GitHub App subscriptions and durable session inboxes. The service reads Actions history and calls
Sandbox Service for destination validation and runner commands/receipts. It does not depend on the integration app or
connect directly to runners.

Agents subscribe with an explicit destination/session and an idempotence key, then retrieve stored payloads when
notified. Reads are non-destructive; acknowledgement explicitly advances the handled contiguous prefix. Unacknowledged
entries do not cause repeated reminders.

- [API, authorization, persistence and delivery semantics](docs/api.md)
- [Staging GitHub acceptance record](docs/staging_github_acceptance.md) — live CI/comment delivery and its limits.
- [Plans and deferred work](../plans/notifications.md)
- `/openapi.json` and authenticated `GET /v1/sources`: schemas and available sources.

## Running and deployment

Bazel targets: `:server`, `:migrate`, `:image`, `:migration_image`. The service needs its own PostgreSQL database,
Actions and Sandbox Service endpoints, and projected service-account tokens.

Run the image-coupled migration before starting workers. Use published server/migration image tags and preserve existing
staging data. `/healthz` reports process liveness; `/readyz` checks workers and PostgreSQL and the LISTEN connection.
Verify an Action subscription through a newly opened harness after rollout. Existing sessions retain their original
prompt.

## Configuration

Set `AGENTPLANE_NOTIFICATIONS_CONFIG_FILE` to a Pydantic-validated YAML file; explicit missing files and unknown keys
fail startup. Environment variables override YAML using `__` for nested fields. [settings.py](settings.py) defines the
configuration fields and their descriptions. Configuration changes require a service restart.

`actions` contains `url` and `token_file`; `sandbox_service` contains `target` and `token_file`. Token paths refer to
rotating projected ServiceAccount tokens, not static secrets. Actions rereads its token file for each request. Set
`AGENTPLANE_NOTIFICATIONS_DATABASE_URL` through a Secret-backed environment variable rather than putting the DSN in the
ConfigMap.

The deployment mounts validated settings from its ConfigMap. Roll the application image and configuration together
because their structure is versioned with the code.

### Runner notice debounce

The deployment ConfigMap sets the `notice_debounce.quiet_seconds` and `notice_debounce.max_wait_seconds` policy. Per
inbox, wait for the quiet window after the newest unannounced entry, subject to the maximum wait from the oldest
unannounced entry. Set `quiet_seconds: 0` to disable batching delays. Both settings accept fractional seconds; the
maximum wait must be positive.

They apply to all sources, not individual subscriptions. Environment overrides use e.g.
`AGENTPLANE_NOTIFICATIONS_NOTICE_DEBOUNCE__QUIET_SECONDS`.

Only preparing new runner notices is delayed: payload persistence and inbox reads remain immediate. The deadline is
derived from durable entry timestamps and scheduled through the existing PostgreSQL wakeups/deadlines, so restart or
replica failover does not restart the window. An already prepared notice keeps its command ID, content and coverage;
retries are not debounced. Acknowledged/expired entries do not need a new notice, and unacknowledged entries do not
cause repeated reminders. The maximum bounds the batching delay, not source processing, runner outages or delivery
retries.

## GitHub App setup

GitHub is disabled when `github` is absent from both YAML and environment. To enable it, put the public `github.app_id`
in YAML and supply these Secret-backed environment variables:

- `AGENTPLANE_NOTIFICATIONS_GITHUB__PRIVATE_KEY`: PEM App private key.
- `AGENTPLANE_NOTIFICATIONS_GITHUB__WEBHOOK_SECRET`: random webhook signing secret, at least 16 characters.

The secrets are `SecretStr` fields and are validated before HTTP startup. Nested environment settings contribute
configuration even if YAML has `github: null`; remove both to disable the source. No fixed installation ID or OAuth
client secret is needed.

Staging is enabled and the operator has installed the App; see the
[live acceptance record](docs/staging_github_acceptance.md). Testing remains disabled. For another environment, register
a separate App, supply its credentials through encrypted deployment configuration and `secretKeyRef`, and expose only
`/v1/webhooks/github` through HTTPS ingress with matching network policy and TLS. Never expose the workload API
publicly. Verify real App delivery through inbox and harness rather than relying solely on configuration or a successful
ingress response.
