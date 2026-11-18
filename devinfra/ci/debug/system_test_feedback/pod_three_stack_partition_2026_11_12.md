# Pod three-stack partition: selected-graph trial

The manual partition passed the same selected 27 Pod, MCP Actions, and VM cases as
the unchanged control. The candidate selected Bazel graph completed in 357.487s,
still above the 240s objective. Its Pod path was no longer last; VM was. The
control took 580.533s and included a failed VM execution followed by a second
execution with the same action digest. This pair does not establish an
attributable end-to-end speedup or a total-cost saving. Do not add the manual
partition to required CI based on this trial.

## Partition and coverage

[The reproducible patch](pod_three_stack_partition.patch) adds three
`manual`-tagged targets and leaves the default `test_pod` target behavior intact.
All four targets use the same `test_pod.py`; only the manual targets select cases
with `-k`. Each target starts its own deployment, database, namespace and PKI.
The exclusive workspace-storage case runs first in open A. Open A and B contain
the cases that may create case-owned resources, with two slots per stack and no
retained runner pairs. The shared target uses only retained runner pairs: at
most two pairs occupy four Sandboxes;
its gateway TLS case adds one temporary impostor Sandbox, reaching the node's
five-Sandbox cap without an adjacent open case.

The split was balanced from a prior Pod trace's case durations, which projected
approximately 118s, 116s and 42s of case work for open A, open B and shared.
Those estimates were not observed timings; the shared target's gateway TLS case
subsequently took 88.1s and its case envelope reached 116.1s.

Uncached RBE collection [abd92daf](https://app.buildbuddy.io/invocation/abd92daf-8fb4-495b-875d-5f8a06cfe693)
selected 21 cases in the unchanged target and 5 + 9 + 7 in the manual targets.
The actual pytest node-ID multisets are equal and the three subsets are
disjoint. The storage case occurs once, only in open A. Collection ran no
fixtures. In the physical runs, exact JUnit case-name multisets also match:
Pod 21, MCP Actions 5 and VM 1, each once. All eight target results passed with
zero failures, errors and skips. Every returned `persistence-suite.json`
reported a warm retained K3s baseline, `cleanup: complete`, and distinct fresh
namespace, PostgreSQL Pod, root certificate, root Secret and collector namespace
UIDs across the three control and five candidate targets.

## Physical selected graph

Both commands used `bbr test ... --nocache_test_results --keep_going` and the
same local source tree, `c44a15e40c162fbb9d7760254020a6e78aeaca73` plus
the manual patch. The control selected `test_pod`, `mcp:test_actions`, and
`test_vm`; [invocation 02e68907](https://app.buildbuddy.io/invocation/02e68907-8fb7-46f3-9fc5-f8c7023c837b)
completed in 580.533s. The candidate selected open A, open B, shared, MCP
Actions and VM; [invocation 369fbd72](https://app.buildbuddy.io/invocation/369fbd72-f1af-418e-a110-cfc01851b8e8)
completed in 357.487s. The remote runners fetched different moving `devel`
bases (`8a8ce47a` and `60d97d69`) and applied cumulative patches back to the
local tree. The identical MCP Actions and VM action digests in both invocations
verify the exact same action inputs/platform for those targets; the four Pod
actions have distinct target-specific command/input roots. Direct CAS reads
found identical digests in all four Pod runfiles for `test_pod.py`,
`pod_cases.py`, `pod_runners.py`, `deployment/stack.py`, and
`driver/case_batch.py`; this verifies those common source files, not the
entire runfiles tree or remote checkout. The published patch differs from the
tested spike only in BUILD whitespace and a corrected comment about the
shared target's peak five-Sandbox demand.

| Returned action                          |  Queue |  Process |   Setup | Case envelope | Cleanup |       CPU | Requested CU-min |
| ---------------------------------------- | -----: | -------: | ------: | ------------: | ------: | --------: | ---------------: |
| Control Pod                              | 0.064s | 387.428s | 75.697s |      247.243s | 36.342s | 1175.310s |            25.83 |
| Candidate open A                         | 0.060s | 220.075s | 70.474s |      100.084s | 28.152s |  513.817s |            14.67 |
| Candidate open B                         | 0.062s | 255.994s | 76.586s |      116.899s | 39.585s |  791.464s |            17.07 |
| Candidate shared                         | 0.065s | 256.097s | 81.122s |      116.114s | 29.057s |  671.425s |            17.07 |
| Control MCP Actions                      | 0.059s | 271.495s |         |               |         |    814.3s |            18.10 |
| Candidate MCP Actions                    | 0.060s | 260.660s |         |               |         |    751.1s |            17.38 |
| Control VM first, infrastructure failure | 0.011s | 217.519s |         |               |         |    862.6s |            29.00 |
| Control VM replacement, passed           | 0.068s | 321.878s |         |               |         |   1208.2s |            42.92 |
| Candidate VM, passed                     | 0.065s | 313.991s |         |               |         |   1160.3s |            41.87 |

The setup/case/cleanup spans are measured inside the Pod TestRunner action and
do not add exactly to its whole process span. Case envelope is the first case
start through the last case end, not the sum of overlapping cases. Queue is the
returned action's queued-to-worker-start interval, and excludes Bazel
prerequisites. The first control action queued 37.864s after its Bazel command;
the first candidate action queued 42.450s after its Bazel command. Each Pod
action requested four compute units. The candidate's three Pod actions summed
to 48.81 requested execution CU-min and 1976.706 observed CPU-seconds, versus
25.83 CU-min and 1175.310 CPU-seconds for the single control Pod action.
Requested CU-min is not a billing measurement.

The control VM's first execution ended with `error pausing VM` and a Firecracker
socket `connect: connection refused`. A second execution ID with the same
action digest completed successfully. The returned evidence does not identify
whether Bazel or BuildBuddy initiated that retry. Both original operations are
retained below. The failed attempt adds 217.519s process time, so the 580.533s
control wall time cannot serve as a clean comparison with the candidate. The
candidate's VM action was last at 313.991s process; it remains a binding path
after the Pod partition. The Pod case-phase reduction is descriptive for this
warm pair, not a causal or steady-state estimate: the five inspected Pod source
files and the complete MCP/VM action digests match, but worker placement,
neighboring load, and time were not held fixed. The separate VM
placement experiment and report visual checks overlapped these windows.

Exact operation IDs, for read-only response recovery without resubmission:

```text
control MCP: /uploads/2034711f-2829-4ea1-8a6a-bc3d5269dceb/blobs/6054bb8b2231fa61b92d4f99cce77d0193c48f0bffb7bc44a55ae9681995f79f/1218
control Pod: /uploads/9e25bf68-c120-435d-9b0a-f0c1184c1e0e/blobs/af887689cca7f501453759e0a6e7921f8c76c4d1e57e64bbc83d1b4a03e199bc/1218
control VM failed: /uploads/349a138a-83f3-4882-947a-7eb2771d5a0b/blobs/d4e4b6830625f6359deeda38d59154550f964fe28db3a48c308a3031bf73d8a0/1852
control VM replacement: /uploads/9d84ddb9-20c0-4809-9805-ec9342b15253/blobs/d4e4b6830625f6359deeda38d59154550f964fe28db3a48c308a3031bf73d8a0/1852
candidate open A: /uploads/36049434-fdbe-4943-bcb1-a8d85c369be2/blobs/5684d82a744175f9f7adef79a9b1b4523b4879358b306d34aee747a302c41ae7/1218
candidate open B: /uploads/1703a3b2-9dcf-4163-bc47-acab1b6c9d39/blobs/cd9b635f2b7566899e8f3fd6920f4bcf70916324af96fdd0847345c346096681/1218
candidate shared: /uploads/4089a885-b734-4733-80cb-35fe42ce18a3/blobs/f9d0b7e8192ca655ac0440b71b8a4bf943121beb19e00926dc01afdd4fa36900/1218
candidate MCP: /uploads/77c0ebb6-70fc-4cb3-bcd6-67d634e648ed/blobs/6054bb8b2231fa61b92d4f99cce77d0193c48f0bffb7bc44a55ae9681995f79f/1218
candidate VM: /uploads/ec4ec77f-bb80-4e2a-bc48-45b9e316a3a2/blobs/d4e4b6830625f6359deeda38d59154550f964fe28db3a48c308a3031bf73d8a0/1852
```

The trial shows one coverage-preserving partition that passed within the
five-Sandbox limit in this run,
but it duplicates stack setup and increases Pod requested compute. VM and MCP
remain close to or beyond the four-minute budget even with faster Pod work.
Before changing required CI, test a fixture/partition design that reduces
duplicated setup and measure all required paths under comparable admission and
load conditions.
