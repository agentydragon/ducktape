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

## Why the late Runner spans were not collected

The same run's `containerd.log` identifies the replacement Pod's Runner container as
`24470f2d...` and its egress sidecar as `c31f8ea0...`. At 09:45:34.185 UTC kubelet sent
SIGTERM to both. The egress sidecar exited at 09:45:34.231, while Runner remained alive until
09:45:39.238. The successful Quiesce RPC was at 09:45:34.060–34.068, and Gateway received and
published `draining`, `flushing`, and `quiesced` before 09:45:34.185. Runner's
`forwardWatchState` records a completed child span for each stage before sending it to Gateway.
The collector retained Runner's earlier `none` span but none of those later spans; it continued
receiving Gateway spans during this interval.

Runner's configured OTLP endpoint is `127.0.0.1:3132`, the egress sidecar's telemetry route to
Alloy. Its SDK batches exports and flushes during process shutdown with a five-second bound.
Kubelet removed that route about 46 ms after signaling Runner, while Runner still had late
spans to export. The exact exporter error is not retained in this run, so the artifacts do not
prove which individual export attempt failed. They do establish that ordinary concurrent Pod
container termination cannot guarantee collection of shutdown-adjacent Runner spans. The
Runner Pod template therefore makes the egress relay a Kubernetes-native sidecar: an
`initContainer` with `restartPolicy: Always`, which kubelet terminates only after ordinary
containers have exited. This keeps the existing protected telemetry route available through
Runner's SDK shutdown; no sampling policy, deadline, or application lifecycle barrier changes.
The earlier `UNAVAILABLE` at 09:44:34.643 remains a separate unresolved RPC failure.
