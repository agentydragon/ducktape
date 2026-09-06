# GitHub GraphQL quota exhaustion

Tracking issue: [#5213](https://github.com/agentydragon/ducktape/issues/5213).

The observed large burn is causally linked to Claude Desktop's cloud-mediated
GitHub batch-status route. An exact-route temporary block is verified on wyrm2;
rugged has a source opt-in but no verified rollout. The investigation remains
open: the exact refresh trigger and upstream work are partly unobserved, and
the required multi-day exhaustion-free acceptance window is not complete.

- [Attribution and controlled block/reversal](attribution_2026_09_05.md):
  measurements, source evidence, competing explanations, and remaining experiments.
- [Desktop proxy operation](desktop_proxy.md): normal launch and OAuth routing,
  private CA trust, temporary block, capture durability, rollback, and central
  HTTPS/password compatibility findings.
- [Central migration](central_proxy_migration.md): verified server rollout and
  real relay canary, host cutover gates, local mitigation retirement and remaining
  monitoring limitations.
- [Acceptance monitoring](acceptance_monitoring.md): exact retained-metric
  queries, coverage requirements, historical gaps, and notification limitations.
- [Notification delivery](notification_delivery.md): the source-specific ntfy
  TCP failure and the boundary between rule evaluation and actual receipt.
- [Earlier investigation](earlier_investigation.md): prior measurements and
  hypotheses; its header identifies superseded conclusions.
- [Wyrm2 restart pod delta](wyrm2_pod_delta_2026_09_04.md): supporting historical
  evidence about the scope of the node-off control, not current caller exclusion.

The recorder coverage report is part of
[PR #5665](https://github.com/agentydragon/ducktape/pull/5665), under this directory
once landed. [#5663](https://github.com/agentydragon/ducktape/issues/5663) tracks
retained process/resource history; [#5666](https://github.com/agentydragon/ducktape/issues/5666)
tracks attributed in-cluster GitHub proxying.

Raw captures, credentials, private request variables, and session identifiers
are not investigation artifacts to commit here. Preserve local originals;
publish sanitized evidence with its attribution and coverage limits.
