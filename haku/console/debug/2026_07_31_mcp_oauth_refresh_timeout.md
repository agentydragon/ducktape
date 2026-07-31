# MCP OAuth refresh timeouts — unresolved

On 2026-07-31 the `home-assistant` MCP server was reported as having "lost its association".
The association was intact: `status: degraded`, holding the terminal `reconnect` state introduced
by <2026_07_20_tana_refresh_rotation_timeout.md>. `kubectl-passthrough-mcp` was in the same state.

**Root cause is not established.** The leading hypothesis was tested and refuted; see below.

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

A single blip is permanent: `REFRESH_SKEW` is 60s, so the sweep gets one attempt, and an ambiguous
timeout is classified terminal `reconnect` with `refresh_retry_at = None`.

## Refuted: the residential egress path

From inside a console pod `auth.allegedly.works` resolves to public OVH node addresses, and one of
the two replicas runs on `optiplex` (region `home`), so its requests exit via residential broadband
— Authentik records `remote: 99.108.137.35` (AT&T). The hypothesis was that this leg drops
connections and wedges the association.

Measured directly: 600 HTTPS requests to Authentik's `ha-mcp` OIDC discovery endpoint, 4-way
parallel, from a pod pinned to `optiplex` and simultaneously from one pinned to `ovh-ns103711`.

```text
optiplex (home)   ok=468 fail=132  avg=5.864 p50=5.715 p95=8.994 p99=11.366 max=17.800
ovh-ns103711      ok=502 fail=98   avg=5.604 p50=5.434 p95=8.380 p99=12.213 max=17.783
```

The distributions are indistinguishable. The residential leg is **not** measurably worse, so node
placement does not explain the failures and pinning the console to OVH is not justified.

## What the experiment did surface

Both regions equally:

- A tail reaching **~17.8s** — matching the ~17.6–17.8s of the console's two production `upstream`
  failures. That tail exists inside the datacenter.
- Under only 8 concurrent cheap discovery GETs, p50 rose to ~5.5s and p99 to ~12s, against the
  ~1–2s the console observes when idle.
- ~19% of requests returned **403 in ~0.2s** — something in the path throttles under trivial load.

Absolute latencies here are inflated by the test's own load and that throttling, so they do not
describe normal conditions; only the home-vs-OVH comparison is load-independent.

The working hypothesis is now that the auth path (Envoy plus Authentik) has little headroom and
degrades sharply under concurrency, and that a refresh occasionally coincides with a degradation
deep enough to exceed 30s. Whether the stall is in Envoy's queue or in Authentik's workers is
unproven, and the 30s deadline sits under 2× the observed tail either way.

## Next

- Instrument where the 30s goes: Envoy access logs for the failing request, or Authentik worker
  saturation metrics at a failure timestamp.
- Identify what returns 403 under load, and whether the same limiter can affect token requests.
- `haku_mcp_oauth_token_request_duration_seconds` was never scraped (no `/metrics` endpoint, no
  ServiceMonitor), which is why none of this was visible for three days. Fixed in the same change
  as this note; that histogram is the instrument for everything above.
