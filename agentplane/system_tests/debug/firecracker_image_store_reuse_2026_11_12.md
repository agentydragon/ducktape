# Firecracker image store across separate KVM test actions

The Firecracker system test reused containerd's image store across separate BuildBuddy
`TestRunner` executions on the same KVM worker. The first warm run's final image inventory
is byte-for-byte identical to the second warm run's **before-import** inventory. This
establishes store continuity across the action boundary, not a measured runtime saving.

## Executions and physical evidence

All three invocations passed `//agentplane/system_tests/suites:test_firecracker`
on 2026-11-12 UTC. They ran on worker `d81a9661-db05-446a-a80d-bb4a4ba17367`.

| Invocation                                                                                   | Source commit                              | Test duration |         Before-import inventory | Before-import image filesystem used |
| -------------------------------------------------------------------------------------------- | ------------------------------------------ | ------------: | ------------------------------: | ----------------------------------: |
| [Initial run](https://app.buildbuddy.io/invocation/aac05cff-53ec-5084-b281-e8c3767f8277)     | `e7a5f96ad79789b3067fe5151f78ecc960e7f427` |     229.196 s |            Header only (1 line) |                                 0 B |
| [First warm run](https://app.buildbuddy.io/invocation/1d0825b0-0947-4a8e-8f0d-5fcb70630e19)  | `dddecd23e0bfee7c0cb360120d7e2e8cc329f9a6` |     216.274 s | 52 listed references (53 lines) |                     2,400,043,008 B |
| [Second warm run](https://app.buildbuddy.io/invocation/d74fd16f-b026-4775-8951-89e55cf3efbc) | `630fd860990a6482d365d9581941a2bdfc2de36b` |     213.310 s | 52 listed references (53 lines) |                     2,400,043,008 B |

The two warm runs have the same TestRunner action digest
`9a9634d706bc262dc9ffcc6b2f84d1bb0664df582e665805f9de5a1b9c537735/595`,
although their invocation source commits differ. Their execution IDs differ:
`/uploads/0272be57-a555-4415-be2a-d15a338a4d3c/blobs/9a9634d706bc262dc9ffcc6b2f84d1bb0664df582e665805f9de5a1b9c537735/595`
and
`/uploads/8ba4f487-9ba6-4a44-89ca-d7fddd98473b/blobs/9a9634d706bc262dc9ffcc6b2f84d1bb0664df582e665805f9de5a1b9c537735/595`.
Both executions have `doNotCache: true`; this was not a reused test result.

The first warm run's `test.outputs/storage/final/images.txt` and the second warm run's
`test.outputs/storage/before-import/images.txt` have the same SHA-256:
`e3b820811ed1b43798eb1514ab68ebe90acbc259990ede167d0615ed8607f065`.
The inventory includes named Agentplane application and Firecracker references,
cert-manager references, K3s system references, and digest aliases; its 52 rows
are not 52 unique images. All three before-import storage summaries report node
start time `2026-11-12T10:12:03Z`.

## Mechanism and safety boundary

The `kvm-vm` executor [bind-mounts](../../../devinfra/buildbuddy/kvm/worker/executor.yaml)
`/buildbuddy/k3s` into each Docker action at `/var/lib/k3s-env`; the test platform
sets `test.recycle-runner=false`. Stack setup stops any owned previous server and
starts a new K3s control plane. It retains the containerd store only after a clean
prior stop, with the same K3s binary digest and at least 20 GiB free. Start marks
the store dirty; stop marks it clean only after containerd cleanup. A rejected store
loses metadata and layers together. The merged
[retained-store acceptance test](../k3s/test_retained_store.py) covers reuse without
reimport, a new control plane, reset after SIGKILL, and reset on binary mismatch.
This review found no concrete defect in those paths.

The initial run used a different TestRunner action digest and source commit; its
empty pre-import inventory is a useful cold observation, not a paired control.
The logs show stack setup of 76.30 / 70.47 / 67.66 s and 20 image-import spans
totaling about 36.39 / 27.39 / 26.50 s for initial / first warm / second warm.
Those inclusive spans and three test durations do not isolate the effect of
retention from source changes, other work on the worker, or run-to-run variation.
The evidence establishes same-worker reuse for these executions, not reuse after
worker replacement or a fleet-wide performance estimate.

Reproduce the provenance and inventory comparison with `bbapi invocation <id> --json`,
`bbapi execution <id> --json`, and `bbapi artifact cat <id>
test.outputs/storage/{final,before-import}/images.txt`; compare the first warm
final artifact with the second warm before-import artifact using `cmp` or `sha256sum`.
