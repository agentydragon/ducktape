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
  },
  "query_fingerprints": { "1/A": "<SHA-256 of source target and effective datasource>" },
  "variable_fingerprints": {}
}
```

Every visible panel target needs a `query_fingerprints` entry keyed by `panel/ref`.
Every query-type templating variable needs a `variable_fingerprints` entry keyed
by its name. The SHA-256 input is canonical JSON: sorted keys, compact separators,
UTF-8, and no ASCII escaping. A panel target hashes
`{"datasource": <target datasource or panel datasource>, "target": <whole target>}`;
a query variable hashes its whole source declaration. Recompute a fingerprint
only after reviewing whether the existing recorded response or variable choices
still represent the changed query. Missing or stale fingerprints fail before
Grafana starts, including for panels that load lazily.
Run the dashboard's `grafana_dashboard_visual_test` Bazel target after an edit:
the failure names the first stale `panel/ref` or variable and prints its expected
digest. Review the source change against the recorded data, update that one
fixture entry if it still applies (or recapture it), then rerun. This deliberately
does not bulk-refresh fingerprints.

An intentionally empty response also needs an `empty_reasons` entry keyed by
`panel/ref` (or `panel/ref/variant` for repeated-panel variants), explaining
the captured absence. If a live capture needed a corrected query, document the
actual query and why under `capture_corrections`, for example
`{"3/A": {"query": "SELECT ...", "reason": "..."}}`. The fingerprint still
binds the fixture to the dashboard's source query; a correction does not waive
drift detection. Older capture notes remain with their responses.

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

Open design and maintenance work is tracked in [grafana/TODO.md](../TODO.md).
