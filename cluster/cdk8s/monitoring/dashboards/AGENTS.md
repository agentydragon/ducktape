# Grafana dashboards

- Before creating or changing a dashboard, read and follow
  [`grafana/skill/SKILL.md`](../../../../grafana/skill/SKILL.md) — mandatory. Valid
  JSON, a passing Kustomize build, or a hand-substituted PromQL probe is not verification:
  run its candidate verifier before the PR when possible and inspect the Chrome
  screenshots; run its live verifier after Flux applies when candidate rendering was
  unavailable or deployment wiring changed.
- Author dashboards with native Foundation SDK builders/models and shared helpers in
  `foundation.py`. The owning generator embeds `JSONEncoder().encode(...)` directly
  in the `GrafanaDashboard`; keep symbolic datasource references and explicit Operator
  mappings to provisioned UIDs.
- Add the builder to `//cluster/cdk8s:dashboard_json_bin` for candidate/browser-test
  artifacts. The shared exporter writes unformatted JSON; no per-dashboard binary or
  checked-in JSON copy is needed.
- Don't hand over dashboards with unresolved macros, empty frames, datasource errors, or
  unexplained all-zero panels; record any explicit empty/all-zero allow-list exception and
  its reason in the PR.
