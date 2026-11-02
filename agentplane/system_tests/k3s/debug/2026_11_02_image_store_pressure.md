# Retained image-store pressure

## Failure and mechanism

[PR #13887's invocation](https://app.buildbuddy.io/invocation/22bfcd85-9000-5990-86b2-711205f6af19)
failed all four MCP suites and Dispatcher during cert-manager rollout. In Actions, the retained
baseline rebuilt at 13:00:40 UTC; kubelet evicted the webhook at 13:00:56 and rejected replacement
Pods under `DiskPressure`. Image GC removed 23 historical images at 13:02:50. The original artifacts
do not contain the eviction-signal metric or filesystem capacities.

The [instrumented Actions rerun](https://app.buildbuddy.io/invocation/0dc77286-0ebf-4887-ac60-ac192843df48)
passed. Root/nodefs had 47.5 GB free of 55.0 GB; the overlay snapshotter tmpfs/imagefs held
4.03 GB of 5.10 GB before imports. A separate
[retained inventory](https://app.buildbuddy.io/invocation/92c84fbf-1d27-4935-a3a2-d62a1951e413)
had 67 containerd image references, including obsolete config-ID aliases without named references.
That healthy rerun cannot identify the historical eviction signal by itself.

Containerd's CRI plugin creates a managed `sha256:<config digest>` alias for an imported image.
Replacing the fixture's stable `:unset` tag moves its pin to the new version but leaves the old
config-ID alias and unpacked layers. This is visible in the native image inventory and follows
[containerd's import update path](https://github.com/containerd/containerd/blob/v2.2.7/internal/cri/server/images/image_pull.go#L386-L440).
Imagefs is the snapshotter tmpfs, not the disk requested with `EstimatedFreeDiskBytes`.
The kubelet's effective settings were 85%/80% image-GC thresholds, a two-minute minimum image age,
5% imagefs/nodefs available eviction thresholds, and 10% minimum reclaim. Rebuilding the kubelet
resets its observation age for restored images, leaving a window where pressure can evict Pods
before the old images qualify for GC.

## Controlled reproduction

Two isolated RBE actions imported three versions of one pinned fixture tag. Each version had a
distinct 190 MiB zero-filled layer; compressed OCI inputs remained small. The tmpfs capacity was
the native baseline's used bytes plus the three layers plus 24 MiB. Both actions started a normal
pause Pod before the imports. Root/nodefs retained about 50 GB free. No production executor pool
or eviction threshold was changed.

| Treatment                                                                                     | Imagefs used / capacity after imports   | Obsolete ID aliases | Native outcome                                                                                       |
| --------------------------------------------------------------------------------------------- | --------------------------------------- | ------------------- | ---------------------------------------------------------------------------------------------------- |
| [Cleanup disabled](https://app.buildbuddy.io/invocation/fb9365e7-9f61-4fba-abcc-e1a054cf88a8) | 732,753,920 / 757,899,264 bytes (96.7%) | 2                   | `DiskPressure=True`, `kubelet_evictions{eviction_signal="imagefs.available"}=1`, Pod `Evicted` event |
| [Cleanup enabled](https://app.buildbuddy.io/invocation/d42a5617-72b9-47ec-b446-d1676d9f46e6)  | 334,290,944 / 757,899,264 bytes (44.1%) | 0                   | `DiskPressure=False`, no evictions, Pod `Running`                                                    |

The control target failed because its final Pod-status assertion ran before the asynchronous
terminal status was published; its retained metric and `Evicted` event establish the eviction.
The candidate target passed. This reproduces the mechanism under controlled pressure; attribution
of the original incident to imagefs remains an inference from matching behavior and retained state.
These runs do not estimate fleet latency savings.

The temporary probe used the ordinary `K3s.start` and image importer, with cleanup disabled only
for the control. It ran in a dedicated recycling family, removed its image store during teardown,
and is not part of normal CI. Its source SHA-256 was
`db597322a50adabb9ccf37a4cd650fa68fa476b9208d1336b5a129964bb6c91f`;
the source base was `6d14473a8f0fdda01a7b4f226a2cf92bbece99d7` plus the experiment patch.

## Repair and follow-up

Remove only untagged, unpinned CRI images not referenced by any existing container, before imports,
at group completion, when new imports consume half the group's starting filesystem headroom,
and while the kubelet is stopped during a cold rebuild. Preserve tagged
and digest-named caches, current pinned images, and container-used IDs. Verify removal by rereading
the CRI inventory, tolerating another collector removing an ID concurrently but surfacing survivors.
Keep filesystem, kubelet and image inventories in undeclared outputs so another failure retains
the actual pressure signal and capacity. Follow recurrence after this change lands; do not infer
fleet reliability from one controlled pair or from cached CI results.

Sweeping after every cached import cost 8.51 seconds across 22 calls in Dispatcher and 12.00 seconds
across 26 calls in MCP Actions in the [initial validation](https://app.buildbuddy.io/invocation/4aec62a2-89b2-4296-a4fe-b81ce4352e62).
Checking only new-blob imports still spent 8.65/9.55 seconds across 16/17 calls in a
[cold validation](https://app.buildbuddy.io/invocation/ee1c2f6e-b13c-42b7-a785-868e92c68e87).
The final frequency uses native `statvfs` headroom checks within an import group, with CRI sweeps
at group boundaries and when transient growth consumes half the starting free space. A single
image still needs enough space to unpack, as it did with per-image cleanup; this does not expand
the filesystem or discard current pinned images.
