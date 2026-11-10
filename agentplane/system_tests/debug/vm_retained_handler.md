# Retained KubeVirt node eligibility, 2026-11-02

The VM system-test fixture reused KubeVirt's Available condition after restoring its
infrastructure snapshot. That condition does not mean its node is eligible for a
launcher Pod. A restored handler's heartbeat can be old in wall time while its
periodic timer still has time left to run. The node controller then revokes
eligibility, and new launcher Pods wait for the handler's next heartbeat.

## Observed boundary

The observer-scoping trial
[`a9603fff-8663-4dae-8cb5-1014ae9d250c`](https://app.buildbuddy.io/invocation/a9603fff-8663-4dae-8cb5-1014ae9d250c)
passed the VM target. Guest Runtime became active at 36-38 seconds on each guest's
native monotonic clock, but the external readiness watches lasted 143-163 seconds.
The latter spans start after `create_sandbox` responds. These clocks and spans
cannot be added together to produce a host launch duration.

The retained `storage/{before-import,final}/resources.yaml` snapshots and Events
locate a common gate before guest launch:

| Event                                                                  | Host UTC    |
| ---------------------------------------------------------------------- | ----------- |
| Last initial handler heartbeat                                         | 14:55:05    |
| NodeUnresponsive: handler not responsive, marking node unresponsive    | 15:00:17    |
| VM, VMI and launcher Pod creation                                      | 15:01:24-25 |
| FailedScheduling: node affinity/selector mismatch, all three launchers | 15:01:25    |
| State PVC provisioning starts                                          | 15:02:18    |
| State PVC provisioned and launcher Pods scheduled                      | 15:02:22-23 |

The initial Node has `kubevirt.io/schedulable=false`, and the final Node has `true`.
KubeVirt requires that label to be `true` on launcher Pods. This is a sufficient
failed selector term consistent with the Events. The endpoint labels otherwise
match; transient CPU-label changes between those snapshots are not excluded.

Creation-accepted/readiness-watch start to PodScheduled was 58-59 seconds for all
three guests, versus 4-6 seconds in control
[`89ec30fd-5594-40a6-8a53-3f165d1b6967`](https://app.buildbuddy.io/invocation/89ec30fd-5594-40a6-8a53-3f165d1b6967).
WorkloadReady to EnvironmentRuntimeConnected remained similar: 52/60/68 seconds
in the observer arm versus 52/61/72 seconds in control. The first host CRI receipt
from the guest bootstrap log to connection was also similar (8-14 seconds).
CRI receipt can be buffered; it is not the guest Runtime's exact activation time.
This comparison locates the extra host delay before scheduling rather than at the
post-boot connection boundary. It is not a treatment estimate.

## Pinned producer

The repository pins KubeVirt v1.8.2. Its upstream tag resolves to
`3203e9d1ce77af32f6a9ce72b9f954830666f72c`:

- [Launcher selector](https://github.com/kubevirt/kubevirt/blob/3203e9d1ce77af32f6a9ce72b9f954830666f72c/pkg/virt-controller/services/nodeselectorrenderer.go)
  requires `kubevirt.io/schedulable=true`.
- [Node controller](https://github.com/kubevirt/kubevirt/blob/3203e9d1ce77af32f6a9ce72b9f954830666f72c/pkg/virt-controller/watch/node/node.go)
  considers a heartbeat older than five minutes unresponsive, emits the observed
  Event, and writes `schedulable=false`.
- [Handler heartbeat](https://github.com/kubevirt/kubevirt/blob/3203e9d1ce77af32f6a9ce72b9f954830666f72c/pkg/virt-handler/heartbeat/heartbeat.go)
  checks devices immediately on process startup, waits for device plugins, and
  then uses a sliding 60-132 second jittered timer. It owns the heartbeat and
  eligibility writes. Heartbeat-send logs are V4 and absent from retained logs;
  this evidence does not give every individual heartbeat-write timestamp.

## Bounded experiment: no implementation selected

The proposed remedy was to refresh the managed handler Pod when attaching a clean
retained VM baseline, before applying fixture workloads. The prospective helper
rejected any VM/VMI, launcher or fixture Pod, proved the Pod's controller-owner UID
matched the retained DaemonSet, and deleted only that Pod with a UID precondition.
It required a Ready replacement plus producer-written eligibility and a changed
heartbeat. The operator's DaemonSet configuration and stale-node policy remained
unchanged; there was no manual label/heartbeat override or resource change.
This helper was not published: the runtime trial did not exercise it.

Source for the comparison is `3363b5f75e7f019ca030d86cd4dcc97c4c87fbca` plus the
same helper patch in both arms. A diagnostic environment switch chooses refresh
on/off without changing the baseline fingerprint; the entire runtime patch and
switch were removed before this documentation-only publication. This source predates the observer opt-in PR, so both
comparison arms run the observer on all three guests. Any future comparison must
include restart and eligibility-wait cost in setup; scenario readiness alone does
not measure a net saving.

The seed
[`39d91778-cf41-4857-ba45-911292fcc4cb`](https://app.buildbuddy.io/invocation/39d91778-cf41-4857-ba45-911292fcc4cb)
passed with a cold baseline: action 368.488 seconds, setup 114.658 seconds, case
191.692 seconds, complete cleanup and three workspace loops released. Its final
captured heartbeat was 15:37:33 UTC; action execution ended 15:38:24 UTC.
The control and candidate were submitted at 15:44:00 UTC to exercise natural stale
restore without adjusting clocks or node labels:

- Control: [`ffa5229b-bb47-4201-9178-e9f7972552b7`](https://app.buildbuddy.io/invocation/ffa5229b-bb47-4201-9178-e9f7972552b7).
- Candidate: [`5cdf4230-56bc-4cbe-b4ae-e199cf5e09b0`](https://app.buildbuddy.io/invocation/5cdf4230-56bc-4cbe-b4ae-e199cf5e09b0).
- Safety tests: [`556c9672-0507-4fc6-94fc-aabefa12ed63`](https://app.buildbuddy.io/invocation/556c9672-0507-4fc6-94fc-aabefa12ed63), passed.

The first control dispatch failed before the Run RPC with a transport authentication
handshake deadline. It had no registered inner invocation or accepted runner ID;
status lookup returned no invocation. One localized retry was submitted at about
15:47 UTC:
[`b60e8735-0ff4-491e-be29-887156b17e15`](https://app.buildbuddy.io/invocation/b60e8735-0ff4-491e-be29-887156b17e15).
Its first physical TestRunner failed with provider code 14, `executor OOM killer
terminated task`, at 15:48:34.735059 UTC. BuildBuddy then retried it internally.
The failed dispatch and physical attempt remain failures in the evidence.

The candidate used worker `2fc1987a-6294-4468-bf91-f26cee5ceeb8` and rebuilt cold.
It therefore **never ran the refresh helper**: there is no refresh span or artifact.
Its fingerprint matched the seed
(`34287fd491044cf35bb5a6530ef20b7c451d66be4146707b6f5d6d465369c6a1`), but
its new node UID was `210eb7bc-7e14-4659-a42a-033b40ebb5c0`. Before import the
new node had neither KubeVirt heartbeat nor eligibility label; its final snapshot
had heartbeat 15:50:24 UTC and eligibility `true`. Those are cold installation
observations, not stale-node recovery.

The single candidate physical action took 415.853 seconds: setup 116.955 seconds,
case 249.198 seconds. Guest Runtime activation was 41-45 native monotonic seconds,
and external readiness took 112-132 seconds. Scenario assertions completed,
including actual OOM containment (`oom_kill` 0 to 1, unchanged cgroup/Runtime boot,
follow-up exit 0), but target teardown raised
`GuestBootEvidenceError: Guest boot evidence report did not complete` for the OOM
guest. The persistence receipt lacks completed-cleanup and loop-release fields;
this run is not full target/cleanup proof.

The prospective helper's safety and library checks passed
([initial](https://app.buildbuddy.io/invocation/556c9672-0507-4fc6-94fc-aabefa12ed63),
[final](https://app.buildbuddy.io/invocation/bb60ed89-abd6-41fa-8d00-7f11ad045065)),
including live-workload refusal, controller ownership, UID precondition and a
replacement-ready/unchanged-heartbeat retry. Those checks do not validate runtime
recovery or latency. No helper, tests, BUILD changes or README policy were shipped.

The control's second physical action completed at 15:57:45.365397 UTC, taking
547.062 seconds on worker `433fa22c-cadd-456c-821d-71c7fdd13acc` (first failed
physical attempt: 68.207 seconds). It also rebuilt cold with the seed's fingerprint,
and a new node UID `f843395b-a668-4cf2-9a98-8f3b71f39417`. Setup took 149.028
seconds, case 322.659 seconds, and guest Runtime activation 58-62 native monotonic
seconds; external readiness was 121-148 seconds. Initial heartbeat/eligibility were
absent; the final heartbeat was 15:56:29 UTC with eligibility `true`.

The control case's OOM assertions and follow-up passed, but target teardown failed
with `GuestBootEvidenceError: Guest boot evidence report was partial`. Its clean
receipt fields were also absent. Both trial arms therefore rebuilt cold and failed
boot-evidence teardown; neither is a valid stale-node treatment comparison or full
coverage/cleanup pass. The control TestRunner action digest
`a0cff67a9c5ad103b4e40cc5f2e1fb501ec74476e6b20dc166817590f32d2236/1891`
exactly matches the seed action, confirming its runtime inputs were unchanged.

The diagnostic helper/switch patch was applied only to the experiment source.
The commands used the repository Nix devshell and ordinary RBE/cache configuration:

```bash
# Cold seed, including changed-library lint/type checks:
bbr test //agentplane/system_tests/suites:test_vm \
  //agentplane/system_tests/vm:sandboxes --nocache_test_results \
  --test_output=errors --test_env=AGENTPLANE_TEST_REFRESH_HANDLER=0
# Control and candidate, respectively; original VM coverage in both:
bbr test //agentplane/system_tests/suites:test_vm --nocache_test_results \
  --test_output=errors --test_env=AGENTPLANE_TEST_REFRESH_HANDLER=0
bbr test //agentplane/system_tests/suites:test_vm --nocache_test_results \
  --test_output=errors --test_env=AGENTPLANE_TEST_REFRESH_HANDLER=1
bbapi artifact list INVOCATION --kind test --json
bbapi execution INVOCATION --json
journalctl --file=SYSTEM_JOURNAL --no-pager --output=short-monotonic
```

## Remaining experiment

No latency improvement was established. Both arms rebuilt cold, invalidating the
planned stale-warm comparison; shared snapshot-family activity and worker selection
were uncontrolled. No further VM experiments were started after this bounded trial. The sufficient failed selector and its
producer are identified, but the remedy is untested. A future trial needs an
isolated retained snapshot family with confirmed identical warm fingerprints and
aged heartbeats in both arms, then a full-cost comparison of managed handler
replacement against its periodic heartbeat. Include replacement readiness probes,
setup, scenario, shutdown/cleanup, and all provider attempts. Do not infer fleet or
required-gate savings from the event chronology or the cold candidate.
