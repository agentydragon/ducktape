# Grafana dashboards

- Before creating or changing a dashboard, read and follow
  [`grafana/skill/SKILL.md`](../../../../grafana/skill/SKILL.md) — mandatory. Valid
  JSON, a passing Kustomize build, or a hand-substituted PromQL probe is not verification:
  run its candidate verifier before the PR when possible and inspect the Chrome
  screenshots; run its live verifier after Flux applies when candidate rendering was
  unavailable or deployment wiring changed.
- A new dashboard is `<name>.json` here plus its entry in `FILE_DASHBOARDS` in
  [`../grafana_dashboard_inputs.py`](../grafana_dashboard_inputs.py).
- Don't hand over dashboards with unresolved macros, empty frames, datasource errors, or
  unexplained all-zero panels; record any explicit empty/all-zero allow-list exception and
  its reason in the PR.

`home_network_overview.py` is a bounded panel-constructor pilot. It returns native Grafana
JSON; unusual panels and complete `fieldConfig`, `options` and targets remain ordinary
JSON dictionaries. Its Bazel `home_network_overview_json` target renders the existing
`home-network-overview.json` artifact for both deployment and visual tests. Edit the Python
source; there is no second maintained JSON copy. Other dashboards remain JSON files.
