# `optiplex` NVMe thermal dropout

The investigation of the 2026-09-23 and 2026-10-04 NVMe dropouts on `optiplex`:
[`2026_10_04_dropout.md`](2026_10_04_dropout.md) has the evidence, what is unknown and the options;
the scripts here produce its plots and tables. They are not part of the Bazel graph, like the other
one-off scripts under `debug/` directories.

- `fetch_metrics.py`: pulls the series from Mimir into one JSON bundle (standard library only).
  Mimir's `mimir-gateway` is cluster-internal and reachable from `haku-sandbox` pods, so run it in
  a Python image there; keep stderr out of the captured output, because `kubectl logs` interleaves
  the two streams:

  ```bash
  kubectl -n haku-sandbox run mimir-fetch --restart=Never --image=python:3.13-slim \
    --env="SCRIPT=$(cat fetch_metrics.py)" --command -- sh -c \
    'python -c "$SCRIPT" --url http://mimir-gateway.monitoring.svc.cluster.local/prometheus \
       --node optiplex --start 2026-09-14T00:00Z \
       --zoom sep23=2026-09-23T22:20Z,2026-09-23T23:55Z \
       --zoom oct4=2026-10-04T22:20Z,2026-10-04T23:55Z 2>/dev/null'
  kubectl -n haku-sandbox wait --for=jsonpath='{.status.phase}'=Succeeded pod/mimir-fetch --timeout=280s
  kubectl -n haku-sandbox logs mimir-fetch > bundle.json
  kubectl -n haku-sandbox delete pod mimir-fetch
  ```

  The run takes about a minute. `--zoom LABEL=START,END` adds a 30 s resolution window; the
  episode attribution queries run automatically for every episode at or above `--threshold` (85 °C).

- `plot_nvme_period.py BUNDLE.json --out plots/`: `overview.png` and `period_N.png` (temperature,
  writes by workload, write latency, pods on the node; `--days` per page, default 3), `dropout_LABEL.png` per zoom window, and `stats.md`. Needs
  `matplotlib` and `numpy`, both in the repo's dependencies. A dropout is detected as the NVMe
  sensor going silent while the host's CPU sensor keeps reporting.

Mimir's retention bounds how far back this works, and the per-pod writes come from cAdvisor
series that disappear with the pod: attribution of short-lived pods is a lower bound.
