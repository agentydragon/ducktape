# Grafana dashboard PR visuals spike

Each checked-in source dashboard JSON has a neighboring
`grafana_dashboard_visual_test` Bazel target. Generated deployment copies are
covered by their source. The macro in `//grafana/visual_tests:visual_test.bzl` supplies the Docker,
runfiles and visual-publication wiring and uses a shared scenario by default.

Declare a target beside the dashboard and optionally supply fixture data:

```bzl
load("//grafana/visual_tests:visual_test.bzl", "grafana_dashboard_visual_test")

grafana_dashboard_visual_test(
    name = "dashboard_visual_test",
    dashboard = ":dashboard.json",
    fixture_data = ":dashboard_visual_fixture.json",
)
```

The shared scenario starts isolated Grafana and supplies fixed synthetic query
responses. It renders the dashboard in kiosk mode, activates lazy panels, waits
for queries to finish, then publishes one full-page screenshot via
`visual-review.json`. For this appearance-only fixture it converts query
variables to fixed choices, maps Loki and ClickHouse datasource references to
the built-in Prometheus frontend, and intercepts datasource queries. By default
it returns generic numeric series or bounded table rows. `fixture_data` can
provide choices for query variables, pairs of label values for queries,
and panel-specific table columns or series names. For example:

```json
{
  "variables": { "node": ["node-a", "node-b"] },
  "labels": { "node": ["node-a", "node-b"] },
  "panels": {
    "1": { "series": ["first", "second"] },
    "2": { "table": [{ "name": "status", "type": "string", "values": ["ready", "pending"] }] }
  }
}
```

Labels are selected by their presence in the query expression or legend format.
Each label supplies exactly two values, one per synthetic row or series. Panel IDs and
their table columns belong to the dashboard's fixture; see AIQuota for an example.

This scenario does **not** execute or validate PromQL, LogQL or SQL, datasource
plugins, production substitutions, or dashboard-specific data shapes. Panels
without tailored fixtures may not look like production. A green test only
means Grafana loaded and produced these synthetic screenshots. Add a custom
scenario and fixture where richer coverage is needed.

The Piraeus target demonstrates that extension: it uses the actual dashboard
queries against a fixed synthetic Prometheus TSDB and captures the full
dashboard. Its target, scenario, and metrics fixture live beside the JSON.
Neither mode contacts production or reads cluster credentials.

Run a target on the remote Docker-capable executor, for example:

```sh
bbr test //cluster/cdk8s/monitoring/dashboards:piraeus_dashboard_visual_test
```

The manifest is copied into undeclared outputs only after all captures succeed,
so interrupted runs cannot publish a partial screenshot set. An initial run has
no before image until the target has a published devel baseline. The publisher
labels a fallback baseline when it cannot find one for the exact base commit.

TODO: Measure Grafana and browser startup costs and consider sharing their setup
or rendering all dashboards in one test. Compare the savings with the effects on
test isolation, parallelism, caching, and per-dashboard visual baselines.

An explicitly invoked live-data preview could use a supplied metrics endpoint,
but needs a separate design for authentication, network access and screenshot
publication. Live data must not be sent automatically to the public PR visuals
gallery. The production Grafana verifier and its credential-consent requirement
remain separate.
