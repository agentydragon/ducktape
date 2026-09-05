# aiquota API

`aiquota` can run as a small in-cluster API and an Authentik-gated browser
dashboard in addition to the CLI and GNOME extension. Its Kubernetes manifests
and Deployment are separate from CLIProxyAPI. The API's Codex adapter uses the
CLIProxyAPI integration; it does not mount or access CLIProxyAPI's OAuth PVC.

## Endpoints

```text
GET /healthz
GET /readyz
GET /metrics
GET /v1/quotas
GET /v1/providers/{provider}/raw
```

`/healthz`, `/readyz`, and `/metrics` are public for Kubernetes and Alloy.
Quota endpoints accept either a signed browser OAuth session or:

```http
Authorization: Bearer <AIQUOTA_API_BEARER_TOKEN>
```

`/v1/quotas` returns aiquota's normalized provider data plus
`remaining_percent` for every quota window. `/raw` returns the response body
from the provider's usage endpoint, with fetch status and content type. It also
carries the bounded exact response bytes as base64, their full-body SHA-256 and
original byte length. The exact-byte field is truncated only when the response
exceeds the 1 MiB capture cap. It never returns request or response headers,
OAuth refresh responses, cookies, or credentials.

## Browser dashboard and OAuth API

`https://aiquota.allegedly.works/` is a small React dashboard. aiquota owns an
Authentik OAuth authorization-code flow: `/auth/login` redirects to the
`aiquota` OIDC provider, `/auth/callback` verifies the returned token, and a
signed, HTTP-only app session gates both the frontend entry point and the
existing `/v1/*` API. That API also continues to accept its existing bearer
credential for unattended clients. Authentik's application policy and
aiquota's expected `preferred_username` both restrict browser access to
`agentydragon`.

Do not put the bearer token in browser code or an OAuth session.

## Local clients

The CLI and GNOME extension can use the same API through
`~/.config/aiquota/config.toml`. Home Manager materializes that file with the
bearer token and configures it for `https://aiquota.allegedly.works`; the token
is not kept in the source tree. Local clients require this configuration file;
without it they fail instead of enabling the local provider defaults.

Remote results use the normal local `~/.cache/aiquota/quotas.json` cache. A fresh
cache avoids a network request, and an unavailable API falls back to the last
cached snapshot so the CLI and GNOME panel still show the most recent known
quotas while offline.

Responses use `Cache-Control: no-store`. The service keeps the most recent
snapshot in process memory (default 120 seconds).

## Historical collection

The in-cluster Deployment refreshes Claude and Codex every five minutes even
when no API client is active. Each provider snapshot is appended to the shared
analytics ClickHouse cluster:

- `aiquota.raw_http_observations` stores bounded exact upstream bytes,
  integrity metadata, errors, normalized provider JSON, and the quota-window
  values used by the typed projection.
- `aiquota.aiquota_windows` stores typed quota-window and extra-spend fields
  for direct Grafana queries. A ClickHouse materialized view projects these
  rows from the raw insert, avoiding a partial two-table write.

### History endpoints

Codex also serves two endpoints describing the past rather than the present,
both of which the Codex CLI reads for its own `/usage` view:

```text
GET https://chatgpt.com/backend-api/wham/profiles/me
GET https://chatgpt.com/backend-api/wham/rate-limit-reset-credits
```

`profiles/me` returns `stats.daily_usage_buckets` — one account-wide token total
per day for the last twelve months. `rate-limit-reset-credits` returns each
granted reset credit with its type, status and grant time. Both restate their
whole series on every call, so the first successful poll backfills the period
before aiquota existed, and a slower `AIQUOTA_HISTORY_INTERVAL_SECONDS`
(default hourly) collects them without rewriting an unchanged year every five
minutes.

They land in `aiquota.raw_http_observations` under the provider's own `source`,
with `quota_windows` empty and the `token_activity` / `reset_credits` columns
populated; materialized views project those into `aiquota.token_activity_daily`
and `aiquota.reset_credits`. Rows are kept per observation rather than collapsed,
so the repeated readings of the current day show usage accruing within it and a
credit's status change is dated. For the settled total of a past day, take
`argMax(tokens, observed_at)` grouped by `start_date`.

Raw capture is keyed per endpoint (`codex_token_activity`, `codex_reset_credits`)
so a provider reading several endpoints in one cycle keeps every body.

The current Codex usage response also carries
`rate_limit_reset_credits.available_count`. AIQuota displays this authoritative
live count as banked resets in the CLI, GNOME popup, and Haku Console; it never
redeems a credit. When the companion detail endpoint names an expiry for a
currently available credit, those surfaces display it as a _known_ expiry. The
historical credit rows above remain available for analytics, but their detail
list may be capped or omitted and is not used as the live count or assumed to
be a complete expiry list.

Raw response rows retain one year; typed quota observations retain five years.
ClickHouse inserts use `JSONEachRow` over its internal HTTP endpoint with
asynchronous inserts enabled, so small periodic batches are combined before
creating MergeTree parts. Collection failures do not kill the API; `/readyz`
becomes successful after the first persisted snapshot and Prometheus metrics
report subsequent provider or ClickHouse failures.

## Credential ownership

### Managed Claude Code credential file (local default)

With its default `claude.credentials_path` of
`~/.claude/.credentials.json`, aiquota reads Claude Code's OAuth credential
file. If the access token is expired, aiquota uses the file's refresh token and
**writes the rotated credentials back to that same file**. It therefore becomes
an additional writer alongside Claude Code itself. Use this mode only when that
shared-file ownership and its concurrency risk are acceptable; it is not
appropriate where another process is the declared sole refresh owner.

### Initial API deployment

The API has one Claude credential path: CLIProxyAPI's authenticated management
integration. AIQuota does not receive the Claude setup token, mount the
CLIProxyAPI PVC, or connect to the legacy Claude credential-substitution proxy.
The SOPS-managed setup token remains owned by Haku's existing Claude runner
while that separate route is still in use.

### CLIProxyAPI integration

The Claude and Codex adapters discover their single available provider
credential through CLIProxyAPI's authenticated integration endpoint, then ask
CLIProxyAPI to call the provider's usage endpoint with that credential. The
`$TOKEN$` substitution and OAuth refresh remain entirely inside CLIProxyAPI;
aiquota never reads or writes the `ReadWriteOnce` PVC. The integration key is
the SOPS-managed `cli-proxy-api-management` Secret, injected into both
Deployments without putting it in the TOML config.

CLIProxyAPI's current `/api-call` contract requires an opaque runtime
`auth_index` for token substitution, so aiquota briefly discovers the one
available credential for each provider through `/auth-files`. It does not read
file names, file contents, access tokens, or refresh tokens. If CLIProxyAPI
adds provider-based selection to `/api-call`, this discovery can be removed.

CLIProxyAPI can hold multiple credentials for a provider, for example when
cycling between subscriptions. AIQuota deliberately does not choose between
them: it fails unless exactly one matching credential is enabled and available.
The current deployment therefore expects one Claude and one Codex credential;
support for displaying or cycling multiple subscriptions would be a separate
explicit feature.

The CLIProxyAPI management surface is not exposed through the public
`cli-proxy-api.allegedly.works` route; that route only forwards `/v1` model
traffic. The integration key is broad by design, so keep it limited to this
in-cluster path.

The API bearer token is a SOPS-encrypted Kubernetes Secret at
`cluster/k8s/aiquota/aiquota-api-bearer.sops.yaml`.
