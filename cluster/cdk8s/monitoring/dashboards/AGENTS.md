# Grafana dashboards — agent instructions

Before creating or changing a dashboard in this directory, read and use
[`cluster/skills/grafana/SKILL.md`](../../../skills/grafana/SKILL.md). A new dashboard is its
`<name>.json` here plus its line in `_FILE_DASHBOARDS` of
[`../grafana_instance.py`](../grafana_instance.py).

The Grafana skill is mandatory. A valid JSON file, a successful Kustomize build,
or a manually substituted PromQL probe is not a working-dashboard check. Run its
candidate verifier before the PR when possible, visually inspect its Chrome
screenshots, and run its live verifier after Flux applies the dashboard when
candidate rendering was unavailable or when deployment wiring changed.

Do not hand the user a dashboard with unresolved macros, empty frames, browser
datasource errors, or unexplained all-zero panels. Record any explicit empty/all-zero
allow-list exception and its reason in the PR.
