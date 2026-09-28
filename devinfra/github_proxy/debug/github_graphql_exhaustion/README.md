# GitHub GraphQL quota exhaustion

Tracking issue: [#5213](https://github.com/agentydragon/ducktape/issues/5213).

The observed large burn is causally linked to Claude Desktop's cloud-mediated
GitHub batch-status route. Wyrm2 uses the central exact-route block through
a local relay. After repairing SSE buffering (#5699) and connection reuse
(#5703), the operator confirmed Desktop works on September 6, 2026 and asked
to wind down active debugging. Existing mitigation, capture, and metrics remain
running. Review the next **1–2 weeks, September 13–20**, before closing #5213;
the reliability result is not yet established. Rugged has a source opt-in but
no verified rollout; the exact refresh trigger and upstream work remain partly
unobserved. [Confirmation checklist and query window](acceptance_monitoring.md).

- [Attribution and controlled block/reversal](attribution_2026_09_05.md):
  measurements, source evidence, competing explanations, and remaining experiments.
- [Desktop proxy operation](desktop_proxy.md): normal launch and OAuth routing,
  private CA trust, temporary block, capture durability, rollback, and central
  HTTPS/password compatibility findings.
- [Central migration](central_proxy_migration.md): verified server rollout and
  real relay canary, host cutover gates, local mitigation retirement and remaining
  monitoring limitations.
- [Wyrm2 cutover](wyrm2_central_cutover.md): installed relay, preserved captures,
  repaired Desktop regressions, deployed-image proof, and operator acceptance.
- [Acceptance monitoring](acceptance_monitoring.md): exact retained-metric
  queries, coverage requirements, historical gaps, and notification limitations.
- [Notification delivery](notification_delivery.md): the source-specific ntfy
  TCP failure and the boundary between rule evaluation and actual receipt.
- [Central capture storage](central_capture_storage.md): unsupported CSI volume
  statistics, the native collection storage-budget signal, and its limits.
- [Earlier investigation](earlier_investigation.md): prior measurements and
  hypotheses; its header identifies superseded conclusions.
- [Wyrm2 restart pod delta](wyrm2_pod_delta_2026_09_04.md): supporting historical
  evidence about the scope of the node-off control, not current caller exclusion.

[Recorder coverage](recorder_coverage_2026_09_06.md) documents the measurement
boundaries. [#5663](https://github.com/agentydragon/ducktape/issues/5663) tracks
retained process/resource history; [#5666](https://github.com/agentydragon/ducktape/issues/5666)
tracks attributed in-cluster GitHub proxying.

Raw captures, credentials, private request variables, and session identifiers
are not investigation artifacts to commit here. Preserve local originals;
publish sanitized evidence with its attribution and coverage limits.
