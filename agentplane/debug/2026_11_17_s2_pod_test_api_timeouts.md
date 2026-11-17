# S2 Pod system-test Kubernetes API timeouts

PR #17138, head `9903f2264aa554b281c366df748b4fd9eab5a407`, failed
`//agentplane/system_tests/suites:test_pod` in
[Bazel invocation a84602b7](https://app.buildbuddy.io/invocation/a84602b7-5c38-58f5-840c-ddacc57ee234)
([Actions job](https://github.com/agentydragon/ducktape/actions/runs/44012364438/job/133802260578)).
The underlying cause of the API latency is not established. No workflow or unchanged test was
rerun to investigate this failure. S2's system-test gate remains unsatisfied.

## Observed failure chain

All timestamps below are UTC on 2026-11-17. The retained `persistence-suite.json` reports a warm
k3s baseline. The target ended with 11 failed scenarios, five passed, and a teardown error.

- `k3s-retained.log` records Kine SQL reads and writes taking 1.24–1.87 seconds at
  03:52:11–03:52:18. It also records resource-version cache lag and API handler timeouts.
- The Sandbox Controller's Lease GET exceeded its five-second request deadline at
  03:52:24.929. It lost leader election and exited at 03:52:25.482.
- The Policy Controller's Lease GET exceeded its deadline at 03:52:25.880. It lost leader
  election and exited at 03:52:26.166. The trust-manager Lease PUT also timed out.
- Kubelet housekeeping took 11.581 seconds; multiple probe executions exceeded five seconds.
  These observations establish a broader control-plane/runtime delay, not a rejected RBAC grant.
- App Environment watch/suspend calls returned HTTP 503. Scenario cancellation and Sandbox
  cleanup failures followed. Baseline certification refused recycling because three test
  Sandboxes remained, preserving the fixture's cleanliness gate.

The failure artifacts contain no evidence establishing CPU scheduling, disk latency, SQLite
contention, or another mechanism as the underlying cause. Low sampled container working sets do
not exclude host pressure. Do not label this a flake or alter timeouts, test size, concurrency,
or retention policy on this evidence.

## Scope and next evidence

The S2 rendered audit found every existing non-policy root/delivery object unchanged, including
Pod specifications, old Roles and Certificates. Existing Cilium rules remain present. New
ServiceAccounts are unused and no new Deployment runs. This constrains investigation; it does
not prove the added Certificate issuance is unrelated to the test's API load.

Use the invocation's `test_pod` artifacts: `test.log`, `k3s-retained.log`,
`containerd-retained.log`, `persistence-suite.json`, `process-spans.jsonl`,
`test-spans.jsonl`, `memory/working-sets.jsonl`, and certificate diagnostics. Correlate the API
stall with executor scheduling/storage evidence and concurrent work before selecting a fix.
Any infrastructure or service behavior change belongs in its own reviewable PR.

The independent `test_workload_trust` failure in the same invocation came from collapsing
multiple Certificates into a URI-keyed dictionary. S2 prepares unused replacement leaves with
the existing URIs. Its correction preserves checks on every leaf's usages and requires a
read-only mounted leaf for every service URI; it does not resolve this Pod failure.
