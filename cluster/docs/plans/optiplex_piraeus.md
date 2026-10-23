# Reimage OptiPlex onto Piraeus

Proposal, 2026-10-23. Reimage/configuration changes remain a later maintenance
window. The operator separately approved deleting the three retained OpenClaw
PVs below. Merge and reimage execution remain separate decisions. HDD evacuation
can continue; evaluate any overlapping outage against API, LINSTOR, backup and
off-node staging availability.

## Observed starting point

Inspection and the separately approved cleanup on 2026-10-23 established:

- Worker `optiplex`, Talos 1.14.0, Nebula `10.42.0.18`, LAN
  `192.168.1.10`, NIC MAC `e4:54:e8:85:9f:b2`.
- One SK hynix BC511 NVMe, serial `AS9CN54631CA0CT13`,
  **256060514304 bytes / 238.475 GiB**. Stable install path:
  `/dev/disk/by-id/nvme-BC511_NVMe_SK_hynix_256GB_AS9CN54631CA0CT13`.
- `STATE` and XFS `EPHEMERAL` are partitions; `CRI`, `KUBELET` and `LOG`
  are directories within `/var`. `/var` has about 205 GB used; kubelet reports
  about 193 GB of runtime image/snapshot storage. Active container writable
  layers total about 55 MB. The image inventory has 408 unique digests.
- The initial seven local PV directories totaled about **1.82 GB**. The operator
  approved deleting all three retained `haku-openclaw-spike` PVs, including old
  application state (`0fc4fc0b`) and its source/restore caches (`44149b05`,
  `98475891`). That namespace was already absent. Their files were cleared,
  their PV objects deleted with UID/resourceVersion preconditions, and all
  four Home Assistant PV identities/phases verified unchanged at 09:29 UTC;
  see [cleanup evidence](../../debug/piraeus_migration/optiplex_cleanup.json).
- The preservation set is **four Home Assistant directories, about 22 MB**:
  two Bound claims and two older Released/Retain PVs. Preserve their files and
  old PV/claim identities. The retained Home Assistant copies remain in scope.
- Home Assistant's latest observed VolSync backup succeeded at
  `2026-10-23T06:17:17Z`, Restic snapshot `33239fa9`. Its repository and
  credentials stay in place. Refresh this observation before the window.
- Direct authenticated `talosctl` over **192.168.1.10** succeeds from the
  maintenance machine. No maintenance relay is needed. Maintenance-mode DHCP
  addressing must still be established; the configured secondary LAN address
  and Nebula identity disappear when STATE is reset.

### Preserve these directories

Each source is `/var/local-path-provisioner/pvc-<full-old-claim-UUID>_<namespace>_<claim>`.
The private capture manifest must record the complete UUID/path, PV UID, claim
UID, request, mode/ownership and destination PV/CSI handle. Prefixes below only
keep this worksheet readable. Sizes are observed file bytes, not PVC limits.

**Talos host-path gotcha:** read-only inspection found only empty active-claim
placeholders under the kubelet container's rootfs, while the Talos host view
contains the real data and the released directories. A helper's `hostPath`
`type: Directory` check can therefore fail for an existing host directory.
Audit the exact path through Talos first, then use an unspecified hostPath type
and a read-only runtime mount for capture. Require the expected nonempty file
inventory from that mount before accepting it; an empty kubelet placeholder
cannot be a backup. Keep the helper scoped to the four explicit directories.

| Old claim UUID prefix | Namespace / former claim                                        | State                 | Limit | Observed content | Destination                                    |
| --------------------- | --------------------------------------------------------------- | --------------------- | ----: | ---------------: | ---------------------------------------------- |
| `f39d25d7`            | `home-assistant/home-assistant-config`                          | Bound                 |  32Gi |            14 MB | `home-assistant/home-assistant-config-piraeus` |
| `b72aa944`            | `home-assistant/volsync-src-home-assistant-config-restic-cache` | Bound, VolSync-owned  |   1Gi |           0.5 MB | Same cache claim name, new Piraeus PV          |
| `608a0bc6`            | `home-assistant/home-assistant-config`                          | Released, older claim |  32Gi |           7.6 MB | `home-assistant/recovered-config-608a0bc6`     |
| `7e9d03f8`            | `home-assistant/volsync-src-home-assistant-config-restic-cache` | Released, older claim |   1Gi |           0.2 MB | `home-assistant/recovered-cache-7e9d03f8`      |

Keep the two recovery claims as explicitly managed Retain claims without an
application consumer. All four replacement requests total **66Gi**; the two
active claims total **33Gi**. Do not silently discard the remaining caches or
historical copies.

### Other workloads

- **CPAP gateway VM is stateless.** Its root disk is a containerDisk; no VM
  disk, guest filesystem or RAM backup is needed. Stop/recreate it with its
  existing image, Secret reference, node selector and USB Wi-Fi passthrough.
  `cpap-sync` uses an emptyDir for its Git worktree; durable readings live in
  the Forgejo repository and on the card. Do not interrupt an active sync.
- Keep Home Assistant pinned to OptiPlex with its host networking, existing
  Service/route, integrations and authentication identities. Do not rerun
  onboarding or regenerate tokens during restoration.
- Pause new Haku CI jobs on this host; finish active jobs before draining.
  Ordinary stateless Pods can move through normal eviction. Record home-LAN
  syslog/exporter interruption separately; flush Alloy's WAL before shutdown
  where possible, and accept a bounded telemetry gap rather than backing up
  containerd, CNI, KubeVirt runtime state or old Pod logs.
- The inspected running hostPath consumers were node/runtime agents, not
  additional application data stores. Repeat the inventory, including
  hostPaths and all mounts, before accepting that classification for the wipe.

## Proposed disk layout

Keep Talos system filesystems and Piraeus ownership separate on the same SSD.
Partitions constrain capacity; they share physical I/O and the disk's failure
domain. A worker needs no dedicated etcd partition.

| Allocation                    |                              Size | Purpose                                         |
| ----------------------------- | --------------------------------: | ----------------------------------------------- |
| Talos EFI/META/STATE          |                 Installer-defined | Boot and machine identity                       |
| `EPHEMERAL`                   |                            16 GiB | Remaining Talos `/var` state                    |
| `CRI`                         |                            64 GiB | Images, snapshots and container writable layers |
| `KUBELET`                     |                            32 GiB | Pod state and disk-backed emptyDir              |
| `LOG`                         |                             8 GiB | System and Pod logs                             |
| Raw `piraeus` partition       |                           112 GiB | Exclusive Piraeus LVM physical volume           |
| Boot/alignment/free remainder | 6.475 GiB before boot allocations | Record actual geometry after provisioning       |

Disk-backed `emptyDir` volumes live under `/var/lib/kubelet`, so their budget
comes from the dedicated **32 GiB KUBELET** partition. Memory-backed
`emptyDir` uses RAM. The **16 GiB EPHEMERAL** partition holds the remaining
Talos `/var` state; enlarging it does not enlarge `emptyDir` capacity with
this split. See [Talos dedicated system volumes](https://github.com/siderolabs/talos/discussions/14230)
and [Kubernetes local ephemeral storage](https://kubernetes.io/docs/concepts/storage/ephemeral-storage/).

The fixed allocations total **232 GiB**. Set exact min/max sizes with
`grow: false`, selecting the verified disk serial for every volume. Talos
[`RawVolumeConfig`](https://docs.siderolabs.com/talos/v1.14/reference/configuration/block/rawvolumeconfig)
creates the unformatted partition, labelled `r-piraeus`. It must not consume
the space reserved for the system volumes. Inspect emitted geometry before
enabling Piraeus; do not assume document order determines partition order.

Point Piraeus `source.hostDevices` only at
`/dev/disk/by-partlabel/r-piraeus`, after checking its parent serial and size.
Use pool `ssd`, VG `piraeus_ssd`, thin pool `data`, and the accepted **4 GiB
metadata + 4 GiB spare / 64 KiB chunks** policy. Expect roughly **104 GiB**
of thin-data capacity; measure the actual result. Talos owns partitioning and
Piraeus owns the VG/thin pool. Do not initialize the whole NVMe or add a second
Talos-managed VG owner.

Reuse `piraeus-ssd-local`: one diskful copy, remote access disabled,
WaitForFirstConsumer, expansion and Retain. Pin every restore helper and
consumer to `optiplex` so initial allocation is local. Preserve region `home`
and zone `home-lan`; this adds no automatic WAN replication or relocation.
Verify controller/CSI connectivity to OVH over Nebula as a provisioning and
restart dependency; the diskful copy and ordinary volume I/O remain local.
The proposed 66Gi commitments fit the estimated 104Gi data pool without
logical overcommit. Alerting still protects actual data/metadata headroom.

Bound the image cache with OptiPlex-only kubelet configuration: propose
`imageGCHighThresholdPercent: 70`, `imageGCLowThresholdPercent: 55`, and
`imageMaximumGCAge: 168h`. Before admitting workloads, prove the required
current images fit with GC headroom and verify node ephemeral-storage
accounting against the new mounts. Check CI emptyDir requests/limits against
the 32Gi kubelet budget; change the budget before reimage if they cannot fit.

## Prepare reviewable changes

Prepare two implementation PRs; this document does not activate either.

1. **Host and pool preparation:** extend `terraform/main/home-nodes.tf` with
   an explicit OptiPlex layout and independent pool-readiness gate. Generate
   a 1.14.0 installer preserving the current schematic/Nebula identity and
   adding DRBD; load `drbd` with `usermode_helper=disabled`,
   `drbd_transport_tcp`, `dm_thin_pool` and `dm_snapshot`. The current
   `drbd-canary.tf` loop only covers OVH, so merely adding a label is
   insufficient. Add an inert hostname-and-readiness-scoped satellite/pool
   and per-node monitoring in `cdk8s/piraeus.py` and `piraeus_metrics.py`.
   Keep readiness false until the raw partition is verified. Deny fresh
   local-path allocations on OptiPlex at cutover, including legacy/default
   class aliases, while preserving old bound metadata. Review the targeted
   Terraform plan: no other node apply, no identity replacement or bootstrap.
2. **Claims and consumers:** in `cdk8s/home_assistant/app.py`, change
   `_CONFIG_CLAIM` to `home-assistant-config-piraeus` and `_STORAGE_CLASS` to
   `piraeus-ssd-local`. Both the PVC and VolSync's `sourcePVC` / Restic
   `cacheStorageClassName` must use the intended replacement. Declare the two
   recovery claims alongside the other Home Assistant claims, without a
   workload consumer.
   Preserve the existing Home Assistant namespace/Flux ownership and secrets.
   Regenerate manifests and review the exact consumer diff. CPAP needs no
   permanent disk/SC change; its existing node/device selection stays.

Keep workload changes unapplied under the explicit Flux hold until restoration
is accepted. New claim names avoid mutating immutable PVC storage classes.
Before removing the old Home Assistant PVC from generated desired state,
protect it from Flux pruning with the existing
`kustomize.toolkit.fluxcd.io/prune: disabled` mechanism. Its PV already uses
Retain. Do not rename/delete the namespace or its Kustomization.

## Maintenance sequence

### 1. Freeze writers and make independent recovery copies

1. Refresh node/disk identities, all PVs including Released ones, PVC consumers,
   hostPaths, image pins, device resources, Flux owners, VM and backup state.
   Record original replica counts, schedules and hold states. Verify OVH
   control planes, API, LINSTOR and the chosen off-node staging host are healthy.
   Confirm the pinned Home Assistant/provisioner and CPAP images are pullable
   from their registries before discarding the old image cache.
2. Suspend reconciliation for the owning `home-assistant` and `cpap-sync`
   Flux Kustomizations. Pause the Home Assistant token CronJob, CPAP sync
   CronJob, CPAP image-restart controller, and CI scheduling on OptiPlex.
   Stop Home Assistant cleanly; wait for its Pod to exit. Do not start the
   onboarding Job. Stop the stateless CPAP VM with `runStrategy: Halted` and
   wait for its VMI to disappear. Preserve the existing Secret objects.
3. With Home Assistant stopped, request one final manual VolSync backup and
   require a new successful snapshot. Then set the ReplicationSource
   `spec.paused: true`; wait for all backup/cache movers to exit. Keeping
   Flux suspended alone does not stop VolSync, Deployments or CronJobs.
4. Capture **all four source directories** through a bounded read-only helper
   on OptiPlex. Produce one archive per old PV plus a private identity/content
   manifest. Preserve hidden files, numeric UID/GID, modes, symlinks, hardlinks,
   sparse files, ACLs and xattrs. Never print Home Assistant `.storage`, tokens
   or archive bytes in tool logs. Preserve SELinux metadata in the archive;
   let the target CSI/kubelet establish the required target labels on restore.
5. Retain two verified copies outside OptiPlex: one on a **1Gi strict-local
   Piraeus staging claim on a healthy converted OVH SSD node**, and one in a
   protected persistent directory on the maintenance machine. Do not use the
   space-tight HDD evacuation destination. Verify archive hashes, readable
   member lists, file counts and available space; encryption, if used, must
   be decryptable from the recovery machine before wiping. Keep generated
   machine config separately mode 0600, outside Git.

**Wipe gate:** every retained Home Assistant PV directory has a frozen capture
and both off-node copies; no writer/mover remains; machine config and both
normal/maintenance network paths are ready. An older scheduled Restic snapshot does not replace
the cold capture of all four directories.

### 2. Drain and reimage through Talos

1. Cordon OptiPlex and drain remaining non-DaemonSet workloads through normal
   eviction, after accepting their emptyDir loss. Resolve PDB blockers and
   newly discovered local state before proceeding. Keep home workloads held.
2. Upgrade only OptiPlex to the prepared DRBD image while retaining its legacy
   layout; verify the extensions and direct LAN API return. This preserves
   the bootable image needed by the following maintenance reset.
3. Use the established Talos reset flow with an explicit wipe list for the
   observed unsplit worker: **STATE and EPHEMERAL only**, preserving EFI/META.
   This is destructive to all existing `/var`, including local PVC files.
   Re-read CLI help and volume inventory; do not use an all-disk default or
   the OVH separate-data-disk wipe recipe on this one-disk machine.
4. Reconnect through the home LAN maintenance API at its observed DHCP
   address. Verify disk serial and NIC MAC, record the maintenance certificate
   and pin subsequent maintenance calls. Pinning trusts the first connection;
   use the trusted home-LAN commissioning path, not a public relay. Confirm
   maintenance addressing/recovery access before the reset; keep the existing
   USB provisioning procedure available if boot recovery is needed.
5. Apply the Tofu-generated preserved-identity configuration with the bounded
   system volumes and RawVolumeConfig, pool readiness still false. Do not run
   `talosctl bootstrap`. Wait for `optiplex` to rejoin with its original Nebula
   identity, MAC/addressing, hostname, topology and `user.max_user_namespaces`.
6. Inspect every partition's label, size and parent disk. Require all system
   volumes ready and enough boot/free remainder. Only then enable the
   node-specific Piraeus gate through the reviewed source/targeted apply.
   Confirm that its only LVM PV is the 112Gi raw partition. Reconcile Terraform
   afterward and require no remaining target drift or unrelated changes.

### 3. Accept storage, restore files, switch consumers

1. With application writers still stopped, provision disposable Filesystem
   and Block PVCs on OptiPlex. Verify local affinity, write/read, requested
   capacity, mounted filesystem capacity, pool data/metadata metrics and
   retention. Reboot once and verify those disposable volumes persist.
   This physical check covers the new partition-backed pool; it needs no
   additional QEMU fleet or full benchmark campaign. While the node is
   cordoned, give only these pinned check Pods the explicit
   `node.kubernetes.io/unschedulable:NoSchedule` toleration. Use nodeSelector
   and the scheduler, not `spec.nodeName`, so WaitForFirstConsumer can bind.
   After storage acceptance, uncordon; keep Home Assistant, CPAP and CI holds
   until their own restore gates pass. This admits ordinary stateless Pods
   without leaving restore helpers blocked behind the cordon.
2. Create `home-assistant-config-piraeus` at 32Gi and the two recovery claims
   in the table. Use pinned restore helpers to trigger local provisioning and
   restore each matching cold archive. Compare restored content hashes and
   ownership/modes before starting any application. Record new PV/CSI handles
   against full old identities. Account for expected CSI SELinux relabeling.
3. Keep VolSync paused while replacing its old 1Gi cache claim. It is owned
   by the existing ReplicationSource: preserve that owner identity, recreate
   the same cache claim name on `piraeus-ssd-local`, restore its captured
   files, and verify local binding. Do not try to patch its storageClass in
   place or assume changing `cacheStorageClassName` migrates an existing PVC.
4. With the desired consumer change already committed, keep Flux held and
   patch only the existing Deployment's `config` volume claimName and the
   ReplicationSource's `sourcePVC` / `cacheStorageClassName`. Preserve
   Deployment replicas zero and ReplicationSource paused. Verify the new data
   claim and cache class before any scale-up; applying the full final
   Deployment at this point would restore its declared replica prematurely.
5. Admit the pinned Home Assistant Pod, then verify HTTP/SSO, existing users,
   integrations/devices, automations, history and token consumers. Keep the
   original namespace/Secrets and restore the original replica count. Start
   the stateless CPAP VM from its pinned image; require VMI Ready, guest agent,
   `kubevirt.io/cpap-wifi` allocation, and a card HTTP read through `cpap-card`.
6. Resume VolSync against the new claim and require a successful new Restic
   snapshot in the **existing repository**. Restore token and CPAP schedules,
   image-restart controller and Flux reconciliation to their recorded states.
   Run one CPAP sync and verify its Forgejo result. Resume CI after confirming
   required images, emptyDir budgets and node ephemeral-storage accounting.
7. Remove temporary restore Pods, disposable volumes and migration holds.
   Prove no active claim or new provisioning path still uses the
   erased OptiPlex local-path directory. Retain the two recovery claims and
   both off-node archives; final deletion is a separate decision. Remove stale
   old PV/PVC API objects only after the capture-to-new-volume mapping and
   application/backup acceptance are recorded.

## Recovery boundaries

- **Before reset:** undo holds and restart original workloads against their
  unchanged local-path volumes if a preservation or access gate fails.
- **After reset:** old local-path PV objects no longer provide recoverable
  backing files. Recover from the verified off-node archives or the final
  Home Assistant Restic snapshot. If Piraeus acceptance fails, keep writers
  stopped, repair the new layout/pool, or reinstall the prior layout and
  restore the archives. Merely reverting the Git commit cannot restore data.
- **After new writes:** any rollback needs a fresh cold capture of the current
  Piraeus data. Retain pre-migration archives for historical recovery; do not
  copy an old archive over a newer application state.

## Completion evidence

Keep sanitized observations with the existing migration evidence under
`cluster/debug/piraeus_migration/`, and private archives/configs outside Git.
Record actual partition/LVM geometry, all four old-to-new mappings, capture
and restored-file verification, reboot/PVC results, Home Assistant and CPAP
checks, successful post-cutover backup, fresh per-node monitoring, normal
controller/schedule states and the targeted Terraform drift result.

Sources: [Talos system volume configuration](https://docs.siderolabs.com/talos/v1.14/reference/configuration/block/volumeconfig),
[Piraeus Talos preparation](https://piraeus.io/docs/v2.10.5/how-to/talos/),
[Piraeus raw-device pool creation](https://piraeus.io/docs/v2/reference/linstorsatelliteconfiguration/),
[VolSync Restic source/cache configuration](https://volsync.readthedocs.io/en/stable/usage/restic/index.html).
