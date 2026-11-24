---
name: grafana
description: >-
  Create and verify Grafana dashboards end to end. Use for any Grafana dashboard
  authoring or change in the cluster: validate every datasource query, render a
  temporary candidate before merge when possible, inspect it visually in a real
  browser, and verify the deployed dashboard after Flux applies it.
---

# Grafana dashboards

Use this skill for every new or changed dashboard under
`cluster/cdk8s/monitoring/dashboards/`. Verify the rendered dashboard and its actual datasource requests.

## Dashboard source and artifact

Locally authored dashboards use Grafana Foundation SDK builders and models. Monitoring
builders live in `cluster/cdk8s/monitoring/dashboards/`; component dashboards live beside
that component's generator. Declare each with `grafana_dashboard` from
`//grafana:dashboard.bzl`. It builds compact JSON independently for each dashboard;
the owning generator declares the output in `data`, reads it from runfiles, and embeds
its text in `GrafanaDashboard.spec.json`, with datasource inputs mapped to provisioned UIDs in
`spec.datasources`. Dashboards imported from grafana.com keep their upstream references.

Synthesis, browser tests and candidate verification consume the same generated JSON.
Keep generated JSON out of Git. Build a specific dashboard using its JSON target.

```sh
bbr build //cluster/cdk8s/monitoring/dashboards:cpap-gateway-disk.json
```

For a permitted live candidate check, pass the built dashboard to `--dashboard` and
its generated resource file to `--grafana-dashboard-manifest`. The verifier reads
literal datasource mappings from that resource; it does not import generator code.

## Credential consent

Before running the verifier or any live Grafana validation that reads the
`monitoring/grafana-admin-password` Secret, tell the user that the check will
read and use the Grafana admin credential, and ask for explicit permission.
Creating or verifying a dashboard does not itself imply permission to retrieve
or use that credential. Do not run `kubectl get secret`, log in to Grafana, or
start the verifier until the user grants permission.

If the user declines, do not substitute another privileged credential. Perform
static checks and non-credential validation only, and report that live query and
render verification remains outstanding. A future read-only verifier mode may
use a Viewer service-account token, but it must not be assumed to exist until
the verifier supports it and the user provides permission for that credential.

## Required verifier

Run the packaged verifier from the repository root:

```bash
bb run //grafana/skill:verify_dashboard -- \
  --dashboard bazel-bin/cluster/cdk8s/monitoring/dashboards/<dashboard>.json \
  --grafana-dashboard-manifest cluster/generated/monitoring/grafana-instance/grafana-instance.k8s.yaml \
  --screenshot-dir /tmp/agents/YYYY_MM_DD/grafana-<dashboard>-verify
```

This uses the live Grafana API and a real headless Chromium browser. Under
Bazel, it uses the repository's pinned `@chrome_headless_shell` binary; direct
invocations may set `GRAFANA_CHROME_PATH` as a fallback. It reads the admin
credentials from the Kubernetes Secret named
`monitoring/grafana-admin-password`; values are never printed. The candidate
mode creates a uniquely named temporary dashboard through Grafana's HTTP API,
renders that dashboard, and deletes it in a `finally` path. It never overwrites
the dashboard being developed.

The verifier fails on any of these conditions:

- a datasource query returns HTTP 4xx/5xx or a Grafana result error;
- a panel returns no frames or only empty values;
- a query request still contains an unresolved dashboard variable or a
  frontend-expanded macro, including `$__rate_interval`, `$__all`, `$node`, or
  `${topk}`. ClickHouse backend macros such as `$__timeFilter` are allowed in
  requests, but the returned executed SQL must show that they were expanded;
- a query result is entirely zero without an explicit
  `--allow-all-zero-panel` exception and an explanation in the handoff;
- the browser sees datasource request failures, page errors, PromQL/LogQL parse
  errors, or Grafana's `No data` state;
- a panel title is absent from the rendered dashboard. Known dashboard-variable
  placeholders in titles must expand to visible text; structural row titles are
  not panel titles and are skipped.

The browser verifier scrolls to the end of the dashboard so repeated rows and
their panels are rendered even when the dashboard expands beyond a fixed number
of viewports. It waits for datasource query requests to finish after each scroll
before capturing that viewport, and fails if a request remains pending for 45s.

The verifier writes a full-page PNG. The agent must open representative
screenshots with `view_image` and inspect them visually, including at least one
time-series panel, one table/stat panel, and every panel group likely to be
empty. A screenshot file existing or a browser returning HTTP 200 is not visual
verification.

## Authoring and verification contract

1. Inspect the actual Grafana datasource names and UIDs, the dashboard operator
   resource, and the current metrics/log labels before writing expressions.
2. Datasource names and types are not UIDs: a datasource named `Loki` with type
   `loki` can have a generated UID, so do not guess `"uid": "loki"`. Use a
   verified UID explicitly provisioned by the deployment, or an operator input.
   For operator-managed dashboards, use a symbolic SDK datasource reference such as
   `DataSourceRef(type_val="loki", uid="${DS_LOKI}")` and map it in the resource's
   `datasources` to the provisioned Loki UID. The Operator substitutes literal values;
   it does not resolve a datasource name to a UID. Keep both panel and target references
   consistent; a target overrides its panel. Candidate verification applies the same
   substitution from the generated resource before checking the name or UID against
   live Grafana datasources. It does not repair mismatched references.
   For a dashboard owned by another generator, pass
   `--grafana-dashboard-manifest <generated-resource-file>`.
3. Do not rely on Grafana macros unless the browser request or, for ClickHouse
   backend macros, the returned executed SQL proves that the installed datasource
   plugin expands them. The verifier rejects ClickHouse dashboard variables until
   it supports the plugin's interpolation and quoting rules. Fixed windows such as `[5m]` are
   valid and predictable. An unresolved `$__rate_interval` can make Loki report
   `not a valid duration string`, make Prometheus/Mimir return an empty result,
   or make Explore/drilldown show `PromQL query has parsing errors` even when a
   manual API probe succeeds.
4. Treat `$__all` as a dashboard/UI variable value, not as a value to paste into
   a PromQL label matcher. The browser request must contain the intended regex
   or explicit selected values. Verify the actual request payload, not only the
   dashboard JSON. Exercise the URL the user will use. A URL with `var-node=$__all`
   can expand differently from the UI's All selection.
5. Deduplicate the metadata side of Kubernetes joins. Duplicate `kube_pod_info`
   or `kube_node_labels` matches can break range queries while instant queries
   still work. Validate both modes when a panel uses both.
6. Test every target with the actual Grafana `/api/ds/query` endpoint over a
   representative recent range. HTTP 200 alone is insufficient: inspect result
   errors, frame contents, and numeric values.
7. Keep physical device I/O, cAdvisor/container I/O, SeaweedFS logical traffic,
   Loki log bytes, and Mimir ingestion separate. They overlap and must not be
   presented as additive totals without an explicit accounting model.
8. Keep the default range practical. Large Loki range scans can be slow enough
   that a dashboard looks empty while requests are still running; start with a
   bounded range such as one hour and verify the chosen interval against the
   panel's resolution.
9. If a panel legitimately has no samples or is legitimately all zero, do not
   silently weaken the verifier. Add an explicit allow-list argument for that
   panel and record why it is expected in the PR/handoff.

## Post-merge verification

Wait for Flux to apply the generated `GrafanaDashboard`. Check its
`DashboardSynchronized` condition and confirm the Grafana API returns the new
dashboard version. Verify datasource wiring by querying and rendering the deployed
dashboard.

```bash
kubectl -n monitoring get grafanadashboard <dashboard-name> -o yaml
curl -fsS https://grafana.allegedly.works/api/dashboards/uid/<dashboard-uid>
```

Run the verifier against the live UID after Flux applies. This step is required
before reporting success when candidate rendering was unavailable or deployment
wiring changed.

```bash
bb run //grafana/skill:verify_dashboard -- \
  --live-uid <dashboard-uid> \
  --screenshot-dir /tmp/agents/YYYY_MM_DD/grafana-<dashboard>-live
```

Inspect the PNGs as described above. Grafana's `/render/d-solo` endpoint
may return a successful HTTP response containing a “no image renderer available”
placeholder.
