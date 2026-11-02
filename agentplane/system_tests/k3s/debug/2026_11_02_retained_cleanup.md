# Retained-stack cleanup, 2 November 2026

## Finding and shared change

The recurring post-diagnostic tail is primarily Kubernetes namespace deletion, not
local process shutdown or loop-device release. Instrumented controls measured
29.6/32.2 seconds of Pod cleanup and 59.2/59.7 seconds of MCP Delegation cleanup.

Both fixture-owned namespaces were deleted serially. Even the credentials namespace
with no workload Pods took approximately 5.3 seconds to disappear. Submit both
deletions in one kubectl command before waiting, allowing their namespace-controller
reconciliation to overlap. Monitoring remains a later deletion so terminating
fixture services can still export to the receiver. Resource absence checks,
workspace release and clean-marker certification retain their ordering and behavior.

The
[kubectl deletion implementation](https://github.com/kubernetes/kubectl/blob/v0.34.1/pkg/cmd/delete/delete.go)
visits the selected resources and issues deletions before its wait phase. This is
overlap of existing work; it does not shorten termination grace, remove finalizers
or relax the requirement that fixture-owned resources are gone before reuse.

## Instrumented control and candidate

Effective source is `471205dc015fa0ca84cd93d8eec5871db34088d6` plus cleanup spans
for the control, then the namespace batching change for the candidate. Each ordinary
suite ran twice with `--nocache_test_results --runs_per_test=2`, on RBE.

| Suite                    | Control cleanup, seconds | Batched cleanup, seconds | Mean phase reduction |
| ------------------------ | ------------------------ | ------------------------ | -------------------- |
| Pod, all 13 cases        | 32.2 / 29.6              | 24.2 / 24.5              | 6.6 seconds          |
| MCP Delegation, one case | 59.2 / 59.7              | 54.4 / 56.3              | 4.1 seconds          |

[Control invocation](https://app.buildbuddy.io/invocation/49ca8687-4734-43c8-824a-6601ed7bc9ed)
and
[candidate invocation](https://app.buildbuddy.io/invocation/a80384d4-b519-4a60-a387-09d65660a971)
each passed four physical test executions. All receipts report completed cleanup;
the scenario identities/assertions are unchanged. Every selected baseline was cold:
changing `stack.py` correctly invalidates its runtime fingerprint. These are small,
unmatched-worker phase comparisons, not a steady-state fleet estimate.

The Pod process times were approximately 241/280 seconds for controls and 260/260
for candidates; MCP was 169/171 versus 177/173. Whole-action time did not show a
consistent reduction. Changes in setup and scenario work can hide a small cleanup
benefit. Do not promote the measured phase reduction to a causal CI feedback saving.

Controls put fixture process close at 0.8–1.7 seconds and volume release near zero.
The main namespace took 12.6–13.6 seconds in Pod tests and 43.1–43.3 seconds in MCP;
credentials and monitoring each added 5.2–5.5 seconds serially. The new spans cover
these operations and the previously untraced remainder through collector shutdown.

## Separate MCP long pole

Containerd records identify `mcp-everything` as the extra MCP delay. It receives
SIGTERM, waits 30 seconds, then receives SIGKILL and exits 137. The exact pinned
image's `/app/dist/streamableHttp.js` registers a graceful SIGINT handler, closes
transports and exits zero, but has no SIGTERM handler. Other fixture containers
stop in less than a second in the inspected control.

This is a separate fixture shutdown defect. Changing the signal path needs its own
real MCP validation; the namespace batching result does not establish that repair.

Raw invocation and physical execution responses, JUnit, driver/process JSONL,
receipts and daemon logs remain under task scratch. Physical actions were joined
by execution identity, artifact run index and TestResult duration; overlapping VM
wall-clock intervals alone are ambiguous. Test events appeared later than completed
physical execution records, so the first control artifacts were retrieved directly
from their cached execution responses and REAPI output trees.
