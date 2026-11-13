# Pod contract ownership and projection cadence

Tracker: [#16099](https://github.com/agentydragon/ducktape/issues/16099).
Source PR: [#16102](https://github.com/agentydragon/ducktape/pull/16102), same
`agent/test-architecture-pivot` branch. These are experimental changes; the routine
feedback objective remains unmet. No required coverage was removed.

## What changed

- Four protocol/app cases exercise their already-reserved, independent Claude and
  Codex Sandboxes concurrently. Their Sessions, command IDs and scripted model
  queues remain distinct. TaskGroup failure cancels and awaits the peer; the pool
  destroys a failed pair. The two-pair/four-Pod pool capacity is unchanged.
- Pod scenarios have a separate fixture/image composition. MCP edge is omitted at
  construction and from the image map; Actions stays because command-box creation
  exercises it. Dispatcher, probe, default, MCP, VM, Firecracker and real-model
  profiles retain their previous service compositions. The Pod app still configures
  unused Connection routes; those routes are unavailable and aggregate status
  reports Connection down. This profile does not establish external connections.
- The existing native Gateway/Dispatcher/History composition lives in
  `agentplane/testing/native_service_fixtures.py`. The pilot imports its fixtures
  explicitly, keeps its target/manual tag and Claude selection, and preserves all
  seven fixture function ASTs, settings and lifetimes. This is reusable ownership
  groundwork, not an expanded coverage matrix or a measured speed improvement.
- The live-policy scenario now checks that its Pod UID did not change. A restart
  cannot satisfy its advertised live-grant contract.
- An isolated-node spike uses kubelet `sync-frequency=5s`. Actual configz artifacts
  confirm it; production node settings and the controller's real >=60-second idle
  timer are unchanged. The retained fingerprint hashes `baseline.py`, so a node
  with the previous startup settings rebuilds. This tests real Secret projection
  and Runtime certificate reload under a shorter cadence, not exposure to the
  production-default one-minute schedule.

## Coverage boundaries

Keep real deployment acceptance for app-facade operator resume, Identity lifecycle
denial, the actual idle timer, Pod replacement, cert-manager/WorkloadCredential
binding, same-Pod Secret projection, storage and exact retained History. The native
pilot authors Pod/Sandbox status in envtest and cannot replace those witnesses.

The four slow fresh-Sandbox cases retain their original execution shape. Idle
resume is already one Codex scenario. Messaging, live policy grant and mTLS
replacement still exercise both harnesses sequentially. The replacing mTLS case
does not belong in the pool whose borrow invariant requires unchanged Pod UIDs.

Existing focused owner coverage includes injected-clock idle reconciliation,
lifecycle authorization, credential rotation, native Notification Service message
lifecycle and live proxy revocation. Adding another copy of a service predicate
does not establish the real runner CLI, app or controller wiring.

## Full-suite screen

All three invocations ran identical 21 JUnit case IDs, without skips, with one NEW
uncached attempt and complete cleanup. The inspected devel merge parent was
`1e03f9e4fcc110462b11eae4cc3fc68c9ec69046`.

| Variant / literal source                                    | Bazel wall | TestRunner process |   Setup |       CPU | Baseline |
| ----------------------------------------------------------- | ---------: | -----------------: | ------: | --------: | -------- |
| Control `6369d22eff7f714125cf3bc0c9299a911df0cdcd`          |   463.767s |           419.229s | 96.796s | 1021.665s | cold     |
| Concurrency `defe54fa1270013bc30cb948b109d35e36209c56`      |   511.511s |           472.226s | 71.728s | 1302.442s | warm     |
| Plus Pod profile `6fc4da6af4520668a9d97a7d1a8206848679529e` |   410.262s |           378.331s | 86.374s | 1223.896s | warm     |

Invocations: [control](https://app.buildbuddy.io/invocation/9c7b1900-20dd-4dcc-a9e8-0e9929d929aa),
[concurrency](https://app.buildbuddy.io/invocation/1ab3660f-d81d-4d84-8a50-7bb56b65675c),
[combined](https://app.buildbuddy.io/invocation/051935c8-10f5-4b27-9503-36f65e551a3a).
The combined invocation also passed 15 rendering contracts and one deterministic
render test. Configured `somepath(test_pod, mcp_edge:service_image)` returned a
three-node image-manifest path in
[control](https://app.buildbuddy.io/invocation/68639d1e-2e17-482b-8f83-868377ba851a)
and empty results in
[candidate](https://app.buildbuddy.io/invocation/9966f8ea-8fc2-4c78-a5c6-2dc10ee9a631).
That proves dependency removal, not transferred bytes.

These overlapping launches used different cache/load exposure. Both candidates
reported the same worker identity; control used another worker. The unchanged
wrong-issuer-to-valid mounted Secret case took 15.494/108.807/13.423s respectively.
Its tail prevents attributing a whole-suite improvement to concurrency or image
closure. Physical process, setup, Bazel wall and CPU are separate clocks.

The initial control launcher invocation
`fbf9106a-3aae-4852-888c-9bf0bba260e6` rejected misplaced remote flags before any
test executed. It is retained as a launcher error, not a physical test failure.

## Same-Pod Secret cadence screen

Run only the existing wrong-issuer/valid-recovery case three times per arm. Source
control is `6fc4da6af4520668a9d97a7d1a8206848679529e`; candidate is
`33cc6df6c70f9e83e95a9959a0ae8aefd093610b`. Match run numbers through actual CAS
Command `TEST_RUN_NUMBER`, not timestamp guesses. Every pair reports the same
worker identity. All six cases passed with complete cleanup.

| Run | Default case | 5s case | Default process / CPU |   5s process / CPU |
| --- | -----------: | ------: | --------------------: | -----------------: |
| 1   |      29.341s | 22.723s |    238.201 / 699.088s | 239.370 / 675.441s |
| 2   |      91.048s | 26.058s |    272.873 / 593.268s | 211.889 / 576.390s |
| 3   |      22.291s | 29.050s |    162.719 / 406.244s | 164.549 / 394.502s |

Invocations: [default](https://app.buildbuddy.io/invocation/09eea6bb-8837-4aec-8105-a9b7a6344205),
[5s](https://app.buildbuddy.io/invocation/dfbc6e9d-bb6d-4321-aaa1-849f3dd160f2).
All six configz records confirm Watch and 1m0s versus 5s. Controls attached warm
baselines; changed startup source forced candidate cold rebuilds. Arms started in
default-then-candidate order with overlap and ordinary CI in flight. Aggregate CPU
1698.600s versus 1646.333s includes setup/cleanup; it does not isolate kubelet CPU.
Three runs per arm are a promising tail screen, not reliability or p90 evidence.

[Kubernetes documents](https://kubernetes.io/docs/concepts/configuration/secret/)
that Secret-volume update latency includes kubelet sync plus cache propagation.
The observed tail is consistent with that mechanism; this sample does not prove
all of its 91/109-second durations were volume propagation. The earlier Pod
annotation intervention remains a scoped negative result, not a substitute for
this changed node configuration.

## Native extraction controls and follow-ups

The extracted fixture's [healthy control](https://app.buildbuddy.io/invocation/d12a1122-8586-4d0b-976a-9c16b26fc0eb)
passed, alongside retained-node compatibility tests. The
[discarded-journal control](https://app.buildbuddy.io/invocation/addede73-ff24-4675-b7fe-f121dfbf0dbc)
failed at the retained-Session assertion: the restarted Runtime's Session set was
empty. [Explicit changed-library builds](https://app.buildbuddy.io/invocation/3b553799-eab5-4fd1-b724-c54844db7c77)
passed with their configured type/lint aspects. The first extracted-fixture
attempt failed Bazel visibility before testing; its narrow consumer grant was
updated and the failed invocation `5e1ac62d-1498-487b-96ab-fabeeb7c7f65` is retained.

Track in #16099: counterbalanced warm/cold and busy-window cadence/concurrency
exposure, CPU and all failure signatures; exact-head required-check feedback and
its synthetic merge provenance; native active-work/History-gap/fencing witnesses
with seeded defects; then the planned 30-pair/100-execution candidate-graph replay.
Preserve real deployment witnesses until coverage equivalence is demonstrated.
The recurrence follow-ups in #16034 and held image split #15214 remain separate.
