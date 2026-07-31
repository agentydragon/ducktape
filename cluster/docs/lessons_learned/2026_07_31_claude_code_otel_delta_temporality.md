# Claude Code OTel metrics silently dropped (delta temporality)

**Date:** 2026-07-31
**Symptom:** Claude Code native telemetry appeared not to work at all — no
`claude_code_*` metrics in Mimir, from any environment, despite the full
`CLAUDE_CODE_ENABLE_TELEMETRY` env block being set and the OTLP relay running.

## Root cause

Claude Code defaults to **delta** metric temporality
(`OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE`, default `delta`).
Prometheus and Mimir can only ingest **cumulative** temporality, and Alloy's
`otelcol.exporter.prometheus` does not convert — it **drops delta metrics
silently**: no error log, no `otelcol_receiver_refused_*`, no
`prometheus_remote_storage_samples_failed_total`. The metric points are accepted
at the OTLP receiver and disappear before becoming samples.

Traces and logs carry no temporality, so they were unaffected — which is what
made the pipeline look healthy while metrics went missing.

## Evidence

| Check                                            | Result                                                     |
| ------------------------------------------------ | ---------------------------------------------------------- |
| `claude_code_*` series in Mimir, 30d window      | none (also: no `claude` substring among 3814 metric names) |
| `service.name` tag values in Tempo               | `claude-code` — traces arrive                              |
| `otelcol_receiver_accepted_metric_points_total`  | 28,915, `refused`/`failed` = 0                             |
| `prometheus_remote_storage_samples_failed_total` | 92,000 — **all SeaweedFS duplicate-timestamp**, unrelated  |
| Alloy `config.alloy` OTLP pipeline wiring        | correct                                                    |
| Alloy `remote_write` health                      | healthy (WAL checkpoints, no rejections)                   |

The combination "accepted at receiver, absent downstream, zero errors anywhere,
traces fine" is the signature of an unsupported temporality, not of a broken
pipeline.

## Fix

Set on every Claude Code environment:

```text
OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=cumulative
```

- Local/NixOS hosts: <../../../nix/home/claude_code/default.nix>
- Web environments (incl. Haku): the env block in
  <../../../devinfra/claude/README.md> § Web Setup — these are set in the Claude
  Code web UI, so a code change does **not** propagate; each environment must be
  edited by hand.

## Why not fix it server-side

Converting in Alloy would need `otelcol.processor.deltatocumulative` between
`otelcol.processor.batch` and `otelcol.exporter.prometheus`. That component is
**experimental** and requires running Alloy with
`--stability.level=experimental` — which would apply to the entire instance, not
just this pipeline. Not worth that blast radius on shared monitoring for a
problem the documented client-side knob solves.

Revisit if a future OTLP source cannot set its own temporality, or once the
processor reaches general availability.

## Gotchas found along the way

- **`environment_manager` was wrongly suspected.** It does propagate the web
  UI's `environment_variables` into the Claude process — see step 11 of
  <../../../devinfra/claude/web*env/re/environment_manager/src/internal/claude/claude_code_executor.go>,
  which appends `e.Config.EnvironmentVariables` to `cmd.Env` \_after* the
  step-10 `filterInitOnlyFromEnviron` pass.
- **`kubectl port-forward` does not work through `kubeapi.allegedly.works`** —
  the L7 proxy can't do the SPDY/websocket upgrade (`error upgrading connection:
empty server response`). Use the service-proxy subresource
  (`kubectl get --raw /api/v1/namespaces/<ns>/services/<svc>:<port>/proxy/...`),
  which needs `services/proxy` RBAC that the sandbox identities don't have.

## Unrelated issues surfaced

Both were found while diagnosing this and are **not fixed here**:

- `mimir.rules.kubernetes` has been failing to sync for at least 48h:
  `per-user rules per rule group limit (limit: 20 actual: 26) exceeded` on
  `monitoring-node-exporter`. Rules are not being evaluated.
- `loki-gateway` was unreachable during the investigation
  (`dial tcp 10.244.2.170:8080: i/o timeout`), so whether Claude Code's OTel
  **logs** reach Loki is still unverified.
