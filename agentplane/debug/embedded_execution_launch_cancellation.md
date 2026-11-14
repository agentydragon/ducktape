# Embedded Execution cancellation before native launch acknowledgement

On 2026-11-14, direct guest target
`//agentplane/x/direct_vm_boot:test_runtime_codex_session` failed in invocation
`d0ad8994-4e52-5118-b12a-e2feeba936f6` at source `d74c813c3390`. Controller
`WatchState` returned `CANCELLED`, with `wait for Execution API readiness: context
canceled` and `embedded Execution service exited without backend fence proof`.

The fixture bounds each initial Controller snapshot read to two seconds and
cancels that stream before retrying. Runtime adoption uses the stream context;
Execution startup runs within that accepted transition. Cancellation requests
shutdown of the independent embedded service lifetime and joins it before retry.

A managed native launch interrupted before returning its `Instance` can finish
fencing successfully. Previously `Host.Launch` returned its original cancellation
joined with the successful stop's nil error. `NativeStart` propagated that error,
and embedded service startup returned before installing its `Instance.Stop`
cleanup handler. Runtime therefore could not distinguish a fenced interrupted
launch from an uncertain launch, and closed admission.

The candidate repair preserves `ErrFencedLaunch` only after the exact managed
attempt's stop and resource release succeed. Embedded service startup accepts
that proof during its own canceled lifetime. A failed or unproved fence still
returns an error and keeps admission closed. The caller receives cancellation;
it can retry adoption for the same incarnation only after the startup/fence
barrier settles. No native mutation is replayed by this change.

The failing guest's serial log records actual Runtime reconciliation at
20:48:35.475 UTC, Execution process/scope setup at 20:48:36.379–36.651, process
teardown reconciliation at 20:48:39.153–39.362 and Runtime shutdown at
20:48:44.863. Exact Controller cancellation and process-fence status were not
captured, so the two-second observer cancellation trigger and successful native
fence are source-grounded explanations rather than directly logged facts.
These source paths were already present in the preceding passing baseline;
the integration alone does not establish when the race was introduced.

A separate cleanup assertion reported `EADDRINUSE` after Docker stop, restored
state ownership and successful container removal. Its bind probe omits
`SO_REUSEADDR` and captures no TCP state, so it cannot distinguish a live listener
from `TIME_WAIT`. Gateway calls already saw connection refused after Runtime
exit. This startup repair makes no claim to fix that cleanup assertion.

Raw serial, Gateway, QEMU and container logs remain private under
`/tmp/agents/2026_11_12/agentplane-ci-replan/iteration11/guest-readiness-diagnosis/`.
