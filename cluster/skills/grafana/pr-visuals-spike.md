# Grafana dashboard PR visuals spike

Each checked-in source dashboard JSON has a neighboring
`grafana_dashboard_visual_test` Bazel target. Generated deployment copies are
covered by their source. The macro in `visual_test.bzl` supplies the Docker,
runfiles and visual-publication wiring and uses a shared scenario by default.

The shared scenario starts isolated Grafana and supplies fixed synthetic query
responses. It renders the dashboard in kiosk mode with a viewport tall enough to mount
Grafana’s lazy panels, then publishes one full-page screenshot via
`visual-review.json`. For this appearance-only fixture it converts query
variables to fixed choices, maps Loki and ClickHouse datasource references to
the built-in Prometheus frontend, and intercepts datasource queries. By default it returns synthetic numeric series or bounded table rows with
common label columns; an optional `fixture_data`
JSON supplies panel-specific table columns or series labels, as the AIQuota
dashboard demonstrates. Consequently it does **not**
execute or validate PromQL, LogQL or SQL, the datasource plugins, production
substitutions, or dashboard-specific data shapes; panels without dashboard-specific fixtures may not look like production. A green test only means Grafana loaded
and produced these synthetic screenshots. Keep this limitation visible in
review, and add dashboard-specific scenarios and fixtures where richer coverage
is needed.

The Piraeus target demonstrates that extension: it uses the actual dashboard
queries against a fixed synthetic Prometheus TSDB and captures the full dashboard. Its target, scenario, and metrics fixture live beside
the JSON. Neither mode contacts production or reads cluster credentials.

Run a target on the remote Docker-capable executor, for example:

```sh
bbr test //cluster/cdk8s/monitoring/dashboards:piraeus_dashboard_visual_test
```

The manifest is copied into undeclared outputs only after all captures succeed,
so interrupted runs cannot publish a partial screenshot set. An initial run has
no before image until the target has a published devel baseline. The publisher
labels a fallback baseline when it cannot find one for the exact base commit.

An explicitly invoked live-data preview could use a supplied metrics endpoint,
but needs a separate design for authentication, network access and screenshot
publication. Live data must not be sent automatically to the public PR visuals
gallery. The production Grafana verifier and its credential-consent requirement
remain separate.
