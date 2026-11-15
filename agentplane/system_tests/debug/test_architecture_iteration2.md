# Test architecture pivot: second iteration

Source branch `agent/test-architecture-pivot`, draft PR
[#16102](https://github.com/agentydragon/ducktape/pull/16102), tracker
[#16099](https://github.com/agentydragon/ducktape/issues/16099).
Controls pin published `d9235bb36a95f58e70f351f756464bd8ed3d48d4` with both
`bbr --run_from_branch` and `--run_from_commit`; this disables local patches.
Candidate spike invocations include source overlays and are described as such.
Fresh tests use `--nocache_test_results`, one attempt, real hosted RBE, ordinary
remote cache and retained infrastructure. Production settings are unchanged.

## Native artifact ownership

`testing:launches` no longer adds both native CLI distributions to every caller.
The Claude-only native recovery pilot declares Claude; the Codex companion fixture
declares Codex. Other callers already declared their selected harnesses.
Configured action queries show 86 distinct Codex input paths before and zero after;
the actual Claude executable remains in both PythonZipper and RunfilesTree inputs.
This is input membership evidence, not a measurement of bytes transferred.

Fresh recovery and the deliberate discarded-journal control exercised the same
assertions. The control still fails at retained Session inventory. Fresh
`env/e2e:test_instructions` and `env/e2e:test_executor_container` also passed, covering
both-harness and Codex companion callers. The native pilot remains manual and does
not replace app, controller, packaged-image or Kubernetes acceptance.

The fresh positive sequence was A1/B1/B2/A2; TestResult times were 62.9, 38.1,
35.8 and 41.9 seconds respectively. Invocation links:
[A1](https://app.buildbuddy.io/invocation/3efb4a12-2ff7-489a-8534-eb6ba0174608),
[B1](https://app.buildbuddy.io/invocation/bde6cfe2-6aa1-4c3f-ada3-7abc7b735d2b),
[B2](https://app.buildbuddy.io/invocation/018b4d52-33e6-458a-907a-6dc4bcc2d4eb),
[A2](https://app.buildbuddy.io/invocation/79853de8-038c-44e4-9064-eb54350565df).
All passed, but two observations per arm and the material A1/A2 spread are only a
bounded screen. The actual test Command digest is identical on both arms; their
input-root digests differ, with the exact Claude subtree preserved and only A
containing the Codex subtree. The reduced closure is proved; latency benefit and
complete required-CI saving are not established by this sample.

## Recovery deployment profile

The same four ordinary CI labels now use `central` or `central_budget` in place of
`mcp`: Gateway crash, retry exhaustion, sampler roll and History outage. Their
test/scenario Python is unchanged. All four passed the central-only feasibility
run [8b92eb38](https://app.buildbuddy.io/invocation/8b92eb38-291f-4a4d-ae1e-bbe0efcb32db)
with the ordinary 120-second reconnect deadline, one case each, no skips, and clean
warm-baseline teardown. Its six-target invocation also includes rendering tests;
its 362.493-second Bazel wall is not a matched four-target comparison.

Rendered MCP/central closures are 175/162 objects and 15/12 Deployments. Only Dex,
MCP Everything and the OAuth fixture workloads/images are removed. Real app,
Actions, MCP edge, ingress, Gateway/Dispatcher, sampler, History, notifications,
egress, controllers and PostgreSQL remain. Rendering relation checks keep native
Gateway, History and sampler Deployment/Service/ConfigMap objects identical.

The profile provides operator authentication using the existing pinned Ed25519
test issuer instead of Dex. The four scenarios do not use operator login: they use
the existing app/Gateway Pod-bound Kubernetes tokens for native clients. Other
MCP/OAuth, browser, Pod, VM and Firecracker targets retain their coverage.

## Shorter real exhaustion deadline

The budget scenario deliberately waits out the configured reconnect window before
checking unknown remote effect, no replay, once-only mutation marker, native
session recovery and successful next input. The production default is 120 seconds.
Only `central_budget` configures 45 seconds; full fixture equality after normalizing
this one setting prevents an unrelated profile difference. The staging render
still omits the override. The existing Go default/configuration test passed fresh
[8d4d479d](https://app.buildbuddy.io/invocation/8d4d479d-82fb-4d90-80c4-18229c6cacef).

The five-second trial
[d58fb8fc](https://app.buildbuddy.io/invocation/d58fb8fc-a7a3-48b3-b3f2-da44de8cf737)
failed the original-session-resume assertion and cleanup reported an unsettled
exchange. Its initial exhaustion occurred after 5.8 seconds; the following probe
also exhausted against the old Gateway Pod address, while the retained EndpointSlice
already listed the new Ready Pod. Pinned grpc-go v1.83.2 has a 30-second minimum
DNS re-resolution interval in
[`internal/resolver/dns/dns_resolver.go`](https://github.com/grpc/grpc-go/blob/v1.83.2/internal/resolver/dns/dns_resolver.go#L46-L48).
The 45-second
candidate leaves room for that native transport behavior. No assertions, source
scenario, retry count or overall test timeout were relaxed.

Shortening the configured deadline removes exposure to the full default duration
in this particular integration case. Other recovery profiles retain 120 seconds;
the existing Go test checks its default and configurability. The corrected 45-second
trial [0d2e6499](https://app.buildbuddy.io/invocation/0d2e6499-e9a9-43ef-9ec2-d16c6ffd5ed8)
passed the unchanged real budget scenario and both rendering targets. This is an explicit
coverage tradeoff, not an identical-duration replacement or a flake-rate estimate.
That run took 197.421 seconds physical execution (71.889 setup, 73.825 case,
30.716 cleanup), 481.060 CPU seconds, with one uncached attempt and clean warm
baseline. It exercised the scenario's existing definitive-session-loss branch:
no automatic probe replay, a fresh replacement session, explicit marker verification
and successful next input. It did not execute the alternative original-session
resume branch; the source still requires that branch's assertions when resume occurs.

## Withdrawn Secret projection spike

A bounded Pod wrong-issuer/valid-Secret pair tested whether annotating the unchanged
Pod after replacing its Secret would improve projection readiness. The control
[d1b16a2c](https://app.buildbuddy.io/invocation/d1b16a2c-b1ca-4d2c-a856-e16c0b9e2dae)
used 154.694 seconds physical execution, 75.229 setup and 23.387 case time. The
overlay candidate
[c8b75114](https://app.buildbuddy.io/invocation/c8b75114-c0d3-46da-a6f9-e3e183c770c1)
used 156.216, 73.599 and 25.508 seconds respectively. Both passed their negative and
positive trust assertions and cleaned the warm baseline. One unmatched-worker pair
shows no saving; the annotation change was withdrawn. At the next natural slow
event, inspect the producer/projection/consumer boundaries rather than add polling
or weaken the same-Pod recovery assertion.

## Promotion remains open

The complete required PR gate, all three required checks, failure/rerun-inclusive
feedback, busy-window exposure, seeded defect matrix and at least 30 pinned pairs /
100 fresh executions remain the adoption evidence in #16099. A faster narrow
contract or sum of overlapping spans cannot establish that result. Image split
#15214 stays held pending its own measurement; #16034 retains the earlier recurrence
follow-ups.
