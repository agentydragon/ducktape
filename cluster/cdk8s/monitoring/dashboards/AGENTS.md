# Grafana dashboards

- Before creating or changing a dashboard, read and follow
  [`grafana/skill/SKILL.md`](../../../../grafana/skill/SKILL.md) — mandatory. Valid
  JSON, a passing Kustomize build, or a hand-substituted PromQL probe is not verification:
  run its candidate verifier before the PR when possible and inspect the Chrome
  screenshots; run its live verifier after Flux applies when candidate rendering was
  unavailable or deployment wiring changed.
- A new dashboard is `<name>.json` here plus its entry in `_FILE_DASHBOARDS` in
  [`../grafana_instance.py`](../grafana_instance.py).
- Don't hand over dashboards with unresolved macros, empty frames, datasource errors, or
  unexplained all-zero panels; record any explicit empty/all-zero allow-list exception and
  its reason in the PR.
