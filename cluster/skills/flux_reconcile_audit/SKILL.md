---
name: flux_reconcile_audit
description: >-
  Audit every Flux Kustomization and HelmRelease over a window (default 7d):
  classify each resource using Kustomization history or HelmRelease conditions,
  with optional Loki counts and Mimir p99 reconcile duration. Use for
  "what's slow / stuck / been broken this week".
---

# Flux Reconcile Audit

Run the `audit` binary from the ducktape repo root, outside the sandbox. It
reads every Kustomization and HelmRelease from the K8s API (current kubeconfig)
and prints one Markdown report.

```bash
# Default — 7-day window, K8s API only, ~2-3 s
bb run //cluster/skills/flux_reconcile_audit:audit --

# "What's broken right now?"
bb run //cluster/skills/flux_reconcile_audit:audit -- --window 30m

# Drill into one resource
bb run //cluster/skills/flux_reconcile_audit:audit -- --window 1h --name forgejo

# Week-long view with Mimir p99 and Loki counts (needs the port-forwards below)
bb run //cluster/skills/flux_reconcile_audit:audit -- --window 7d --include-mimir --include-loki
```

`--slow-kustomization-s` (default 60) and `--slow-helmrelease-s` (default 300)
set the Slow threshold.

## Buckets

For Kustomizations, classification uses per-revision reconcile history within
the window. For HelmReleases, classification uses current conditions and optional
Mimir latency. HelmRelease history records deployments.

| Bucket          | Kustomization                                                                       | HelmRelease                                                        |
| --------------- | ----------------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| **Suspended**   | `spec.suspend=true`.                                                                | `spec.suspend=true`.                                               |
| **Broken**      | Latest reconcile entry failed.                                                      | `Ready=False` for a reason other than `DependencyNotReady`.        |
| **Propagating** | `Ready=False reason=DependencyNotReady` and no failures in the window.              | Waiting on a dependency or reconciling without a reported failure. |
| **Miswired**    | Earlier reconcile entries failed and the latest did not.                            | Requires ordered reconcile evidence.                               |
| **Slow**        | A success in the window and p99 or longest last reconcile time meets the threshold. | `Ready=True` and Mimir p99 meets the threshold.                    |
| **Unknown**     |                                                                                     | No Ready result and no active reconciliation reported.             |
| **Healthy**     | Everything else.                                                                    | `Ready=True`.                                                      |

## Data sources

1. **Kustomization `status.history`** (Flux 2.7+) — about 10 entries per
   object, each with a status, attempt count, and duration for the revision.
2. **Flux conditions** — current HelmRelease readiness and progress. A
   bracketed `[Kind/namespace/name status: 'X']` reference in a Ready condition
   message identifies the underlying culprit.
3. **Label-selector probe** — lists each kind in `DEFAULT_PROBE_KINDS`
   (Deployment, CNPG `Cluster`, SeaweedFS `Bucket`, …) cluster-wide once and
   groups by `kustomize.toolkit.fluxcd.io/name`.
4. **Mimir** (`--include-mimir`) — `histogram_quantile(0.99, …)` over
   `gotk_reconcile_duration_seconds_bucket`. The default report shows the
   longest in-window last reconcile duration where Kustomization history includes it.
5. **Loki** (`--include-loki`, used only when `--window` > 1h) — counts of
   `Reconciliation failed` / `Reconciliation finished` controller log lines,
   for windows longer than the history covers. The report uses Loki counts for
   HelmReleases and the larger of history and Loki counts for Kustomizations.

### Port-forwards for Mimir and Loki

The script reads Mimir at `localhost:8080` and Loki at `localhost:3100`:

```bash
kubectl port-forward -n monitoring svc/mimir-querier 8080:8080 >/tmp/mimir-pf.log 2>&1 &
kubectl port-forward -n loki svc/loki-read 3100:3100 >/tmp/loki-pf.log 2>&1 &
```

Wait until both logs print `Forwarding from` before running. A failed Mimir or
Loki query is swallowed and returns nothing, so a port-forward still booting
silently drops that source from the report.

Mimir needs the Flux controllers scraped: the PodMonitor in
`cluster/cdk8s/flux_monitoring.py` (`kubectl get podmonitor -n flux-system flux-system`).

## What the report contains

Per non-healthy resource, the report includes current `Ready` status and reason,
available durations and attempt counts, the culprit, and the last error. For
Kustomizations it also includes up to five history entries, recovered revisions,
and up to eight unhealthy managed objects.

## Gotchas

- **Culprit extraction only works for some failures.** Most `HealthCheckFailed`
  messages carry the bracketed reference; `BuildFailed` / `ApplyFailed` /
  `PostBuildFailed` do not, so they get only "Last error".
- **Deployment conditions.** `Available=True` with
  `Progressing=False ProgressDeadlineExceeded` is stuck; the probe shows both.
- **`flux-system` often shows as Slow** (reconciling gotk-components takes
  minutes). Expected, not a finding.
- **New stateful CRD?** Append it to `DEFAULT_PROBE_KINDS` in `audit.py` so the
  probe covers it.
