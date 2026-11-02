# Grafana image renderer comparison

The opt-in `renderer_spike_*` targets exercise upstream image renderer v5.12.10
with the existing Piraeus and node-memory fixtures. The renderer runs its own
Chromium, so replaying recorded responses requires a test-only reverse proxy.
The `renderer_spike_query_backed_*` targets instead run the Piraeus dashboard's
queries against synthetic Prometheus data, without the proxy. Images and
coverage are undeclared test outputs, not PR visuals.

```sh
bbr test //cluster/cdk8s/monitoring/dashboards:renderer_spike_piraeus \\
  //cluster/cdk8s/monitoring/dashboards:renderer_spike_node_memory \\
  //cluster/cdk8s/monitoring/dashboards:renderer_spike_query_backed_valid \\
  //cluster/cdk8s/monitoring/dashboards:renderer_spike_piraeus_error \\
  --nocache_test_results
```

In [the recorded-data run](https://app.buildbuddy.io/invocation/65c5e122-3728-4d40-92e6-6b6e0ab1f503),
the renderer produced 1440 × 5195 and 1440 × 5423 images respectively; both
included populated panels and repeated rows. The corresponding existing
[Playwright captures](https://app.buildbuddy.io/invocation/e8dc1005-a627-4a90-a21b-5b39cd21aff7)
had the same dimensions and looked similar on inspection. A repeated Piraeus
capture was byte-identical. The [query-backed capture](https://app.buildbuddy.io/invocation/8bf37ebd-8258-442c-8e8b-6d94260accb9)
was populated and byte-identical to an earlier capture. These two repeated runs
do not establish determinism across all dashboards or renderer versions. The
[single-node filter run](https://app.buildbuddy.io/invocation/c2ba6761-2ba6-4b74-9e5e-7b445f35ee22)
passed `var-node=storage-a` and showed the expected 4 TiB capacity, versus 9
TiB with all nodes. This exercises a dashboard variable; it does not prove
other types of Grafana filters work.

In [the error injection run](https://app.buildbuddy.io/invocation/2c57a54d-2261-4ba5-b999-7880046f9e02),
the recorded fixture proxy returned HTTP 400 for a single panel's datasource
request. The renderer still returned HTTP 200 and a PNG showing Grafana's
error indicator and “No data”; renderer logs did not identify the panel error.
The same behavior occurred with [invalid PromQL against synthetic Prometheus](https://app.buildbuddy.io/invocation/90bdbec2-fafc-4c0f-ab09-5500b4c25ff5),
without the proxy.
The current Playwright capture checks responses and Grafana's panel errors, so
checking only renderer HTTP status would lose that protection.

The renderer replaces our full-page scrolling and sizing code. Its separate
browser means Playwright request interception, frozen clock, console and network
checks, and panel error checks do not automatically come along. The visual-review
publisher has a helper for other image producers, but the repo's dashboard test
policy requires the shared browser capture path; this spike does not publish
renderer images. Query-backed fixtures avoid the replay proxy, but startup of
Grafana, Prometheus, and the renderer has repeatedly exceeded the small test's
60-second limit on RBE. Loading all four images concurrently allowed the
single-panel filter run to pass in 50.6 seconds, but one run does not establish
reliability. One successful full-dashboard run took 48.8 seconds; these are
not controlled performance comparisons. Preservation of health and
determinism guarantees and reliable test startup need evaluation before
replacing the capture path. Grafana's [image-rendering documentation](https://grafana.com/docs/grafana/latest/setup-grafana/image-rendering/)
describes the renderer as a separate service; the former plugin is deprecated.
