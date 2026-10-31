# CLI retry during the VM shutdown probe

Observed 2026-10-27. This is transport-retry and invocation-identity evidence
from the [VM shutdown investigation](../../../../agentplane/system_tests/k3s/debug/vm_shutdown.md#evidence).
The VM lifecycle findings and selected TestRunner artifact identity remain there.

Baseline source: `51542844ae8d9d18e71cab407b288ec309f80cbf`. The probe ran
`//agentplane/system_tests:test_vm_sandbox_vm` through `bbr`, selecting the
Claude smoke case with uncached test results. This is the historical target name; the current VM
suite target is `//agentplane/system_tests/suites:test_vm`.

The CLI retried after a log-stream `RST_STREAM INTERNAL_ERROR`, reusing the
[Bazel invocation ID](https://app.buildbuddy.io/invocation/c7d1affd-a5eb-44f0-b09b-bf63ae1dcae4).
These outer runs were not independent manual submissions:

| Attempt   | Outer runner invocation                                                                                           |
| --------- | ----------------------------------------------------------------------------------------------------------------- |
| First     | [1a92557f-ddde-49f6-b592-39f786569728](https://app.buildbuddy.io/invocation/1a92557f-ddde-49f6-b592-39f786569728) |
| CLI retry | [5e99e4f5-636f-46a9-a5be-b91d3859ca04](https://app.buildbuddy.io/invocation/5e99e4f5-636f-46a9-a5be-b91d3859ca04) |

Invocation-level target indexes can therefore mix or replace attempt evidence.
Correlate each execution ID and outer attempt separately. Retrieve a retained
worker result with `bb execution get <execution-id> --output=json` even when
retrying has replaced the invocation's target index.

The original VM note does not contain a cancellation-acknowledgment timeline.
This retry observation does not establish failed cancellation, post-cancellation
dispatch, or the hosted lease duration. It is distinct from the 20:28 UTC
[continued-action incident](investigation.md#incident-2026-10-27-vm-test).

## Full-suite replay on 2026-10-31

A `bbr --script` benchmark ran a warm build and then fresh full-suite test
passes with explicit child invocation IDs. The CLI's log observer received
`RST_STREAM INTERNAL_ERROR` while the first outer runner had already accepted
and run work. It retried the whole script under a second outer invocation,
reusing the same child IDs and starting another full-suite pass. Canceling only
that outer invocation while the local CLI stayed alive triggered a third outer
attempt (`invocation cancelled` was classified as retryable). The local launcher
was then stopped before the active outer and child invocations were canceled.

| Attempt                        | Outer runner invocation                                                               |
| ------------------------------ | ------------------------------------------------------------------------------------- |
| Original                       | [ab9396a7](https://app.buildbuddy.io/invocation/ab9396a7-c8ef-4d1c-ad96-506c0472d412) |
| Observer-error retry           | [bed0fc63](https://app.buildbuddy.io/invocation/bed0fc63-54f3-463d-97d8-c77fbcf2e544) |
| Retry after outer cancellation | [6bd05016](https://app.buildbuddy.io/invocation/6bd05016-b08f-4085-bd6c-6d14f1dcf896) |

The outer invocations above and child [baseline test](https://app.buildbuddy.io/invocation/7ea29197-b0ed-467f-adff-ee1720988897)
and [candidate test](https://app.buildbuddy.io/invocation/88529384-7bc3-4ce7-b241-1576b288bd9f)
show the repeated work. After cleanup, both active child IDs had only terminal
execution records. This establishes the replay mechanism, not a latency
estimate for normal CI. It is a **bb CLI whole-run retry**, separate from the
[Bazel Execute RPC retry](rpc_retry_trace.md) controlled by Bazel's
`--remote_retries`; changing that Bazel option would not prevent this replay.
