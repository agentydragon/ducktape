# mcp-oauth-facade

Generic Authentik-backed OAuth facade for upstream MCP servers. Fronts an
internal MCP server with `OIDCProxy` + `JWTVerifier` so claude.ai (or any
MCP-OAuth client) can authenticate against Authentik before reaching the
upstream.

## Upstreams

- **HTTP** (`HttpUpstream`): a Streamable HTTP MCP endpoint reachable inside
  the cluster. Optional server-held bearer token forwarded to the upstream
  on every hop.
- **Stdio** (`StdioUpstream`): a subprocess that speaks MCP over stdin/stdout.
  The subprocess inherits the facade pod's environment, so Secret-mounted env
  vars (e.g. `MANIFOLD_API_KEY`) reach the child unchanged.

## Configuration

All env-driven via `pydantic-settings`:

```
MCP_FACADE_AUTH__OIDC_ISSUER=https://auth.allegedly.works/application/o/<slug>/
MCP_FACADE_AUTH__OIDC_CLIENT_ID=<client_id>
MCP_FACADE_AUTH__OIDC_CLIENT_SECRET=<secret>
MCP_FACADE_AUTH__PUBLIC_BASE_URL=https://<host>
MCP_FACADE_FACADE_NAME=<human readable name>
MCP_FACADE_UPSTREAM__KIND=http        # or stdio
MCP_FACADE_UPSTREAM__URL=...          # http only
MCP_FACADE_UPSTREAM__BEARER_TOKEN=... # http only, optional
MCP_FACADE_UPSTREAM__COMMAND=["..."]  # stdio only (JSON list)
```

The image binary is at `//x/mcp_oauth_facade:image` (`ghcr.io/agentydragon/mcp-oauth-facade`).

## Health, readiness, and metrics

Process liveness and upstream availability are deliberately separate, because a
facade can be "up" while serving zero tools when the upstream rejects the
server-held bearer token (the recurring Tana failure: the desktop renderer
starts refusing the PAT while its own `/health` still reports healthy).

- `GET /healthz` (on `port`) — process liveness only. Always `{"ok": true}`
  once the server is up. Use for the k8s liveness/startup probe.
- `GET /readyz` (on `port`) — `200` only when a background probe recently listed
  `> 0` tools from the upstream, else `503`. Use for the k8s **readiness** probe
  so the pod goes NotReady (and the public route stops serving) when the
  upstream is dead, instead of silently advertising an empty tool list.
- `GET /metrics` (on `metrics_port`, default `9090`) — Prometheus exposition.
  A **separate port** so metrics are scraped cluster-internally and never
  exposed through the public HTTPRoute. Metrics:
  - `mcp_facade_upstream_up{facade}` — `1` if the last probe succeeded.
  - `mcp_facade_upstream_tools{facade}` — tool count from the last probe.
  - `mcp_facade_upstream_last_success_timestamp_seconds{facade}`.

A background loop (`upstream_probe.py`) lists upstream tools every
`probe_interval_seconds` (60s) through the same transport the proxy uses.
Readiness is staleness-based (`probe_max_staleness_seconds`, default 195s > 3
intervals) so a single transient probe failure does not flap readiness, while
sustained upstream failure flips the pod NotReady and fires the
`mcp_facade_upstream_*` alerts.
