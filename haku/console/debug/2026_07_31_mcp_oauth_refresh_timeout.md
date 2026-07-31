# MCP OAuth refresh timeouts — partially resolved

On 2026-07-31 the `home-assistant` MCP server was reported as having "lost its association".
The association was intact: `status: degraded`, holding the terminal `reconnect` state introduced
by <2026_07_20_tana_refresh_rotation_timeout.md>. `kubectl-passthrough-mcp` was in the same state.

The disconnections are fixed and a large latency contributor with them. **The 30s timeouts
themselves are still not explained** — the fixes make the system tolerate them rather than remove
them, so this note stays open.

## Established

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

From Loki, at both failure timestamps the pattern is identical: a refresh succeeds ~60s earlier
(Authentik `runtime` 1186ms / 1023ms, console 1.777s / 1.929s — they reconcile), then the next
attempt burns the full 30s and Authentik logs nothing for it. There were zero Authentik
worker-timeout, traceback, or pool-exhaustion lines in an 8-minute window around the first failure.
Authentik logs `runtime` only for requests it completes, so its silence does not by itself
distinguish "the path dropped it" from "Authentik hung".

`30.03s` is exactly the client deadline, so the console's event loop was responsive and the timeout
fired on schedule. That rules out event-loop starvation — and with it the console's 512Mi OOM loop
(`vv7p6`=12, `xjl5b`=10 restarts over 7d; the previous ReplicaSet peaked at 383–441MB with 0
restarts) as the cause. The OOM loop is a real but separate regression.

## Why one blip was fatal, and why it happened so often

Two independent things had to be true for a transient timeout to disconnect a server.

**It was permanent.** An ambiguous timeout was classified terminal `reconnect` with
`refresh_retry_at = None`, so `_claim` returned `_Blocked` forever. Note this was the _classification_,
not the 60s `REFRESH_SKEW`: had the failure been `RETRYING`, the sweep would have kept retrying
indefinitely, because an already-expired token still satisfies `token_expires_at <= now + 60s`.

**It was frequent.** `access_token_validity` on these providers was `minutes=10` — the goauthentik
Terraform provider's default, not Authentik's own `hours=1` model default, and none of the four
console-linked providers set it. Renewal runs 60s before expiry, so each association refreshed
**~150 times a day**, every one able to wedge it. At the ~0.3% failure rate measured above, that
predicts a wedge every ~2 days; the observed failures were 07-28 and 07-30. The arithmetic ties out
exactly: `kubectl-passthrough-mcp` failed at `03:57:52`, and its newest revoked refresh token was
issued `2026-07-30 03:48:19` — one 9m33s cycle earlier.

Both were fixed: the classification is now `RETRYING`, halting on `invalid_grant` (ducktape#3598),
and validity moved to `hours=24`, about one renewal a day (ducktape#3595).

### Authentik's rotation and reuse behaviour

Retrying is safe here, which is what the terminal classification had assumed it was not:

- **Rotation is unconditional.** `refresh_token_threshold` defaults to `seconds=0` and the `== 0`
  short-circuit revokes the presented token on every refresh. Visible in the database as a large
  pile of revoked rows against a handful of live ones — `grocy-mcp-sf` 1901 vs 13,
  `kubectl-passthrough-mcp` 1565 vs 6, `ha-mcp` 1154 vs 4.
- **Reuse is not escalated.** A revoked token raises `TokenError("invalid_grant")` and logs a
  `SUSPICIOUS_REQUEST` event, without revoking the token family or session — Authentik does not
  implement RFC 9700 §4.14 family invalidation. All 10 `suspicious_request` events in the whole
  event history resolved to nothing beyond a rejection.

So a retry after an ambiguous timeout either succeeds (the server never processed the request) or
returns `invalid_grant` (it did), and the second case costs only a definitive answer in place of a
guessed one.

## Refuted: the console's residential egress path

From inside a console pod `auth.allegedly.works` resolves to public OVH node addresses, and one of
the two replicas runs on `optiplex` (region `home`), so its requests exit via residential broadband.
The hypothesis was that this leg drops connections and wedges the association.

Measured: 600 HTTPS requests to Authentik's `ha-mcp` OIDC discovery endpoint, 4-way parallel, from a
pod pinned to `optiplex` and simultaneously from one pinned to `ovh-ns103711`.

```text
optiplex (home)   ok=468 fail=132  avg=5.864 p50=5.715 p95=8.994 p99=11.366 max=17.800
ovh-ns103711      ok=502 fail=98   avg=5.604 p50=5.434 p95=8.380 p99=12.213 max=17.783
```

Indistinguishable. The client's placement is irrelevant — the bottleneck was entirely server-side,
which is exactly why a client-side comparison came back null.

## Fixed: Authentik ran in a different region from its database

`authentik-server` and `authentik-worker` had no `nodeSelector`, only pod anti-affinity, and had
landed on `optiplex` (home). Their database is pinned to OVH. Measured from `optiplex`:

```text
TCP connect -> authentik-db-ovh-rw:5432    114ms  (15 samples, very low variance)
TCP connect -> the public gateway            4ms
```

So every query Django issued paid 114ms. Colocating both onto the database's own
`topology.kubernetes.io/zone: hil-ovh` selector gave, on the same uncontended probe (one request per
2s for the static OIDC discovery document, `total == ttfb` throughout):

|        | ttfb range     | median |
| ------ | -------------- | ------ |
| before | 1.51 – 2.32s   | ~1.60s |
| after  | 0.645 – 1.124s | ~0.75s |

**Correction to the pre-deploy estimate.** `1.6s / 114ms ≈ 14 round trips` implied that removing the
cross-region hop would leave near-zero. It did not. The measured saving is ~0.85s, so the database
accounted for roughly 7–8 round trips, and **~0.75s of server-side time is something else**.

## Still open

- **The ~0.75s residual** for a static discovery document. Prime suspect is the `500m` CPU limit:
  `authentik-server` was throttled on 97 of 745 CFS periods (13%) while nearly idle, 3681s
  cumulative over a ~22h pod life. Not yet tested.
- **The 30s timeouts.** No mechanism established. Authentik logged nothing for either failure and
  the console's own elapsed time was exactly the deadline. Envoy access logs for a failing request,
  or Authentik worker saturation at a failure timestamp, are the next instruments.
- **Intermittent 403s.** 20–25% of discovery requests return 403 in ~0.1s — before and after the
  colocation, under load and at one request per two seconds alike. Not a rate limiter tripped by
  test load, and not placement-related.
- The tail reaching **~17.8s** seen from both regions, matching the console's two production
  `upstream` failures.

- **The console's 512Mi OOM loop**, unrelated to the timeouts (ruled out above) but unfixed.

`haku_mcp_oauth_token_request_duration_seconds` was never scraped (no `/metrics` endpoint, no
ServiceMonitor), which is why none of this was visible for three days. That is fixed, so
`outcome_unknown` on that histogram is now the tripwire for whether ambiguous timeouts still happen
at all — and, since the retry makes them survivable, the only remaining signal that they do. There
is still no `PrometheusRule` on it, so nothing pages.
