# CLI retry during the VM shutdown probe

Observed 2026-10-27. This is transport-retry and invocation-identity evidence
from the [VM shutdown investigation](../../../agentplane/system_tests/k3s/debug/vm_shutdown.md#evidence).
The VM lifecycle findings and selected TestRunner artifact identity remain there.

Baseline source: `51542844ae8d9d18e71cab407b288ec309f80cbf`. The probe ran
`//agentplane/system_tests:test_vm_sandbox_vm` through `bbr`, selecting the
Claude smoke case with uncached test results.

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
