# Docker runner recycling on the existing KVM pool

Initial measurements: 17 November 2026, 07:06–08:00 UTC. Retention was rejected under the then-active
2,000,000,000-byte limit. The explicitly authorized higher-cap follow-up below changes that boundary.
Required test routing and worker CPU/RAM remain unchanged.

The candidate uses the same real Claude/Codex VM scenarios as `x/vm_kvm:test_vm_kvm`, with fixture
persistence and Docker recycling enabled. Both targets request 4 CPU, 7 GiB and 50 GB; the outer
worker has 4 vCPU and 8 GiB RAM. See the [reproduction and disposal recipe](../x/vm_kvm/README.md).

## Higher-cap follow-up

[Policy #17263](https://github.com/agentydragon/ducktape/pull/17263) configured 10,000,000,000 bytes.
BuildBuddy v2.321.0 clamps this to the executor's advertised 7 GiB (7,516,192,768 bytes). One verified
idle worker was gracefully restarted to load it; the VM's PVC/PV identities were preserved. Other
already-running executor processes were not restarted. This is a cached-runner rejection threshold,
not additional RAM or an active-action limit.

All four full-suite actions used source `4c568861afb4a9f182d9f95cffb256def0466e6b`, with both unchanged
Claude/Codex scenarios, no retries or reduced selector:

| Action                                                                                        | Process / nested stack setup | Result                          |
| --------------------------------------------------------------------------------------------- | ---------------------------- | ------------------------------- |
| [Cold seed](https://app.buildbuddy.io/invocation/ea41003e-2642-4369-8332-ded351c7a8f1)        | 309.912 / 144.053 s          | Both cases and teardown passed. |
| [Warm reuse](https://app.buildbuddy.io/invocation/f15631c2-e335-4cf5-9aef-a6c44c99a8d4)       | 203.784 / 41.294 s           | Both cases and teardown passed. |
| [Ordinary control](https://app.buildbuddy.io/invocation/24cf860c-fa4d-4ce1-9e78-55e25968f456) | 238.325 / 104.739 s          | Both cases and teardown passed. |
| [Warm + discard](https://app.buildbuddy.io/invocation/26e92507-4b46-45a5-9bc3-8aa2188f6275)   | 188.629 / 42.300 s           | Both cases and teardown passed. |

Actual Docker identity, native BuildBuddy runner identity and the private previous-action receipt chain
established reuse. K3s process/start ticks, node UID and PKI/KubeVirt controller object UIDs stayed
stable; workload namespace, PostgreSQL Pod, root Certificate/Secret and collector namespace identities
changed on every reused action. The ordinary non-recycling control ran separately while this baseline
was paused; the final candidate still opened warm, then explicitly stopped and discarded its baseline.
Post-action inspection found no candidate container or private ext4 loop mapping.

Cold/first-warm memory after runner-scoped reclaim was 2.40/2.44 GB before pytest exited, with zero
reported cgroup OOM events. Worker MemAvailable was 5.51 GB after the first warm action; sparse samples
during ordinary coexistence included 3.62 and 2.09 GB, with no swap. These are not peak-memory bounds.
The ordinary control is faster than the first cold seed, so 310→204 s alone overstates the contrast
with ordinary execution. This small sequential sample qualifies reuse and one coexistence exposure;
it does not establish ordinary affinity, fleet capacity, reliability rates or required-gate savings.
Earlier guest-shutdown proof failures remain unresolved. Repeated loaded qualification is next.

The work disk uses `piraeus-ssd-local-delete`; both StorageClass and bound PV have `Delete` reclaim
policy, with VM→DataVolume→PVC ownership. Restart preserves the claim; normal VM deletion is configured
to remove it. Volume deletion was not destructively tested.

The [latency dashboard](https://agentydragon.github.io/ducktape/latency/#docker-recycling) owns the
reviewed evidence snapshot and retains the original trial receipts and clocks.

## Initial 2 GB exposures

Each row is one uncached, NEW TestRunner action pinned to the same executor registration
`96e89f34-7d67-4dcd-b85f-bc54ae327338`, native worker `a2104609-2a8e-429e-abb3-61d20e380c6c`.
Prebuilds were unpinned. Process and setup use worker-local REAPI and existing fixture-span clocks.
Server-to-worker queue differences were negative in later rows, so their queue durations are unknown.

| Action / source                                                                                                                                     | Process / stack setup | Result                                                                                                        |
| --------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------- | ------------------------------------------------------------------------------------------------------------- |
| [Private Docker overlay](https://app.buildbuddy.io/invocation/ab8b5954-f412-4e0a-9e5a-2e281c4ef5b5), `98125c38071d0cb0e8b8865e682e0045830de242`     | 193.21 / 180.43 s     | Nested containerd overlay mount failed before scenarios.                                                      |
| [Private ext4](https://app.buildbuddy.io/invocation/231b235a-7834-48d5-940e-5fbdae99ee4b), `6b61b33291f928d0c22fa8c9e6a5a88c984a82b0`               | 291.40 / 136.74 s     | Both cases and fixture cleanup passed; executor discarded runner at 4,478,640,128 bytes.                      |
| [Reclaim candidate](https://app.buildbuddy.io/invocation/48c3b6b9-b70d-478e-92de-3b8a4a871453), `f99d7c81ced18592f0c5272d3abfacf3dd3492b9`          | 303.15 / 133.65 s     | Both scenario bodies passed; teardown lacked guest shutdown proof. Reclaim never ran.                         |
| [Same-source retry](https://app.buildbuddy.io/invocation/d7b44f26-a734-43a2-8e4c-e76f84c065e0), `f99d7c81ced18592f0c5272d3abfacf3dd3492b9`          | 381.01 / 188.28 s     | Both scenario bodies passed; teardown lacked proof for both guests. Reclaim never ran.                        |
| [Ordinary target control](https://app.buildbuddy.io/invocation/56904718-2127-4183-8585-7a35dd92487a), `f99d7c81ced18592f0c5272d3abfacf3dd3492b9`    | 276.17 / 136.07 s     | Both cases passed.                                                                                            |
| [Claude-only diagnostic](https://app.buildbuddy.io/invocation/ec61b931-2cae-4b32-a45c-685ef2371662), `f99d7c81ced18592f0c5272d3abfacf3dd3492b9`     | See native execution  | Case and fixture cleanup passed; the probe rejected K3s's valid `/init` cgroup before reclaim. Action failed. |
| [Corrected Claude-only seed](https://app.buildbuddy.io/invocation/bdb0071f-bbb6-48b3-8e9a-c11c92fe7203), `dffed1b577ede39674dcd5d5c420d857436d6efe` | 264.75 / 140.94 s     | Case, cleanup and reclaim passed; executor again discarded the runner. Reduced coverage is diagnostic only.   |

## Initial memory result and decision

The corrected seed's existing cgroup receipt measured:

| Point                               |   Total bytes | Anonymous bytes |    File bytes |
| ----------------------------------- | ------------: | --------------: | ------------: |
| After clean fixture, before reclaim | 6,075,527,168 |   2,199,670,784 | 3,713,048,576 |
| After runner-scoped reclaim         | 2,278,490,112 |   2,199,617,536 |     9,768,960 |
| Executor's later pooling decision   | 2,628,542,464 |     Not sampled |   Not sampled |

The detailed samples were taken before pytest exited: anonymous memory alone then exceeded the
2 GB cap. The executor's later pooling sample has no anonymous/file breakdown. Its v2.321.0 Docker
accounting uses the cgroup-v1 `cache` field; whether correcting that accounting alone would fit the
runner after process exit remains unmeasured. `memory.reclaim` was scoped to this Docker runner with
`swappiness=0`; the worker has no swap, and the seed's cgroup OOM counters stayed zero. Reclaim reduced
cache but did not qualify pooling under the existing executor.

The initial recommendation was to avoid raising the fleet-wide cap based only on this seed: retained daemon RAM must coexist with the next
7-GiB-estimated action inside an 8-GiB worker. First measure the anonymous/file split at actual pooling
and test corrected cache accounting. Any capacity experiment then needs an explicit resident baseline
budget plus concurrent action evidence, or a smaller baseline. The higher-cap follow-up above
supersedes the unmeasured reuse/coexistence boundary; affinity and loaded fleet qualification remain open.

## Storage, teardown and cleanup boundaries

The normal worker-wide K3s bind mount can be reset by another action. The candidate therefore uses
a private sparse ext4 filesystem in the Docker container. Using Docker's overlay filesystem directly
failed; ext4 booted successfully. Bazel supplies filesystem tools through a declared Debian closure;
there is no runtime package download or worker-image repin. Logical sparse-file size is not allocated
disk usage; final worker free space was 49.18 GB.

The full candidate's shutdown failure occurred after both workspace storage unmounts: serial showed
systemd forking the proof helper, then ended without its entry or completion marker. Captured host
threads did not identify a blocked QEMU/virtiofs path, and no post-fork guest stack was captured.
Missing proof does not establish a FUSE, RCU, ext4 or reclaim cause. The control and reduced seed passed;
these small exposures do not establish reliability rates or causality.

Every candidate was removed after failure or the retention rejection. At 08:00:22 UTC the selected
worker had only its executor container, no baseline loop mapping, 7.54 GB available RAM and zero swap.
No retained candidate remained for a disposal action in that initial window. There was **no warm action then**, so this was a
bounded negative qualification, not a speed comparison with managed BuildBuddy executors.
