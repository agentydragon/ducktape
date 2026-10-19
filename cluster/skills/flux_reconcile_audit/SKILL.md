---
name: flux_reconcile_audit
description: >-
  Audit every Flux Kustomization and HelmRelease over a window (default 7d):
  classify broken / slow / miswired / propagating / suspended / healthy with
  per-item culprit, failure counts, and p99 reconcile duration. Use for
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

Classification reads each object's `status.history` entries inside the window
(one entry per revision, carrying status, attempt count, and duration). First
rule that matches wins.

| Bucket          | Rule                                                                                                    |
| --------------- | ------------------------------------------------------------------------------------------------------- |
| **Suspended**   | `spec.suspend=true`. Informational only.                                                                |
| **Broken**      | The latest history entry has a failure status: the controller is stuck at that revision.                |
| **Propagating** | `Ready=False reason=DependencyNotReady` with no failure entries in the window: a dependsOn catching up. |
| **Miswired**    | Latest entry succeeded, but the window holds failure entries: Flux retried until it converged.          |
| **Slow**        | Succeeded, and p99 duration ≥ the per-kind threshold.                                                   |
| **Healthy**     | Everything else.                                                                                        |

Broken keys on the latest entry, not on the failure count: a Kustomization now
`ReconciliationSucceeded` after many failed attempts is Miswired, not Broken.

## Data sources

1. **`status.history`** (Flux 2.7+) — primary signal for every bucket. Flux
   keeps about 10 entries, so a long window on a busy object sees only the
   most recent revisions.
2. **`Ready` condition message** — parsed for Flux's bracketed
   `[Kind/namespace/name status: 'X']` reference: the underlying culprit.
3. **Label-selector probe** — lists each kind in `DEFAULT_PROBE_KINDS`
   (Deployment, CNPG `Cluster`, SeaweedFS `Bucket`, …) cluster-wide once and
   groups by `kustomize.toolkit.fluxcd.io/name`.
4. **Mimir** (`--include-mimir`) — `histogram_quantile(0.99, …)` over
   `gotk_reconcile_duration_seconds_bucket`. Without it, p99 is the longest
   in-window history duration.
5. **Loki** (`--include-loki`, used only when `--window` > 1h) — counts of
   `Reconciliation failed` / `Reconciliation finished` controller log lines,
   for windows longer than the history covers. The larger of the history
   and Loki counts is reported.

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

Per non-healthy resource: current `Ready` status and reason, p99 duration
(source noted), failed/succeeded attempts in the window (with Loki supplements),
the latest entry's status and revision, revisions that failed then recovered,
the culprit, the last error (truncated to 400 chars), the five most recent
history entries, and unhealthy managed objects from the probe (at most 8).

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
