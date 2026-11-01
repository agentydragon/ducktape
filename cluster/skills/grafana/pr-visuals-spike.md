# Grafana dashboard PR visuals spike

`//cluster/skills/grafana:piraeus_dashboard_visual_test` renders the actual
Piraeus dashboard with Grafana 12.4.1 and a Prometheus TSDB populated with fixed,
synthetic samples. It uses an isolated Docker network, a fixed clock and a pinned
browser; it does not read a cluster Secret or contact production Grafana. The
`visual-review.json` and PNGs are Bazel undeclared outputs, so the existing PR
visual publisher can compare this target with its published devel baseline.

Run it on the remote Docker-capable executor:

```sh
bbr test //cluster/skills/grafana:piraeus_dashboard_visual_test
```

The synthetic fixture covers several nodes, SSD and HDD pools, per-copy metrics,
and PVC usage. This is a rendering and representative-query check, not proof
that the deployed cluster's datasource or metrics work. An initial run has no
before image until the target has a published devel baseline. The publisher
labels a fallback baseline when it cannot find one for the exact base commit.

The next step is a reusable render harness and fixtures for other dashboards and
datasources. A separately invoked live-data preview could substitute a supplied
metrics endpoint for synthetic Prometheus, but it needs an explicit design for
authentication, network access and screenshot publication. Live data must not
be sent automatically to the public PR visuals gallery. The production Grafana
verifier and its credential-consent requirement remain separate.
