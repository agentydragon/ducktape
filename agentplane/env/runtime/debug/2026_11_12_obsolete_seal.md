# Obsolete Session seal work after successor adoption

The 50-job CI control at source `28dae5ef3` failed
`TestLifecycleBarrierInterleavings/replace/setup-admission-replace` with
`replacement did not reopen journal`:
[invocation `962a6b85`](https://app.buildbuddy.io/invocation/962a6b85-dfd8-45c3-9b92-5594c954d28f).
The Go race detector reported no data race. The failed assertion observes a
lifecycle ordering error: Runtime accepted the successor while the journal was
sealed.

`sessionLifecycleController.Reconcile` reads an environment revision before it
starts its one-off seal worker. Successor adoption publishes a newer Running
revision and `Manager.Reopen` clears the old journal seal under `admitMu`. An
older reconcile can then close admission and start its seal worker. The original
worker did not carry or check the revision: it read the current incarnation UID
and sealed the successor's journal. Locking the journal write alone did not
prevent obsolete work from running after a valid reopen.

The deterministic PID1 regression seals the old incarnation, reopens a successor,
then starts a worker carrying the old shutdown revision. With the original
sealing body, it fails because the new journal is sealed with `IncarnationUID:
successor` and the cutoff is closed:
[control `f0ad523a`](https://app.buildbuddy.io/invocation/f0ad523a-55a4-45a5-a910-bde11c9aae08).
The control adapts the internal method signature so the same test compiles while
retaining the original mutation behavior. This demonstrates the mechanism; the
historical CI log does not contain the exact obsolete worker's scheduling trace.

The candidate puts the final admission cutoff and journal sealing in one worker.
Under the same lock used by successor reopening, it checks the exact desired
revision, ignores obsolete work, and seals with that revision's explicit seal UID.
The regression also admits a successor command and verifies a later, current
shutdown still seals that successor. No retry or assertion delay is added.

Both full PID1 suites, `//agentplane/env/runtime/session:session_test` and
`//agentplane/env/runtime:runtime_test`, passed with the candidate and the race
detector enabled:
[validation `a2aa6aa3`](https://app.buildbuddy.io/invocation/a2aa6aa3-b905-4ca0-aba4-7dc508193696).
This includes the existing lifecycle interleavings and the deterministic obsolete
worker regression. It does not establish a new failure-rate estimate for CI.
