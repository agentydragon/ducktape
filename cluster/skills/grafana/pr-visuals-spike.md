# Grafana dashboard PR visuals spike

`//cluster/cdk8s/monitoring/dashboards:piraeus_dashboard_visual_test` renders the actual
Piraeus dashboard with Grafana 12.4.1 and a Prometheus TSDB populated with fixed,
synthetic samples. It uses an isolated Docker network, a fixed clock and a pinned
browser; it does not read a cluster Secret or contact production Grafana. The
`visual-review.json` and PNGs are Bazel undeclared outputs, so the existing PR
visual publisher can compare this target with its published devel baseline.
The manifest is copied into undeclared outputs only after all captures succeed;
failed or interrupted runs cannot publish a partial screenshot set.

Run it on the remote Docker-capable executor:

```sh
bbr test //cluster/cdk8s/monitoring/dashboards:piraeus_dashboard_visual_test
```

`grafana_dashboard_visual_test` in `visual_test.bzl` takes a dashboard file,
a synthetic metrics Python library, and a Python test that owns the scenarios and
screenshots. It supplies the shared isolated service and browser dependencies,
the Docker requirement, the `visual` tag, and the dashboard's runfile path in
`GRAFANA_DASHBOARD_RLOCATION`. The Piraeus target, scenario, and synthetic
fixture live beside the dashboard JSON; the shared service currently supports one
Prometheus datasource.

The synthetic fixture covers several nodes, SSD and HDD pools, per-copy metrics,
and PVC usage. This is a rendering and representative-query check, not proof
that the deployed cluster's datasource or metrics work. An initial run has no
before image until the target has a published devel baseline. The publisher
labels a fallback baseline when it cannot find one for the exact base commit.

The next step is fixtures for other dashboards and support for other
datasources. A separately invoked live-data preview could substitute a supplied
metrics endpoint for synthetic Prometheus, but it needs an explicit design for
authentication, network access and screenshot publication. Live data must not
be sent automatically to the public PR visuals gallery. The production Grafana
verifier and its credential-consent requirement remain separate.
