# Notification Service

Standalone Actions subscriptions and durable session inboxes. The service reads Actions history and
calls Sandbox Service for destination validation and runner commands/receipts. It does not depend on
the integration app or connect directly to runners.

Agents subscribe with an explicit destination/session and an idempotence key, then retrieve stored
payloads when notified. Reads are non-destructive; acknowledgement explicitly advances the handled
contiguous prefix. Unacknowledged entries do not cause repeated reminders.

- [API, authorization, persistence and delivery semantics](docs/api.md)
- [Plans and deferred work](../plans/notifications.md)
- `/openapi.json` and authenticated `GET /v1/sources`: schemas and available sources.

## Running and deployment

Bazel targets: `:server`, `:migrate`, `:image`, `:migration_image`. The service needs its own PostgreSQL
database, Actions and Sandbox Service endpoints, and projected service-account tokens.

Run the image-coupled migration before starting workers. Use published server/migration image tags
and preserve existing staging data. `/healthz` reports process liveness; `/readyz` checks workers and
PostgreSQL. Verify an Action subscription through a newly opened harness after rollout. Existing
sessions retain their original prompt.

## Configuration

Set `AGENTPLANE_NOTIFICATIONS_CONFIG_FILE` to a Pydantic-validated YAML file; explicit missing files and
unknown keys fail startup. Environment variables override YAML using `__` for nested fields. See
[settings.example.yaml](settings.example.yaml) and `settings.py` for field descriptions.
Configuration changes require a service restart.

`actions` contains `url` and `token_file`; `sandbox_service` contains `target` and `token_file`.
Token paths refer to rotating projected ServiceAccount tokens, not static secrets. Actions rereads its
token file for each request. Set `AGENTPLANE_NOTIFICATIONS_DATABASE_URL` through a Secret-backed
environment variable rather than putting the DSN in the ConfigMap.

The deployment mounts validated settings from its ConfigMap. Roll the application image and configuration
together because their structure is versioned with the code.
