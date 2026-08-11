# `GET /api/approvals/pending` takes ~2s (2026-08-11)

## Summary

Nothing is slow about the endpoint. Postgres answers its query in **4.6ms**; the console pods were
just **166ms away from the database**, and one HTTP request costs several database round trips.

`cluster/k8s/haku/console/deployment.yaml` had no `nodeSelector`, so the two replicas landed on
`optiplex` (zone `home-lan`) and `wyrm2` (zone `atlas`). The CNPG cluster is pinned to `hil-ovh`
(`db/postgres-cluster.yaml`, for `local-path-ovh`), and the public ingress is there too — the five
A records for `haku.allegedly.works` are exactly the OVH nodes. Every request therefore crossed the
WAN on the way in, several more times inside the handler, and once on the way out.

Fix: pin the Deployment to `topology.kubernetes.io/zone: hil-ovh`.

## Measurements

RTT from `wyrm2` (running one console replica) to the CNPG primary pod:

```text
ping 10.244.2.28  → rtt min/avg/max = 166.272/166.490/166.620 ms
tcp connect :5432 → 168-173 ms (x5)
ping local pod    → 0.048 ms
```

Latency by endpoint, measured from `wyrm2` straight at the pod's API port (no ingress, no nginx),
so the numbers are the handler's own cost:

| Endpoint                       | Database work            | TTFB       |
| ------------------------------ | ------------------------ | ---------- |
| `/healthz`                     | none                     | **0.7 ms** |
| `/api/approvals/pending` (401) | none (rejected pre-auth) | **0.7 ms** |
| `/metrics`                     | one session, one query   | **1.17 s** |

`/metrics` is the calibration point: `refresh_connection_metrics` opens exactly one session and runs
one query, and it costs 1.17s ≈ **7 round trips**. A `pool_pre_ping` checkout, `BEGIN`, the
statement's parse and execute, and the rollback on close are each their own trip, and every one of
them is 166ms.

An authenticated `GET /api/approvals/pending` opens **two** such sessions —
`_operator_actor` → `operator_session` → `resolve_active_session` revalidates the cookie against the
identity tables, then `pending_approvals` runs the ledger query — which lands at ~2.3s server-side,
matching the report.

The ingress path pays for the split as well. From `wyrm2`, whose own node runs a replica:

```text
https://haku.allegedly.works/api/approvals/pending (401)   total=0.84s
http://10.244.5.169:8080/api/approvals/pending     (401)   total=0.0006s
```

The request goes wyrm2 → OVH gateway → back to the pod on wyrm2 → OVH gateway → wyrm2: connect
0.17s, TLS 0.34s, then ~0.5s for a response the app produced in 0.7ms.

## Not the cause

- **The query.** `EXPLAIN (ANALYZE, BUFFERS)` of the scoped pending-approvals select:
  `Execution Time: 4.656 ms`, all buffers cached.
- **Table size.** 27,201 rows / 78MB, of which 26,894 are `ok` — the auto-approved read traffic.
- **The ledger's status filter**, for now. There is no index on `status`, so the plan is a
  `Seq Scan` removing 27,201 rows to find the handful that are pending. It costs 4.6ms today
  against a warm cache and it grows linearly with the audit ledger; a partial index
  (`WHERE status = 'pending_approval'`) is the cheap answer when it starts to matter, but it was
  never what made this endpoint feel slow.

## Follow-ups this does not do

Co-locating removes the 166ms multiplier but not the round trips it multiplied. Worth doing on
their own merits, in rough order of value:

1. **One session per request.** The auth dependency and the handler each open their own; a
   request-scoped session would halve the trips for every operator API call.
2. **`pool_pre_ping=True`** (`app.py`) buys a liveness check per checkout at the cost of a round
   trip. `pool_recycle` covers the same failure mode without one.
3. **Carry the payload on the WebSocket.** `/api/events/ws` is invalidation-only by design (REST
   stays the source of truth), so every event makes every open tab re-fetch the full list — and the
   frontend also re-syncs every 30s. Sending the pending approvals in the event body would leave
   REST as the load-time read and the reconnect catch-up, which is what the "source of truth"
   property actually needs.
