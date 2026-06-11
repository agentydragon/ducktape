---
name: flux_reconcile_audit
description: Audit every Flux Kustomization + HelmRelease over a window (default 7d) and classify each into Broken / Slow-but-converges / Miswired-but-converges / Propagating / Suspended / Healthy. Per non-healthy item, surface the underlying-resource culprit (parsed from Flux's own condition message), the in-window failure/success counts, the p99 reconcile duration, and a label-selector probe of objects this Kustomization manages. Use when the user asks "what's slow", "what's stuck", "what's been broken this week", or wants targeted attribution of reconciliation lag.
---

# Flux Reconcile Audit

Run `audit.py` from this skill directory. It starts a `kubectl proxy`,
queries K8s API + Mimir + Loki concurrently, and prints a single
Markdown report.

```bash
# Default — 7-day window, all data sources on, ~25 s
python3 skills/flux_reconcile_audit/audit.py

# "What's broken right now?" — 30 m window, no Loki, ~3 s
python3 skills/flux_reconcile_audit/audit.py --window 30m --no-loki

# Drill into one resource
python3 skills/flux_reconcile_audit/audit.py --window 1h --name augur
```

## Buckets

| Bucket                     | Definition                                                                            | Promoter                                                                                                        |
| -------------------------- | ------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| **Broken**                 | Currently failing; last event was a failure.                                          | `last_was_failure` AND (`real_fails > finishes` OR `real_fails ≥ 2`).                                           |
| **Miswired-but-converges** | Eventually `Ready=True` but only after retries.                                       | `real_fails > 0` AND `finishes > 0` AND not Broken.                                                             |
| **Slow-but-converges**     | Reaches Ready=True but takes too long per push.                                       | p99 reconcile duration ≥ 60 s (Kustomization) / 300 s (HelmRelease) AND `finishes > 0` AND not Broken/Miswired. |
| **Propagating**            | Currently Ready=False because a dependsOn is catching up; no real failures in window. | `Ready=False, reason=DependencyNotReady, real_fails=0`.                                                         |
| **Suspended**              | `spec.suspend=true`. Informational only.                                              | —                                                                                                               |
| **Healthy**                | Everything else.                                                                      | —                                                                                                               |

First rule that matches wins. The `last_was_failure` gate on Broken is
load-bearing: a Kustomization currently `Ready=True
ReconciliationSucceeded` with hundreds of historical failures is
Miswired-but-recovered, not Broken.

## Data sources

In rough order of preference (cheapest + most authoritative first):

1. **K8s API via `kubectl proxy`** — the script starts a `kubectl proxy
--port=8001` for its lifetime. All Kustomization / HelmRelease /
   Event / probe-kind list calls hit `http://localhost:8001`. One TCP
   connection, no per-call kubectl subprocess fork, ~30 ms per call vs
   ~300 ms for `kubectl get … -o json`.
2. **K8s Events** (`/api/v1/events?fieldSelector=involvedObject.apiVersion=…`).
   Structured `reason` / `message` / `count` / `lastTimestamp`. One
   request per controller. Default event retention is ~1 h.
3. **Kustomization `status.conditions[Ready].message`** parsed for the
   bracketed `[Kind/namespace/name status: 'X']` reference — primary
   attribution for "which underlying object is the problem".
4. **Label-selector probe** — at startup we fetch every condition-bearing
   probe kind (`Deployment`, `Cluster` from CNPG, `Bucket` from
   SeaweedFS, …) cluster-wide via `/apis/<g>/<v>/<r>` in parallel, then
   in-memory filter by `kustomize.toolkit.fluxcd.io/name`. One HTTP
   call per kind, never per resource.
5. **Mimir** — one batched query `histogram_quantile by (name, kind)`
   for p99 reconcile duration. Skipped with `--no-mimir`.
6. **Loki** — two batched queries per controller (`Reconciliation
finished` count + `Reconciliation failed` count) by
   `Kustomization_name` / `HelmRelease_name`. Used as the supplement
   for windows past event retention. Only consulted when `--window >
1h`. Skipped with `--no-loki`.

## Prereqs

### Mimir scraping the Flux controllers

`cluster/k8s/flux-monitoring/podmonitor.yaml` points Prometheus at every
controller's `http-prom` port. Verify:

```bash
kubectl get podmonitor -n flux-system flux-system
```

Without it, `gotk_reconcile_duration_seconds_*` is empty and the Slow
bucket can never fire.

### Port-forwards

The script reads Mimir at `localhost:8080` and Loki at `localhost:3100`:

```bash
kubectl port-forward -n monitoring svc/mimir-querier 8080:8080 >/tmp/mimir-pf.log 2>&1 &
kubectl port-forward -n loki svc/loki-read 3100:3100 >/tmp/loki-pf.log 2>&1 &
# wait until both print "Forwarding from" — at least ~5 s — before running.
```

The K8s API does **not** need its own port-forward; the script starts
its own `kubectl proxy` internally.

## What the report contains

Per non-healthy resource:

- **Current status** — Ready + reason from the live K8s API.
- **p99 reconcile duration** — Mimir, when enabled and non-zero.
- **Failures / successes in window** — counted from K8s Events; with
  `--window > 1h` both counts are supplemented from Loki and the
  larger value used. Transient errors (etcd timeouts etc.) are split
  off and reported as `(+N transient)`. Loki supplements show as
  `(loki-fail=N, loki-ok=N)`.
- **Underlying culprit** — `kind/name (namespace)` parsed from Flux's
  own message, plus the cluster-reported status of that object. This
  is the killer feature: Flux already says which Deployment / CNPG
  Cluster / etc. is the cause; the script just extracts it.
- **Last error** — verbatim event message text, truncated to 400 chars.
- **Label-selector probe** — only when the managed object's own
  condition is False or its phase isn't Running/Succeeded/Bound. Capped
  at 8 entries.

## Gotchas

- **Event retention defaults to ~1 h.** For "what's been wrong this
  week" you need Loki. For "what's wrong now" events are sufficient
  and faster — pass `--no-loki`.
- **`Ready=Unknown reason=Progressing` is a Broken signal too** when
  the last event was a failure. The rule is on event ordering, not on
  the current Ready field.
- **Flux's culprit regex is `[Kind/namespace/name status: '<state>']`.**
  Most `HealthCheckFailed` messages carry this; `BuildFailed` /
  `ApplyFailed` / `PostBuildFailed` do not. Those buckets get the
  verbatim error message under "Last error" but no structured culprit
  line.
- **Deployment condition gotcha.** A Deployment with `Available=True`
  but `Progressing=False ProgressDeadlineExceeded` is effectively
  stuck. The probe surfaces both conditions so this isn't hidden
  behind the happier Available.
- **The `flux-system` Kustomization usually shows as Slow** with p99
  ≈ 8 min because reconciling all of gotk-components takes a while.
  Expected, not a finding.
- **Wait long enough for the Mimir/Loki port-forwards to come up.**
  ~5 s minimum. If a port-forward is still booting when the audit
  starts, those queries silently return empty and the report quietly
  miscategorizes.
- **Adding a new stateful CRD?** Append it to `DEFAULT_PROBE_KINDS` in
  `audit.py` so the label-selector probe covers it. The list is
  intentionally curated rather than auto-discovered — discovery would
  add several seconds for no real value on a well-known cluster.

## Performance

Roughly:

- `kubectl proxy` startup: ~1 s
- Universe + Events + 14 probe-kind lists, all parallel: ~1-2 s
- Mimir `histogram_quantile(…[7d])`: ~15-25 s (the irreducible floor
  for week-long histogram queries on this scale)
- Loki two batched queries: ~5 s each, run in parallel: ~5 s
- Classification + report: ~0.1 s

Wall: **~3 s** for `--window 30m --no-loki`, **~25 s** for `--window 7d`
with the full pipeline. Down from ~7 min in the per-resource-kubectl-call
predecessor.

## Test protocol (for skill maintainers)

When changing classification rules or queries, validate against the
live cluster in this order:

1. `--window 30m --no-loki --name <known-broken>` — single resource,
   events only. Confirm culprit attribution lands.
2. `--window 30m --no-loki` — full universe, event-only path. Confirm
   bucket counts are sensible.
3. `--window 7d` — full universe with Loki supplements. Confirm
   Miswired bucket is populated (longer window catches
   retry-then-converge patterns).
4. Always wait ≥5 s after starting the Mimir + Loki port-forwards
   before invoking the script.

See <../../cluster/k8s/flux-monitoring/podmonitor.yaml> for the
metric-scraping wiring this depends on.
