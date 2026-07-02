# claude.ai custom connectors recurringly flip to "Reconnect" — RCA (2026-07-02)

## Symptom

claude.ai Settings → Connectors periodically shows self-hosted (allegedly.works)
MCP connectors as "Reconnect" while third-party connectors (Gmail, GDrive, …)
stay connected. As of 2026-07-02: Cluster kubernetes (sandbox), Grocy Vallejo,
Manifold, PostScanMail dead; Grocy SF, Tana, Plaid Postgres alive. It keeps
recurring; each recovery requires a manual Reconnect click.

## Root cause (proven)

Three-link chain:

1. **authentik-server is a single replica and restarts frequently** (13
   container restarts in 5.5 days as of 2026-07-02; ~35 boots on 2026-06-24
   alone). Diagnosed from two restart specimens (07-01 01:16:49, 07-02
   09:10:49): authentik's `/-/health/live/` endpoint **returns 500 whenever
   PostgreSQL is unreachable**, so any transient DB/DNS blip (CNPG failover,
   node churn, CoreDNS loss — the known etcd-HDD/CP instability) fails the
   liveness probe and kubelet SIGTERMs a healthy server (graceful exit 0,
   `Handling signal: term`, probes 500ing only in the final seconds). The
   reboot takes ~90 s (bootstrap 25 s + app init + 16 s/worker), so a
   seconds-long DB blip becomes a minutes-long `auth.allegedly.works` 503
   window. Mitigated by relaxing the server livenessProbe
   (`failureThreshold: 8`, `periodSeconds: 15` — ride out ~2 min) in
   `cluster/k8s/authentik/app/helmrelease.yaml`.

2. **FastMCP's OAuthProxy translates ANY upstream refresh failure into OAuth
   `invalid_grant`** (fastmcp 3.1.0,
   `fastmcp/server/auth/oauth_proxy/proxy.py` ~1206):

   ```python
   except Exception as e:
       logger.error("Upstream token refresh failed: %s", e)
       raise TokenError("invalid_grant", f"Upstream refresh failed: {e}") from e
   ```

   A transient Authentik 503 or a DNS error becomes `invalid_grant` (HTTP 401)
   at the facade's `/token` endpoint.

3. **claude.ai treats one `invalid_grant` refresh response as terminal** —
   correctly per RFC 6749 (`invalid_grant` means "refresh token
   invalid/expired/revoked"). It flips the connector to "Reconnect" and stops
   ALL traffic permanently. Observed: manifold facade logged zero claude.ai
   requests after its single 401.

claude.ai proactively refreshes each connector's token every ~10–40 min around
the clock, so each Authentik restart is a dice roll across every connector:
~50 restarts/month × ~2%–5% collision probability per connector per restart ≈
several connector deaths per month. Matches observed cadence.

## Evidence

Loki (`kubectl -n loki port-forward svc/loki-read 3100`), facade access logs.
Every death event is a `proxy.py:1207 "Upstream token refresh failed"` ERROR +
`POST /token → 401` with **zero claude.ai traffic afterward**:

| Event (UTC)    | Upstream failure                                            | Connectors killed                       |
| -------------- | ----------------------------------------------------------- | --------------------------------------- |
| 06-11 03:31:36 | (truncated reason)                                          | grocy-vallejo                           |
| 06-20 00:45:29 | Authentik 503 — authentik booted 00:45:11, mid-start        | manifold + postscanmail + grocy-vallejo |
| 06-22 07:20:14 | 5xx                                                         | postscanmail, grocy-vallejo             |
| 06-24 18:43:07 | 5xx — during a ~35-restart authentik flap day               | postscanmail, grocy-vallejo             |
| 06-28 03:09:58 | DNS "Temporary failure in name resolution" (etcd/CP outage) | postscanmail, grocy-sf                  |

Kills across connectors land in the **same second** — claude.ai refreshes them
on a synchronized cadence, so one outage window can kill several at once.

Corroboration:

- Valkey OAuth state (`mcp-oauth-proxy-clients::*`, no TTL) counts one DCR
  registration per Reconnect click: manifold 1 (never reconnected since
  06-20), postscanmail 3, grocy-vallejo 4, grocy-sf 4.
- Valkey persistence is fine (RDB+AOF on `local-path-ovh`, data survived the
  07-01 full-cluster restart) — state loss is NOT the mechanism.
- Server-side refresh tokens for dead connectors are still valid (TTLs show
  they simply stopped being renewed at each connector's death date).
- kubectl-sandbox-mcp (no facade; claude.ai refreshes directly against
  Authentik): **zero** claude.ai token requests in Authentik logs over the
  full 29-day Loki retention → it died >29 days ago (visibility boundary) and
  was never reconnected. Same class of failure, exact date unrecoverable.
- Tana + Plaid facades logged zero refresh failures in 29 d — their refresh
  timing just never collided with an outage window; survival is luck, not
  configuration.

## Why the earlier "reconnect one and the rest fix themselves" theory is wrong

There is no shared session between connectors. Each has an independent DCR
registration + token chain against its own facade. They die in batches only
because refreshes are synchronized and the outage (Authentik restart) is
shared.

## Fixes (implemented 2026-07-02)

1. **`ResilientOIDCProxy`** (`mcp_infra/authentik_auth/auth.py`, wired via
   `build_authentik_auth` → all facades + grocy MCPs): transient upstream
   failures (httpx transport errors, 5xx) are retried (tenacity,
   3 attempts) and, if persistent, answered with **HTTP 503 + Retry-After**
   instead of `invalid_grant`. Genuine upstream OAuth error responses still
   surface as `invalid_grant`. Note: whether claude.ai retries after a 503 at
   `/token` is unverified — but `invalid_grant` is guaranteed-terminal, 503
   at least may be retried.
2. **Alerting**: `mcp_auth_upstream_refresh_failures_total{outcome}` counter,
   scraped via new metrics ports + ServiceMonitors on
   manifold/postscanmail/plaid-db facades and grocy MCP servers (tana already
   had one); central `McpUpstreamTokenRefreshFailed` alert in
   `cluster/k8s/monitoring/rules/mcp-auth-prometheus-rule.yaml`.
3. **Authentik liveness relaxed** (see cause 1 above): `failureThreshold: 8`,
   `periodSeconds: 15`.

## Still open

- **2 server replicas**: clean to enable (chart `server.replicas: 2`,
  anti-affinity already configured, nodes have ample memory headroom — the
  "OVH memory too tight" comment in the helmrelease is stale). Helps for
  node-local events, but note both replicas share the DB, so DB-blip liveness
  kills would have hit both simultaneously — the probe relaxation above is the
  higher-leverage change. Bundled single-replica chart redis is a remaining
  SPOF (existing TODO in the helmrelease to move to operator-managed Valkey).
- **Underlying DB/DNS blips**: the known etcd-HDD/CP-contention instability
  (see `project_etcd_hdd_cp_contention` memory / cluster plan).
- Dead connectors can only be revived by clicking Reconnect in claude.ai —
  nothing cluster-side re-establishes them.
