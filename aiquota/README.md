# aiquota API

`aiquota` can run as a small, bearer-authenticated in-cluster API in addition
to the CLI and GNOME extension. Its Kubernetes manifests and Deployment are
separate from CLIProxyAPI. The API's Codex adapter uses the CLIProxyAPI
integration; it does not mount or access CLIProxyAPI's OAuth PVC.

## Endpoints

```text
GET /healthz
GET /v1/quotas
GET /v1/providers/{provider}/raw
```

`/healthz` is public for Kubernetes probes. Every other endpoint requires:

```http
Authorization: Bearer <AIQUOTA_API_BEARER_TOKEN>
```

`/v1/quotas` returns aiquota's normalized provider data plus
`remaining_percent` for every quota window. `/raw` returns the exact response
body from the provider's usage endpoint, with fetch status and content type.
It never returns request or response headers, OAuth refresh responses, cookies,
or credentials.

## Local clients

The CLI and GNOME extension can use the same API through the managed
`~/.config/aiquota/remote.toml` companion file. Home Manager materializes that
file with the bearer token and configures it for `https://aiquota.allegedly.works`;
the token is not kept in the normal provider config or in the source tree.

Remote results use the normal local `~/.cache/aiquota/quotas.json` cache. A fresh
cache avoids a network request, and an unavailable API falls back to the last
cached snapshot so the CLI and GNOME panel still show the most recent known
quotas while offline.

Responses use `Cache-Control: no-store`. The service keeps the most recent
snapshot only in process memory (default 120 seconds); it does not write quota
or raw-response data to disk.

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
