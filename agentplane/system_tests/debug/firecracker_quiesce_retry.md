# Firecracker cleanup waits for a failed seal retry

The fixed-source `6bebe271d8` guest-reuse A/B/B/A passes all four invocations,
four fresh physical actions and six nonempty JUnit cases, without retries or
skips. A creates two separately clean guests; B composes unchanged issuer trust
and live-state assertions on one fresh guest. These are targeted measurements,
not complete required feedback or an isolation-equivalence claim.

| Arm                                                                             | Native invocation | Execution | Guest cleanup    | Imports not skipped |
| ------------------------------------------------------------------------------- | ----------------: | --------: | ---------------- | ------------------: |
| [A1](https://app.buildbuddy.io/invocation/572ec341-d887-49e7-9a5d-27c090a1a7c3) |          343.286s |  300.022s | 20.968s, 76.358s |                   4 |
| [B1](https://app.buildbuddy.io/invocation/21b2e916-d0d0-45ab-b2ba-9e878fc2844c) |          424.462s |  270.136s | 81.449s          |                  12 |
| [B2](https://app.buildbuddy.io/invocation/a9b44bc7-d5af-47b3-8e6d-06df1835b983) |          246.365s |  199.253s | 13.855s          |                  12 |
| [A2](https://app.buildbuddy.io/invocation/289fcb99-c6ef-477e-9d88-034263b14a29) |          357.140s |  313.951s | 69.361s, 67.363s |                   0 |

All arms retain the actual kernel process identity, forward progress, certificate
rotation and real one-minute idle checkpoint/resume. CPU is unknown. Native and
execution comparisons have different queue/import exposure; they cannot establish
a guest-reuse saving by themselves.

Four of six guest cleanups have an initial controller `runner_quiesce` returning
gRPC `Unavailable`, then a successful call about one minute later. Successful
sealing is followed promptly by Pod deletion and observed Sandbox absence. In B2,
the first seal succeeds and cleanup takes 13.855s. The extra minute is the
controller's persisted `quiesceDeadline`, not a minute of VMM deletion.

The scoped Gateway diagnostics and matching collector allowlist identify the
rejection in [PR CI](https://app.buildbuddy.io/invocation/9e943df5-9491-5e9a-9df7-1e35e45c734d),
testing merge `056bd735be` containing branch `6aae2a61a5`. At 05:04:57.835 UTC,
the Gateway has `sandbox.phase=Quiescing`, the expected Pod UID and
`runtime.proxy.pod_ready=false`; it returns `destination_unavailable` without
forwarding to Runtime. Its verified control-ready event arrives at 05:04:58.786,
but the controller does not retry until 05:05:57.015. Cleanup takes 70.270s.
The other guest's first seal succeeds and cleanup takes 17.083s.

The Controller's direct Runtime WatchState can become fresh before the Gateway's
Pod inventory and elected route are ready. Direct observation alone therefore
does not establish proxy readiness. No free-form error descriptions or request
data are needed to distinguish this failure.

The candidate keeps every Gateway readiness/authorization check and the persisted
controller cooldown. A single handshake makes at most three attempts on
`Unavailable`, one second apart and within the existing 30-second total budget.
Each attempt opens a fresh connection to resolve the elected control Service and
requires the originally requested incarnation Pod UID. Gateway aborts a changed
incarnation before forwarding; authorization refusals and acknowledged unfinished
seals are not retried inside the handshake.

The [old-behavior probe](https://app.buildbuddy.io/invocation/baed436b-71df-4d42-a798-59ec5d452f2b)
fails the new recovery and expected-incarnation assertions. The
[candidate's focused validation](https://app.buildbuddy.io/invocation/36520d5d-7f17-4881-9140-55df5709e645)
passes all four targets: Gateway proxy, controller, quiescer and transport client.
The [unchanged real Firecracker validation](https://app.buildbuddy.io/invocation/772f9305-8a5d-4c60-9f01-f52580d26799)
at literal `458e22a745` passes two nonempty cases in one NEW physical action,
without failures, errors, skips or retries. Its trust guest reproduces
`PodReady=false` and `destination_unavailable`; the second request forwards for
the same incarnation 1.008s later. Cleanup takes 14.564s. The other guest's first
seal succeeds, with 43.819s cleanup. Separate continuation evidence preserves the
live guest's PID, start ticks, kernel boot and marker across suspend/resume, with
its progress counter advancing. Native invocation elapsed is 658.540s, including
253.778s action queue; physical execution is 284.171s. This proves the targeted
repair, not a matched complete-PR saving or a fleet failure-rate reduction.

The independently reviewable production port is
[#16344](https://github.com/agentydragon/ducktape/pull/16344), merged as `02dc3c7a2e` at 2026-11-14 05:56:35 UTC after head `c323fc94eb` passed
Bazel CI, pre-commit and artifact/import checks. It excludes the
architecture experiments and extra tracing. From that merge, inspect the first
20 fresh Firecracker actions containing its patch: first-call rejection,
within-handshake recovery, exhaustion, incarnation fences and cleanup phases,
retaining failed/cancelled outcomes. Track this and the unchanged broader gate
qualification in [#16099](https://github.com/agentydragon/ducktape/issues/16099).
