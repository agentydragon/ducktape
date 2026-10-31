# Pod storage lifecycle latency, 2026-10-31

The `test_fresh_workspace_storage_survives_replacement` case had a 141.3 s window
in the hosted Pod suite invocation
[`06e9a44f`](https://app.buildbuddy.io/invocation/06e9a44f-e234-5785-9934-e647b97698fe).
That measurement covers the case body and fixture cleanup, not only storage I/O.
The test requires an empty Sandbox inventory, suspension and workload removal,
incompatible-claim refusal, and resume with the same PVCs and contents. Its
resources cannot be overlapped with another Sandbox lifecycle.

The scenario now emits OTel spans to `test.outputs/vm-phases.jsonl` around those
operations. A focused real Pod run on RBE passed all assertions in invocation
[`c76e057c`](https://app.buildbuddy.io/invocation/c76e057c-2287-45f2-8e2f-c8ba9234f464).
Its `vm.case` span lasted 81.3 s. Ready took 5.9 s, first suspension 8.0 s,
resumed Ready 2.0 s, and the single enclosing cleanup span took 60.6 s. The
process trace showed two Quiesce calls about 59.5 s apart during that cleanup;
the preceding and following `WatchState` streams each lasted about 61 s. A long
stream span does not establish that the caller was blocked on it.

A second focused run added nested spans for cleanup suspension, Sandbox
deletion, and PVC deletion. It passed in invocation
[`794e67d0`](https://app.buildbuddy.io/invocation/794e67d0-0058-4850-ab65-3b6f53833221),
with `vm.case` at 28.7 s: final suspension took 8.1 s, Sandbox deletion 0.21 s,
and PVC deletion 0.22 s. The storage output recorded `passed`,
`cleanup_complete`, and `incompatible_claim_refused` as true. The process trace
showed short Quiesce RPCs and no 60 s retry gap in this run.

These are separate runs, not a before/after comparison. The run-to-run variation
places the 60 s tail in final suspension/reconciliation when it occurs, while
leaving its cause unresolved. The existing process collector retained spans but
not span events or the Runner's quiesce-stage transitions, so it could not show
whether suspension was still flushing state or whether a terminal fact was
published late or missed by reconciliation. The follow-up should retain only
bounded stage events and compare Runner observation/publication with Gateway
reconciliation. Keep the one-minute safety deadline and all storage assertions
unchanged; do not infer recoverable savings from the 60 s interval alone.

Reproduce with:

```sh
bbr test //agentplane/system_tests/suites:test_pod \
  --test_arg=-k \
  --test_arg=test_fresh_workspace_storage_survives_replacement \
  --cache_test_results=no
```

Download `test.outputs/vm-phases.jsonl`, `test.outputs/process-spans.jsonl`, and
`test.outputs/storage-pod-directory.json` from the invocation to inspect the
scenario phases, RPC lifetimes, and fixture assertions. The case window is the
`vm.case` span; do not use the enclosing Bazel test-action duration as the case
latency.
