# Pod system-test deletion tail, 2026-10-31

The post-routing Pod suite in BuildBuddy invocation
`856dbe85-5d71-594c-acb0-ad16366407aa` passed all 35 tests. Its
source revision was `c40cc24a` on the Quiesce-routing branch, before the
ordered telemetry relay merged as PR #13319. This chronology matters to
interpreting its Runner shutdown timings. Its
`//agentplane/system_tests/suites:test_pod` undeclared outputs contain
`test-spans.jsonl`, `process-spans.jsonl`, `containerd.log`, and `k3s.log`.
Download the latter three with `bbapi artifact download <invocation> \
//agentplane/system_tests/suites:test_pod/test.outputs/<name> -o <local-path>`.
The first file also exists at
`/tmp/ci-latency-top10/next-lanes/post-routing-pod/test-spans.jsonl` in
the investigation scratch directory.

There are 19 `system_test.sandbox.cleanup` spans: median 9.50 seconds,
range 2.99–11.84 seconds. `suspend.accepted` to `suspended` is normally
under 0.1 seconds. `delete.accepted` to `deleted` ranges 2.32–11.14
seconds. `suspend_and_delete` already uses the Sandbox inventory stream and
waits for the UID-owned resource to disappear after its finalizer releases
the Pod and volumes. Its wait is the correct physical cleanup barrier.

For the messaging Pods at 10:29:14–10:29:26 UTC, the controller's
`runner_quiesce`, gateway observation, and Runner `quiesce_stage` spans
show the Runner reaching `quiesced` within about 50 ms. The older
minute-long Quiesce routing retry did not occur. For example,
`systest-msg-to-1` was accepted for deletion at 10:29:16.337,
containerd began stopping its containers at 10:29:17.899, and the Runner
stopped in 0.058 seconds. Kubelet unmounted its state and workspace
volumes at 10:29:18.233, and the Sandbox disappeared at 10:29:18.658.

The longer `systest-msg-from-2` case was accepted at 10:29:16.937.
Its Runner's `StopContainer` ran from 10:29:20.302 to 10:29:25.333,
5.031 seconds; the egress sidecar stopped in 0.051 seconds. Kubelet began
unmounting its volumes at 10:29:25.591 and the Sandbox disappeared at
10:29:25.712. Mapping containerd `RunPodSandbox` and `CreateContainer`
IDs to `StopContainer` pairs across the suite gives 15 Runner stops
between 5.026 and 5.092 seconds, with other Runner stops around
0.04–0.08 seconds. This bimodality is a Runner container shutdown path,
not a slow Quiesce RPC or client polling. A separate pre-fix Pod storage
run had already identified the old relay lifecycle: kubelet stopped the
ordinary egress sidecar before Runner's final OTLP flush, leaving Runner
alive for about five seconds. PR #13319 made the relay a native sidecar;
its real Pod storage proof (`dd7ffc77-ecaf-4ef1-a021-e47849c292a6`)
shows the replacement Runner exited 26 ms after SIGTERM, before kubelet
stopped the relay. Thus the old sample cannot establish a remaining
native child grace problem. The Runner supervisor's five-second child
grace is a separate safety fence and should not be changed from these data.

No timeout, force kill, finalizer, UID check, or volume-release barrier
should change based on this result. A fresh filtered Pod storage run on
current `devel` is needed to measure the remaining deletion tail after
the ordered relay fix; compare its physical Pod and volume cleanup
timestamps before proposing another lifecycle change.
