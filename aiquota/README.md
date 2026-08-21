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

### Initial API deployment (Claude and Codex)

aiquota sends a non-secret sentinel through the dedicated Claude
credential-substitution proxy. The real long-lived setup token remains mounted
only in that proxy and substitution is limited to `api.anthropic.com`
authorization headers. aiquota cannot refresh or write that token.

### CLIProxyAPI integration

The Codex adapter discovers the single available Codex credential through
CLIProxyAPI's authenticated integration endpoint, then asks CLIProxyAPI to
call `https://chatgpt.com/backend-api/wham/usage` with that credential. The
`$TOKEN$` substitution and OAuth refresh remain entirely inside CLIProxyAPI;
aiquota never reads or writes the `ReadWriteOnce` PVC. The integration key is
the SOPS-managed `cli-proxy-api-management` Secret, injected into both
Deployments without putting it in the TOML config.

The CLIProxyAPI management surface is not exposed through the public
`cli-proxy-api.allegedly.works` route; that route only forwards `/v1` model
traffic. The integration key is broad by design, so keep it limited to this
in-cluster path.

The API bearer token is a SOPS-encrypted Kubernetes Secret at
`cluster/k8s/aiquota/aiquota-api-bearer.sops.yaml`.
