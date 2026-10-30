# Overlapping image imports with stack setup

The cold six-VM CI action in
[`9379a1c3`](https://app.buildbuddy.io/invocation/9379a1c3-f18d-592f-af46-d446b4209cf1)
took 500.0 seconds, including 239.4 seconds to stack readiness. Its existing
`k3s-timeline.jsonl` records 174.6 seconds of image imports. Those imports are
serial, and three apply/readiness intervals also block further imports:

| Main-thread work                           | Next images      | Existing interval |
| ------------------------------------------ | ---------------- | ----------------: |
| Apply KubeVirt and Kyverno                 | PKI              |             7.1 s |
| Install PKI and probe admission            | Fixture services |            19.0 s |
| Apply CRDs/manifests and wait for issuance | VM guests        |             5.0 s |

The 31.1-second sum is an optimistic overlap budget, not a measured speedup.
Imports and rolling Pods still contend for CPU and disk. Service rollout already
overlaps the roughly 50-second guest import; preserving that overlap matters.

The prototype uses one standard `ThreadPoolExecutor` during startup. It submits
only the next group, waits for completion before applying that group's workloads,
and exits the executor before k3s cleanup. No later group is queued if setup fails.
A running import is joined, not cancelled; this does not add a subprocess timeout.
The importer still queries containerd and streams only missing blobs one image at
a time. A write lock protects the existing timeline's JSONL records now that setup
and imports can write concurrently.

The 2026-10-30 experiment compares the unchanged serial setup with this pipeline
at base `ee31a726d3a307fd1cf984aa4a44851e019b1359`, with the same six independent guests, assertions, disk-backed
layers and 8-unit worker request. A temporary experiment-only switch removes the
disposable containerd image store after the previous stack is reset and before
k3s starts, and rejects tmpfs layers. This makes each observation cold regardless
of worker recycling; the switch is removed from the final implementation. Test
results remain uncached. Existing Bazel results, timeline and guest artifacts
supply timings and coverage evidence; no new timing instrumentation is added.

The runs executed sequentially, serial/pipeline/pipeline/serial. The second
pair sets `PYTHONHASHSEED=0` in both arms; its recorded import order matches
exactly. Each run used `bbr test //agentplane/system_tests/suites:test_vm`
with uncached test results and the temporary cold-store switch.

The first pair used the default hash seed:

| Arm      | Invocation                                                                            |  Action | Stack ready | Scenario case | Imports |      CPU | Peak memory |
| -------- | ------------------------------------------------------------------------------------- | ------: | ----------: | ------------: | ------: | -------: | ----------: |
| Serial   | [8e72ab32](https://app.buildbuddy.io/invocation/8e72ab32-ea29-4e41-8da3-b584be3de823) | 469.5 s |     223.2 s |       217.5 s | 169.5 s | 2039.2 s |    8.25 GiB |
| Pipeline | [5ad85022](https://app.buildbuddy.io/invocation/5ad85022-8e8f-4149-8f2c-fd17808d153b) | 425.4 s |     177.1 s |       214.9 s | 154.6 s | 2010.2 s |    8.50 GiB |

Both used worker `dc5ded7a-8fd6-4105-a80d-846ba18a501a`, with six distinct
guests, peak six live guests and six shutdown receipts. Registry pulls were
limited to k3s's existing CoreDNS, local-path provisioner and BusyBox helper
images; no fixture, PKI or KubeVirt/Kyverno image pulled unexpectedly.

The action improved by 44.1 seconds (9.4%), and stack readiness by 46.1 seconds.
Import work itself was 14.9 seconds faster. Image order varied because the
infrastructure and PKI groups iterate sets, so the entire observed improvement
cannot be attributed to overlap. The pipeline trace shows imports during each
of the three intended setup intervals. The concurrent scenario case was only
2.6 seconds faster; no scenario implementation changed.

The fixed-order pair, with the pipeline run first:

| Arm      | Invocation                                                                            |  Action | Stack ready | Scenario case | Imports |      CPU | Peak memory |
| -------- | ------------------------------------------------------------------------------------- | ------: | ----------: | ------------: | ------: | -------: | ----------: |
| Pipeline | [3d3c49d4](https://app.buildbuddy.io/invocation/3d3c49d4-cf01-4bb6-9039-dfd575c3ee2e) | 408.4 s |     166.2 s |       198.7 s | 142.6 s | 1842.2 s |    8.32 GiB |
| Serial   | [534cba8e](https://app.buildbuddy.io/invocation/534cba8e-2ed9-4b12-a77b-85603f0aaa26) | 424.6 s |     181.9 s |       196.2 s | 136.4 s | 1878.5 s |    8.51 GiB |

The matched pair improves the action by **16.2 seconds (3.8%)** and stack
readiness by **15.7 seconds (8.6%)**. Imports take 6.2 seconds longer in the
pipeline, and its scenario case takes 2.5 seconds longer. The startup gain
therefore survives those costs rather than simply borrowing a faster scenario.
The serial trace has 23.6 seconds of apply/readiness intervals between imports;
contention and other run variation reduce the net improvement.

These runs used the same worker as the first pair and again retained six
distinct guests, peak six live guests and six shutdown receipts. All four runs
passed on their first attempt; each has one successful TestRunner execution
and no unexpected fixture or infrastructure pulls. Queueing was 0.3–2.3 seconds.
The two-observation-per-arm action medians are 447.0 seconds serial and 416.9
seconds pipeline, but the fixed-order pair is the more conservative result.

The measured improvement supports retaining the pipeline. Batching imports,
deferring other images and profiling guest startup remain separate experiments.

Removing the image store does not flush OS page caches or control executor
host load, so even matched cold-store observations are not isolated hardware
benchmarks. Full PR CI time, including queueing, builds and other tests, must
be measured separately; this does not establish a 30% whole-CI improvement.
