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
`cluster/k8s/monitoring/grafana-instance/dashboards/`. The deliverable is a
dashboard that renders real data in Grafana, not merely valid JSON or a query
that works after an agent manually edits it.

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
bb run //cluster/skills/grafana:verify_dashboard -- \
  --dashboard cluster/k8s/monitoring/grafana-instance/dashboards/<dashboard>.json \
  --screenshot-dir /tmp/grafana-<dashboard>-verify
```

This uses the live Grafana API and a real headless Chromium browser. Under
Bazel, it uses the repository's pinned `@playwright_browsers` binary; direct
invocations may set `GRAFANA_CHROME_PATH` as a fallback. It reads the admin
credentials from the Kubernetes Secret named
`monitoring/grafana-admin-password`; values are never printed. The candidate
mode creates a uniquely named temporary dashboard through Grafana's HTTP API,
renders that dashboard, and deletes it in a `finally` path. It never overwrites
the dashboard being developed.

After the PR is merged and Flux has applied the dashboard, run the same verifier
against the real UID instead of the candidate file:

```bash
bb run //cluster/skills/grafana:verify_dashboard -- \
  --live-uid <dashboard-uid> \
  --screenshot-dir /tmp/grafana-<dashboard>-live
```

Wait for the `GrafanaDashboard` resource to report successful application and
for the Grafana API to return the new dashboard version before this post-merge
run. If candidate rendering was impossible before merge, this live run is
mandatory before reporting success to the user.

The verifier fails on any of these conditions:

- a datasource query returns HTTP 4xx/5xx or a Grafana result error;
- a panel returns no frames or only empty values;
- a query request still contains an unresolved Grafana macro or dashboard
  variable, including `$__rate_interval`, `$__all`, `$node`, or `${topk}`;
- a query result is entirely zero without an explicit
  `--allow-all-zero-panel` exception and an explanation in the handoff;
- the browser sees datasource request failures, page errors, PromQL/LogQL parse
  errors, or Grafana's `No data` state;
- a panel title is absent from the rendered dashboard.

The verifier writes a full-page PNG. The agent must open representative
screenshots with `view_image` and inspect them visually, including at least one
time-series panel, one table/stat panel, and every panel group likely to be
empty. A screenshot file existing or a browser returning HTTP 200 is not visual
verification.

## Authoring and verification contract

1. Inspect the actual Grafana datasource names and UIDs, the dashboard operator
   resource, and the current metrics/log labels before writing expressions.
2. Keep datasource placeholders such as `${DS_MIMIR}` and `${DS_LOKI}` only when
   the deployed dashboard mechanism resolves them. The verifier resolves them
   for a temporary candidate using the live datasource map.
3. Do not rely on Grafana macros unless the browser request proves that the
   installed datasource plugin expands them. Fixed windows such as `[5m]` are
   valid and predictable. In particular, never ship a literal
   `$__rate_interval` into a PromQL or LogQL range selector.
4. Treat `$__all` as a dashboard/UI variable value, not as a value to paste into
   a PromQL label matcher. The browser request must contain the intended regex
   or explicit selected values. Verify the actual request payload, not only the
   dashboard JSON.
5. For joins against Kubernetes metadata, deduplicate the metadata side before
   joining. A syntactically valid query can still fail over a time range with a
   duplicate-series match-group error.
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

## Deployment check

The repository's `GrafanaDashboard` CRD and ConfigMap are applied by Flux. A
successful Bazel/Kustomize build proves wiring, not that Grafana imported the
new dashboard. After merge, check the live resource and dashboard API, then run
the live verifier:

```bash
kubectl -n monitoring get grafanadashboard <dashboard-name> -o yaml
curl -fsS https://grafana.allegedly.works/api/dashboards/uid/<dashboard-uid>
```

Do not use Grafana's `/render/d-solo` endpoint as the sole visual check. This
installation may return a successful HTTP response containing Grafana's
"no image renderer available" placeholder. Use authenticated Chrome instead.

## Known failure modes captured here

- Grafana's query API can return HTTP 200 with an empty frame, so status-only
  checks missed the original broken dashboard.
- The browser request can retain `$__rate_interval` even when an agent's manual
  API probe substituted a range first. Only the browser's actual request body
  proves dashboard interpolation.
- Loki rejects an unresolved range macro with `not a valid duration string`,
  while Prometheus/Mimir may return an apparently successful but empty result.
- Grafana's Explore/drilldown extension parses the panel expression separately;
  an unresolved macro can produce `PromQL query has parsing errors` even when a
  different API probe looked healthy.
- A duplicate `kube_pod_info` or `kube_node_labels` match can break range queries
  while instant queries appear healthy. Validate both query modes where a panel
  uses them.
- The Grafana image-renderer plugin is not guaranteed to be installed. A PNG
  response from `/render` is not evidence that a panel was painted.
- A URL containing `var-node=$__all` may not exercise the same expansion as the
  dashboard's UI `All` selection. Inspect the payload generated by the exact URL
  the user will use.
