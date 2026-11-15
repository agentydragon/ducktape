# Grafana dashboard PR visuals spike

Each authored dashboard has a neighboring `grafana_dashboard_visual_test` Bazel
target. One shared `//cluster/cdk8s:dashboard_json_files` action exports the SDK
models as transient compact JSON for those tests; deployments embed the same models
directly in Kubernetes manifests. The macro in `//grafana/visual_tests:visual_test.bzl` supplies the Docker,
runfiles and visual-publication wiring and uses a shared scenario by default.

Declare a target beside the dashboard and supply its fixture data:

```bzl
load("//grafana/visual_tests:visual_test.bzl", "grafana_dashboard_visual_test")

grafana_dashboard_visual_test(
    name = "dashboard_visual_test",
    dashboard = "//cluster/cdk8s:home-gateway.json",
    fixture_data = ":dashboard_visual_fixture.json",
)
```

The shared scenario starts isolated Grafana and replays dashboard-owned query
responses. It renders the dashboard in kiosk mode, activates lazy panels, waits
for queries to finish, then publishes one full-page screenshot via
structured test-output records. For this appearance-only fixture it converts query
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

Recordings may use `grafana-recorded-shared-v1`: `shared` holds ordinary JSON
subtrees and `fixture` holds the response, with singleton
`{"$recorded_ref": "value_001"}` objects referring to shared entries. References
can occur at any depth (including inside shared entries). These are literal
captured arrays and schema fragments, with no generated samples, interpolation,
or compression. Expansion precedes the same query/variable validation and
returns independent copies at every use. Missing/cyclic references and unknown
versions fail. Ordinary unencoded fixtures remain supported.

For capture/refresh, retain the existing sanitization and capture notes, then
run the codec on the full recording (paths below are examples):

```sh
bb run //grafana/visual_tests:fixture_codec_cli -- compact capture.json dashboard_visual_fixture.json
prettier --write dashboard_visual_fixture.json
bb run //grafana/visual_tests:fixture_codec_cli -- expand dashboard_visual_fixture.json expanded.json
```

The compactor verifies expanded-object equality before writing. Also compare the
expanded parsed JSON with the sanitized capture after formatting, then discard
the extra capture/expanded files and run the dashboard's visual target. Do not
refresh recordings merely to re-encode them. `$recorded_ref` and
`recorded_format` are reserved keys: capture input containing either is rejected
rather than silently reinterpreted. Use expansion to inspect/edit a full
response; editing a shared entry intentionally affects all its references.

This scenario does **not** execute or validate PromQL, LogQL or SQL, datasource
plugins, or production substitutions. The fixtures preserve captured data shapes,
but may not cover every variable choice or current live state. A green test only
means Grafana loaded and produced these recorded-data screenshots. Add a custom
scenario and fixture where query execution coverage is needed.

The additional Piraeus query-backed target demonstrates that extension: it uses the dashboard
queries against a fixed synthetic Prometheus TSDB and captures the full
dashboard. Its target, scenario, and metrics fixture live beside the dashboard builder.
Neither mode contacts production or reads cluster credentials.

Run a target on the remote Docker-capable executor, for example:

```sh
bbr test //cluster/cdk8s/monitoring/dashboards:piraeus_dashboard_visual_test
```

The collector selects outputs from successful terminal test attempts, so a failed or interrupted
run cannot advance the review baseline. An initial run has
no before image until the target has a published devel baseline. The publisher
labels a fallback baseline when it cannot find one for the exact base commit.

An explicitly invoked live-data preview could use a supplied metrics endpoint,
but needs a separate design for authentication, network access and screenshot
publication. Live data must not be sent automatically to the public PR visuals
gallery. The production Grafana verifier and its credential-consent requirement
remain separate.

Open design and maintenance work is tracked in [grafana/TODO.md](../TODO.md).
