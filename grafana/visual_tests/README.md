# Grafana dashboard PR visuals spike

Each checked-in source dashboard JSON has a neighboring
`grafana_dashboard_visual_test` Bazel target. Generated deployment copies are
covered by their source. The macro in `//grafana/visual_tests:visual_test.bzl` supplies the Docker,
runfiles and visual-publication wiring and uses a shared scenario by default.

Declare a target beside the dashboard and supply its fixture data:

```bzl
load("//grafana/visual_tests:visual_test.bzl", "grafana_dashboard_visual_test")

grafana_dashboard_visual_test(
    name = "dashboard_visual_test",
    dashboard = ":dashboard.json",
    fixture_data = ":dashboard_visual_fixture.json",
)
```

The shared scenario starts isolated Grafana and replays dashboard-owned query
responses. It renders the dashboard in kiosk mode, activates lazy panels, waits
for queries to finish, then publishes one full-page screenshot via
`visual-review.json`. For this appearance-only fixture it converts query
variables to fixed choices, maps Loki and ClickHouse datasource references to
the built-in Prometheus frontend, and intercepts datasource queries. Each fixture
explicitly provides choices for query variables and a Grafana response for every
panel and query. For example:

```json
{
  "variables": { "node": ["node-a", "node-b"] },
  "panels": {
    "1": {
      "A": {
        "frames": [{ "schema": { "fields": [{ "name": "Value", "type": "number" }] }, "data": { "values": [[42]] } }]
      }
    }
  }
}
```

The fixtures were captured from live datasource responses over representative time
ranges, then trimmed. Where recent data was missing, an older window is used and
noted in the fixture. Trimmed time series advertise their retained sample spacing
so Grafana connects consecutive samples while preserving larger gaps. Identifying strings and log contents are replaced with
placeholders; selected storage, memory and CPU values retain their measured
scale and history. Other numeric series are normalized. Capture notes identify
queries that needed a correction to obtain data. An absent query or variable
fixture fails the test; an explicitly empty frame represents a real empty result.
Repeated panels can provide `variant_variable` and `variants` keyed by the
selected fixture value, so each repeated row uses its own recorded data.

This scenario does **not** execute or validate PromQL, LogQL or SQL, datasource
plugins, or production substitutions. The fixtures preserve captured data shapes,
but may not cover every variable choice or current live state. A green test only
means Grafana loaded and produced these recorded-data screenshots. Add a custom
scenario and fixture where query execution coverage is needed.

The additional Piraeus query-backed target demonstrates that extension: it uses the dashboard
queries against a fixed synthetic Prometheus TSDB and captures the full
dashboard. Its target, scenario, and metrics fixture live beside the JSON.
Neither mode contacts production or reads cluster credentials.

TODO: Standardize on one approach after evaluating the cost and complexity of
running Prometheus and maintaining queryable fixtures. Testing
the dashboard queries is appealing, but compare that coverage with the fidelity
and maintenance of recorded responses, including dashboards using other
datasources.

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
