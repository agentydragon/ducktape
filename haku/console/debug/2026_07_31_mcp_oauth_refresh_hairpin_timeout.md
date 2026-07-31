# MCP OAuth refresh timeouts via the public hairpin path

On 2026-07-31 the `home-assistant` MCP server was reported as having "lost its association".
The association was intact: `status: degraded`, holding the terminal `reconnect` state introduced
by <2026_07_20_tana_refresh_rotation_timeout.md>. `kubectl-passthrough-mcp` was in the same state.

## Evidence

Per-association failure records:

| server                    | refresh failed at    | kind              | attempts |
| ------------------------- | -------------------- | ----------------- | -------- |
| `home-assistant`          | 2026-07-28T16:32:00Z | `outcome_unknown` | 1        |
| `kubectl-passthrough-mcp` | 2026-07-30T03:57:52Z | `outcome_unknown` | 1        |

Token-request outcomes across the retained console log window:

```text
outcome_unknown    count=1     avg=30.03s max=30.03s
upstream           count=2     avg=17.62s max=17.80s
success            count=313   avg=2.29s  max=7.09s
```

Authentik's own `runtime` for `/application/o/token/` over the same period: min 1053ms, p50 2830ms,
p90 5445ms, max 5463ms. It has **no** log entry for a request matching the 30s timeout, and none
anywhere near 17s.

## Root cause

`auth.allegedly.works` resolves, from inside a `haku-console` pod, to five **public** OVH node
addresses (147.135.37.175, 147.135.104.5, 147.135.39.162, 147.135.104.16, 147.135.39.176) — not to
the in-cluster `authentik-server` ClusterIP (10.109.71.139). Cilium Gateway API runs in
`gatewayAPI.hostNetwork.enabled` mode, so Envoy binds 443 on the node IPs and there is no
`LoadBalancer` Service to resolve to (`kubectl get svc -A` lists none).

Every console token refresh therefore leaves the pod, traverses the public path, and re-enters
through a node's public IP. The failures are properties of that path, not of Authentik:

- 30.03s elapsed is exactly the client deadline, so the console's event loop was responsive and the
  timeout fired on schedule — a genuine read timeout, with the connection established and no
  response returned.
- `outcome_unknown` (rather than `connect`) means TCP was established and the request was sent.
  Combined with Authentik having no corresponding log line, the request died in the ingress path
  after connect — the signature of hairpin NAT with asymmetric return/conntrack loss.
- The two `upstream` 5xx at ~17.8s are the same phenomenon at lower severity: an Envoy gateway
  timeout, again with no matching Authentik entry.

A single such blip is permanent: `REFRESH_SKEW` is 60s, so the sweep gets one attempt, and a 30s
ambiguous timeout is classified terminal `reconnect` with `refresh_retry_at = None`.

## Why it went unnoticed for three days

`haku_mcp_oauth_token_request_duration_seconds` was never scraped. The only monitor in the namespace
was the CNPG-generated `haku-console-db` PodMonitor; the console app served no `/metrics` endpoint at
all, so the histogram had no exposition path and Mimir returned empty for it over 7d. Fixed
separately by adding the endpoint plus a ServiceMonitor.

## Not the cause

The console `server` container was pegged at exactly its 512Mi limit and OOMKilled repeatedly
(`vv7p6`=12, `xjl5b`=10 restarts over 7d; the previous ReplicaSet `5688c5fb6` peaked at 383–441MB
with 0 restarts). Memory pressure was the initial hypothesis for the stalls, but the 30.03s
measurement rules out event-loop starvation. It remains a real, separate regression worth fixing.

## Open

Making the console reach its authorization server in-cluster rather than through the public hairpin
is the actual fix and is unresolved. It is not a simple DNS override: the OIDC issuer and discovered
token endpoint are `https://auth.allegedly.works/...`, and `authentik-server`'s own 443 does not
serve a certificate valid for that name, so redirecting the name in-cluster needs a TLS-terminating
in-cluster path that does not exist under hostNetwork Envoy. The 2026-07-20 Tana incident was
resolved by taking the same kind of step (dropping the public facade from the console's path).
