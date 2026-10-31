# Pod storage cleanup: Runner quiesce retry

This is a visibility result, not a latency fix. The storage-preservation case passed with its
cleanup, incompatible-claim refusal, resume, and content checks intact. No lifecycle deadline or
barrier changed.

## Observed run

Filtered `//agentplane/system_tests/suites:test_pod` on RBE passed on 2026-10-31 in
[invocation `ad096587`](https://app.buildbuddy.io/invocation/ad096587-8681-41b6-9ac7-49cffdff3637).
The test action took 174.8 s; `vm.case` took 85.399 s. Artifacts are
`test.outputs/process-spans.jsonl`, `test.outputs/vm-phases.jsonl`, and
`test.outputs/storage-pod-directory.json`. The latter records `passed`,
`cleanup_complete`, `incompatible_claim_refused`, and a successful resumed probe.

Relative to `vm.case` start, `sandbox-controller`'s completed
`agentplane.sandbox_controller.runner_quiesce` spans recorded:

|    Offset | Duration |        gRPC status |
| --------: | -------: | -----------------: |
|  +6.300 s |  15.0 ms |           0 (`OK`) |
| +19.271 s |   4.7 ms | 14 (`UNAVAILABLE`) |
| +78.676 s |  24.4 ms |           0 (`OK`) |

The Runner Gateway's WatchState span starting at +18.042 s observed and published `none` first.
It recorded no stage change until +78.700 s, when it observed `draining`, `flushing`, and
`quiesced`; each stage was published to the Lease within milliseconds. The Quiesce retry began
at +78.676 s. Thus this sample shows no already-published progress waiting for controller
observation: the Lease remained at `none` through the retry interval, and Gateway publication was
prompt once stage changes arrived.

`Reconciler.quiesce` persists the attempt before calling the Runner. On any Quiesce error it
returns `retryAfter: quiesceDeadline`; while the same Runner Pod UID's attempt is younger than the
one-minute deadline, it does not call again ([controller.go](../../sandbox_controller/controller.go#L397)).
The observed interval from `UNAVAILABLE` to the successful retry is 59.405 s. This is measured
retry delay, not recoverable savings: the error's source and the absence of Runner-side transition
spans remain unresolved, so the current safety policy stays in place.

The failed RPC has controller and Gateway spans, but no retained Runner server `Quiesce` span.
Runner emitted a short `none` stage span at +18.058 s, but no Runner stage spans for the later
Gateway-observed transitions. The artifacts therefore identify `UNAVAILABLE` at the controller's
Runner call boundary, not whether it came from Gateway connection state, Runner authorization, or
Runner lifecycle state. The test log contains no more specific status detail. The next visibility
boundary is the Gateway-to-Runner forwarding result and the corresponding Runner server span;
do not infer a component-level cause from status 14 alone.

Focused checks passed on RBE in [invocation `b4d634d8`](https://app.buildbuddy.io/invocation/b4d634d8-7029-44ad-b34c-57312104460c):
`//agentplane/runner/control:control_test`, `//agentplane/runner_gateway/gateway:gateway_test`,
`//agentplane/sandbox_controller:sandbox_controller_test`, and
`//agentplane/system_tests/k3s:test_process_traces` (4/4). Pre-commit passed on the changed files.
