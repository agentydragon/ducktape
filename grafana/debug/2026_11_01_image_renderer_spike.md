# Grafana image renderer comparison

The opt-in `renderer_spike_*` targets exercise the upstream image renderer
v5.12.10 with the same provisioned Grafana dashboards and recorded responses as
our existing visual tests. The renderer runs its own Chromium, so the spike uses
a test-only reverse proxy to replay datasource responses. It writes images and
query coverage as undeclared test outputs; it does not publish PR visuals.

```sh
bbr test //cluster/cdk8s/monitoring/dashboards:renderer_spike_piraeus \\
  //cluster/cdk8s/monitoring/dashboards:renderer_spike_node_memory \\
  --nocache_test_results
```

In [the initial run](https://app.buildbuddy.io/invocation/65c5e122-3728-4d40-92e6-6b6e0ab1f503),
the renderer produced 1440 × 5195 and 1440 × 5423 images respectively; both
included populated panels and repeated rows. The corresponding existing
[Playwright captures](https://app.buildbuddy.io/invocation/e8dc1005-a627-4a90-a21b-5b39cd21aff7)
had the same dimensions and looked similar on inspection. These are individual
runs, not a performance benchmark. A second uncached Piraeus run produced the
same PNG bytes. This is encouraging but does not establish determinism across
all dashboards or renderer versions. An injected-query-error experiment timed
out in container setup before rendering, so panel-error detection remains
unverified.

The renderer replaces our full-page scrolling and sizing code. Its separate
browser means Playwright request interception, frozen clock, console and network
checks, panel error checks, and visual-review publication do not automatically
come along. A query-backed fixture could avoid the replay proxy; that tradeoff
and preservation of the existing health and determinism guarantees need an
implementation and evaluation before replacing the capture path. Grafana's
[image-rendering documentation](https://grafana.com/docs/grafana/latest/setup-grafana/image-rendering/)
describes the renderer as a separate service; the former plugin is deprecated.
