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

The Foundation SDK spike authors CPAP gateway disk, Flux cluster, and Rugged Power in
Python. Edit their typed builders; `foundation_dashboard` supplies each hyphenated JSON
artifact to deployment and the existing recorded-data visual tests. The shared Mimir
reference uses `${DS_MIMIR}`; `FILE_DASHBOARDS` maps it to the literal provisioned UID
`mimir`. Other dashboards remain authored JSON. The SDK's classic API retains the
existing Grafana Operator format.
