# Auto-diskful canary

Fixtures and [live observations](results.md). This experiment is separate from
the existing `local-canary` class and its earlier acceptance results. Apply
explicitly; Flux does not own these disposable objects. Do not change the
provider or host setup.

The new class permits remote access, requests one disk-backed copy, and sets
`DrbdOptions/auto-diskful: "1"` (minutes) with automatic cleanup explicitly enabled.
It uses only the two previously prepared workers and their existing `local-canary`
FILE_THIN pools. The 1Gi claim bounds physical allocation. Preserve the earlier
20Gi per-node stop threshold and existing production headroom.

## Preconditions

- Both prepared workers Ready, schedulable, and LINSTOR ONLINE; satellites,
  controller, CSI and affinity controller healthy.
- Inspect existing pool use and active resources before creating anything. Do not
  reuse an existing namespace, class, PVC or resource group with these names.
- Inspect effective provisioning properties and the resource group's replica
  count after provisioning. A class parameter alone is not proof of behavior.
- Record PVC UID, PV name, CSI handle, LINSTOR resource name and initial placement.
- Use normal termination and detachment. Never force promotion, manipulate
  bitmaps, delete a source copy manually to manufacture a pass, or reboot hosts.

## Sequence and acceptance

1. Apply `fixtures.yaml`. Confirm initial provisioning on `ovh-ns102453`.
2. Fully write the disposable 1Gi raw device with fio CRC32C verification and
   fsync, then record a full-device SHA-256. This dense first pass separates the
   automatic transition from the previously observed sparse-resync behavior.
3. Stop only Pod `io`, wait for detachment, and recreate it from the fixture with
   `spec.nodeSelector.kubernetes.io/hostname: ovh-ns103656`. Keep the existing PVC.
   Do not manually create or toggle a LINSTOR resource on the destination.
4. Observe remote access on the destination before local synchronization. Record
   its role, disk state and I/O success immediately; the one-minute delay begins
   when LINSTOR observes that this volume is in use as a diskless Primary.
5. Observe LINSTOR initiating local allocation and synchronization automatically.
   Require the destination to become UpToDate before source cleanup. Record
   progress and timestamps; finish within a bounded observation window (10m),
   stopping if headroom falls below the existing budget.
6. Require automatic source removal and exactly one remaining disk-backed copy
   on the destination, without manual copy deletion. Diskless client resources,
   if present, are not additional data copies. Verify the unchanged PVC UID and
   CSI handle, full-device hash and CRC32C, and local fsync writes after cleanup.
7. Repeat in reverse. Include an I/O stream during synchronization in a separate
   pass, preserving a checksum-verified immutable region. Check final data after
   the stream stops; do not expect a pre-write hash to match new data.
8. Separately repeat with a fresh 1Gi claim containing only a 64MiB payload. Keep
   generated resync defaults for this pass so it can expose the known sparse
   behavior. If it stalls, preserve diagnostics and report failure; do not hide
   the stall with a reconnect. Any tuning belongs in a separate recorded pass.
9. After recording results, stop the test Pod, wait for detachment and verify the
   recorded IDs. Delete only this experiment's claim, retained PV, matching
   LINSTOR resource definition, empty resource group, namespace and class.
   Confirm no experiment backing image or resource remains on either worker.

The class permits remote access only on nodes with the installed CSI/satellite
stack. It does not make unprepared NixOS or roaming nodes eligible. A single-copy
volume still needs its source alive and reachable until the destination is fully
synchronized; this experiment does not provide automatic recovery from loss of
the only data copy.

## Source grounding

[LINSTOR auto-diskful documentation](https://linbit.com/drbd-user-guide/linstor-guide-1_0-en/#s-linstor-auto-diskful)
describes the trigger, delay and cleanup. The pinned LINSTOR v1.35.2 implementation
is `controller/src/main/java/com/linbit/linstor/tasks/AutoDiskfulTask.java`:
`update()` tracks in-use diskless resources; `run()` chooses a suitable local
storage pool; cleanup waits for UpToDate and protects the newly local resource.
The old resource must be eligible for cleanup and the group must request fewer
copies than currently exist. No whole-node diskless designation is involved.
