# GitHub GraphQL quota exhaustion

Tracking issue: [#5213](https://github.com/agentydragon/ducktape/issues/5213).

The September 6 controlled block/reversal causally linked a major part of the
observed burn to Claude Desktop's cloud-mediated GitHub batch-status route. The
operator accepted Desktop functionality after the relay repairs and requested
a one-to-two-week observation window. The October 10 review did not establish
quota reliability: the seven-day and fourteen-day checkpoints both observed
zero remaining points, and another zero was retained on October 5. The recent
week's fresh-sample coverage was 96.99%. These account-level samples do not
identify the caller; correlate the October 5 zero with Desktop/web use, the
deployed relay and exact-route block, capture/request metrics, and exporter
gaps before drawing a new conclusion. Keep #5213 open. Rugged's source opt-in
has no verified live rollout, and the exact refresh trigger remains partly
unobserved. See the [latest review and pickup checklist](acceptance_monitoring.md#latest-review-2026-10-10).

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
