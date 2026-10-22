# First Piraeus node: execution evidence

Started 2026-10-21 PDT (2026-10-22 UTC). Target: `ovh-ns1001419` only.
The approved rebuild and the four application restorations are complete. The
node is schedulable and its whole secondary NVMe backs the Piraeus SSD pool.
The operator lifted the application-restoration pause before the work below.

## Application restoration checkpoint (2026-10-22 11:44 UTC)

[Sanitized restoration evidence](application_restoration_proof.json) records:

- Attic is 3/3 healthy, with its new 8Gi replica on Piraeus at this node. Study
  Casino is 3/3 healthy, with its new **1Gi** replica here. Native CNPG streaming
  replication replayed an observed primary WAL position for each new member;
  original member/PVC identities and primaries were unchanged.
- The new `seaweedfs-volume-ssd-piraeus-0` uses a 250Gi strict-local Piraeus claim.
  It joined the existing SSD/DC/rack topology and passed one rate-limited native
  move of a 239 MB volume. All 34 SSD logical volumes retained two copies; old
  server Pods/claims were unchanged. Owned S3 reads across the move and a fresh
  write/read matched hashes. The S3 gate and SSD move verified separate paths.
- Master-2 returned from temporary HDD storage on `ovh-ns102453` to a **4Gi**
  `piraeus-ssd-local` claim on `ovh-ns1001419`. A fresh cold Raft copy preserved
  all file hashes, ownership, modes and timestamps. The original topology
  identity, three-peer template, surviving Pod UIDs and volume-ID watermark were
  preserved. All masters agreed on the leader; retained and fresh owned S3
  probes matched. Probe objects/helpers were removed and operator/Helm/Flux
  reconciliation resumed successfully.
- The temporary HDD source PV remains `Released`/`Retain`; its cold archive is
  also preserved privately off-node. It is not another live master. The shared
  master claim template still names local-path, so preserve master-2's existing
  Piraeus PVC override when recovering; Pod replacement reuses the claim but
  deleting that claim would lose the override.
- Four retained Piraeus PVCs now request 263Gi in total: 250Gi Seaweed volume,
  8Gi Attic, 1Gi Study Casino and 4Gi Seaweed master. Live thin-pool reporting
  observed 64KiB chunks, 4GiB metadata, 4GiB spare and no free VG extents. Thin
  allocation is separate from logical PVC capacity.

Next-node preparation is described in the [node-roll runbook](../../docs/runbooks/piraeus_node_roll.md).
No subsequent node reset, wipe or reformat is covered by this checkpoint.

## Physical-node checkpoint (2026-10-22)

[Sanitized production evidence](production_acceptance.json) records:

- System serial `190130800417`: ETCD 16 GiB, EPHEMERAL 32 GiB, CRI 192 GiB,
  KUBELET 128 GiB, LOG 16 GiB. Kubelet reports the 128 GiB filesystem as
  `nodefs` and the 192 GiB filesystem as image/container storage.
- Data serial `1951A3449505`: one whole-device LVM PV, `piraeus_ssd/data`,
  503517806592-byte thin-data LV, 4 GiB metadata, 4 GiB metadata spare,
  64 KiB chunks, no free VG extents. Existing FILE_THIN canaries are separate.
- The same-version DRBD image upgrade completed. Selective STATE/EPHEMERAL
  reset gracefully removed the old etcd member; the prepared configuration
  rejoined the existing cluster without bootstrap. No etcd snapshot was taken.
  A subsequent physical powercycle returned a new boot ID, three healthy etcd
  members and matching hashes for all eight source/restored disposable volumes.
- Default DRBD and STORAGE-only paths both provisioned sparse 50 GiB
  Filesystem/Block claims, restored snapshots with matching 16 MiB seed hashes,
  expanded online to 52 GiB, rejected out-of-bounds block writes, and enforced
  ENOSPC on separate 1 GiB filesystems. Discard released physical blocks while
  preserving seeds. UID/fsGroup 26 could write and restore independent seed files.
- Controller and satellite replacement preserved all eight seed hashes.
  Retain preserved each small test backend after PVC removal; an explicit,
  UID-guarded change to Delete removed its PV and LINSTOR resource.
- Live data/metadata samples were scraped into Mimir; all seven rules evaluated
  successfully. Pausing only the collector's sleep child produced the existing
  stale alert in Alertmanager; resuming sampling cleared it. Data/metadata
  utilization thresholds were not deliberately forced on the physical pool.
- An independent metadata export imported and re-exported into an isolated H2
  database with all 407 rows across 34 tables equal. This validates export
  contents. A live controller disaster restore remains unqualified.
- All temporary acceptance Pods, claims, snapshots, classes and eight backing
  resources were removed after recording their proofs. The three owned QEMUs
  were stopped after local memory pressure was reported, releasing about
  12 GiB resident RAM; disk images and private evidence remain on disk.

The four original target PV objects remain `Released`/`Retain` inventory records.
Their original files were erased by the approved physical data-disk wipe.
Recovery sources are the verified off-node database/Seaweed copies and the
independent Seaweed Raft archive.

### Physical performance observations

Matching single-job, queue-depth-one, direct-I/O fio microbenchmarks used the
same NVMe pool. Each durable-write run used 4 KiB random writes plus
`fdatasync=1` for 20 seconds after a 3-second ramp. The warmed second pair:

| Path                  | Durable writes/s | Sync p99 | Verified sequential write | Verified sequential read |
| --------------------- | ---------------- | -------- | ------------------------- | ------------------------ |
| STORAGE-only LVM_THIN | 1605             | 1.045 ms | 839 MiB/s                 | 998 MiB/s                |
| One-copy DRBD,STORAGE | 1538             | 1.057 ms | 547 MiB/s                 | 1356 MiB/s               |

Sequential runs wrote and CRC32C-verified 1 GiB with 1 MiB blocks. The initial
durable pair measured 926/824 writes/s and overlapped acceptance setup. These
short measurements cover bounded fio jobs; application throughput and a
plain-partition local-path baseline remain unmeasured. Sequential DRBD overhead
was measurable. Client/API reads
and approval review also timed out during reported local memory pressure.
Later queries succeeded after owned QEMUs were stopped; a persistent LINSTOR
API stall was not reproduced.

### Remaining qualification

Metadata exhaustion and aggregate DRBD pool exhaustion remain unqualified.
Production capacity-threshold alert delivery and a full LINSTOR controller
restore have not been exercised. The isolated metadata round trip and observed
stale-alert delivery have the narrower scope described above. Application
performance and the first-node soak remain unqualified. Application redundancy
has returned through the accepted restorations above.

## Production handoff checkpoint (2026-10-22 08:20 UTC; historical)

The following handoffs moved data to existing off-node storage; they did not
restore application data to Piraeus.

- **Etcd:** the last observation found all three members healthy at the same
  applied index; `ovh-ns1001419` was leader. Refresh membership and catch-up
  immediately before any reboot.
- **CNPG:** Attic is 2/2 ready, primary `attic-db-2` on `ovh-ns104952`, standby
  `attic-db-3` on `ovh-ns104963`; the new standby uses an 8 GiB request while
  the remaining off-node member's 2 GiB claim is unchanged. Study Casino is
  2/2 ready, primary `study-casino-db-6` on `ovh-ns104952`, standby
  `study-casino-db-1` on `ovh-ns104963`; all remaining Study Casino claims stay
  at 1 GiB. Both target members were removed, their source PVs remain
  `Released` with `Retain`, and replication lag was zero. See the
  [CNPG off-node proof](cnpg_offnode_handoff_proof.json).
- **Seaweed volume server:** all 16 volumes (IDs 867–882) have two locations on
  the two surviving SSD servers, with no replication dry-run findings. The
  evacuated source remains alive, empty, and in maintenance. FUSE clients were
  refreshed; source request counters stayed unchanged for 40 seconds with no
  in-flight requests. See the
  [volume evacuation proof](seaweed_volume_evacuation_proof.json).
- **Seaweed master:** master 2 now uses a temporary 4 GiB HDD-backed claim on
  `ovh-ns102453`, preserving the original topology identity. All three masters
  agree on the leader and volume high-water mark; the two surviving master Pod
  UIDs and StatefulSet template were unchanged. The Seaweed CR stayed at three
  replicas while its operator was briefly set to zero for the cold state copy;
  the CR and HelmRelease holds were then removed. Its original source PV remains
  `Released`/`Retain`. A 64 KiB owned S3 probe was read after restart and a fresh
  probe write/read also matched. See the
  [master handoff proof](seaweed_master_handoff_proof.json) and
  [S3 recovery proof](seaweed_master_s3_recovery_proof.json).
  BusyBox tar did not preserve directory mtimes during restore; UTC directory
  mtimes were restored explicitly and all six entries were verified afterward.
- **Reconciliation holds:** all temporary holds are removed. A direct suspend
  of the child CNPG Kustomization was reverted by its parent and briefly
  downscaled the first Attic clone; the original two members stayed healthy.
  The handoff then used the documented per-resource reconcile annotation on the
  Cluster. Do not repeat the child-Kustomization suspend approach. The Seaweed
  handoff separately used resource annotations on its CR and HelmRelease; the
  operator was restored after the cold copy and the Seaweed CR replica count
  remained three.
- **Still gated:** monitoring PR #11512 is merged, but the production Piraeus
  metrics DaemonSet has desired count 0 and the existing FILE_THIN canary pools
  are unchanged. Production thin-pool alert delivery and target pool
  acceptance remain unproven. Do not create an application Piraeus PVC or
  restore app data until the explicit user pause is lifted.

## Initial production preflight snapshot (2026-10-21)

This is a historical baseline from before the off-node handoffs. Refresh live
placement, capacity and health before any future operation.

These Kubernetes/Talos observations were captured read-only before application
handoffs:

- All three OVH control planes were Ready. Etcd members `.16`, `.17`, `.19`
  reported no errors and applied indexes within one entry of each other;
  `.19` was leader. All inspected CNPG clusters reported healthy instances.
- Verified system NVMe serial `190130800417` and data NVMe serial
  `1951A3449505`; each is 512110190592 bytes.
- The target has the four expected local-path claims: Attic member 1,
  Study Casino member 2, Seaweed master 2 and SSD volume server 2.
- `talosctl usage --depth 2 --humanize /var/mnt/seaweedfs-data` reported
  approximately 5.9 GB total: Attic 3.3 GB, Study Casino 620 MB, Seaweed volume
  server 2.0 GB and master state 9.7 kB. These are directory-usage observations,
  not database logical sizes or PVC reservations.
- The other SSD data filesystems used approximately 31 GB (`104952`) and
  27 GB (`104963`). This is only a pre-evacuation snapshot; refresh filesystem
  free space and workload placement before any future disk operation.
- Piraeus controller/operator/CSI controller run on `ovh-ns102453`, independently
  of the target. Existing satellites are still the two FILE_THIN canary workers.
- The Seaweed operator's previous exit reported loss of its Kubernetes leader
  lease. It was Ready at inspection; this does not establish sustained stability.

Attic's 2 GiB local-path request is smaller than its observed directory usage.
Do not recreate that allocation unchanged on a size-enforcing backend. Determine
database/WAL usage and provide headroom (initial candidate: 8 GiB). The operator
confirmed that Study Casino keeps its existing 1 GiB capacity; no increase is planned.

Preserved machine configuration and full inventories are in a private local
directory, outside git. No etcd snapshot was taken. Treat Talos machine-config
`spec` as a YAML string, not an object; never print it during inspection.

## Rehearsal environment

Wyrm2 has accessible `/dev/kvm` outside the agent filesystem sandbox. The sandbox
hides it, so an in-sandbox existence check incorrectly suggested it was absent.
The host reported approximately 60 GiB available RAM and 404 GiB free on `/tmp`.
Atlas's VLAN address was unreachable, its hostname did not resolve, and its mesh
address rejected the available SSH key; Atlas access is unnecessary for this lab.

Three local KVM VMs each use 2 vCPUs, 4 GiB RAM, a sparse system disk of exactly
512110190592 bytes and a disposable 32 GiB data disk. Their PKI, hostname, node
addresses (`10.91.0.11`–`.13`), pod CIDR and service CIDR are separate from production.
Talos APIs are forwarded only onto localhost ports 50011–50013. Production
credentials are not used in the VMs. No cloud server was ordered.

Pinned components:

- Talos `v1.14.0`, source revision `9abd05af`; CLI `v1.14.2`.
- Kubernetes `v1.35.1` configuration.
- Image Factory schematic
  `bd238aba78074fdd4d509ca7b1e9062405787225edcd93faf0de15a3425540a3`:
  DRBD and iSCSI extensions, serial console and stable `eth0`/`eth1` names.
- QEMU `10.2.4`, supplied through the repository Nix devshell.
- Installed provider matches production: Piraeus Operator `2.12.0`,
  linstor-cluster chart `1.3.2`, cert-manager `v1.21.2`.
- LINSTOR `1.35.2`, CSI `1.13.1`, DRBD kernel module `9.3.3`.
- External snapshot controller/CRDs `v8.6.0`, matching production's pin.

The five partition documents use fixed `minSize`/`maxSize`, `grow: false` and
the system disk's explicit serial: ETCD 16 GiB, EPHEMERAL 32 GiB, CRI 192 GiB,
KUBELET 128 GiB and LOG 16 GiB. All three nodes provisioned the layout and
joined the disposable control plane. Approximately 89 GiB remains unallocated.

## Runtime observations

These are bounded functional checks on disposable virtual disks, not an OVH
performance or production-readiness result. Aggregate data-pool exhaustion/recovery was exercised on the STORAGE-only stack;
metadata exhaustion and DRBD behavior at aggregate pool exhaustion remain unqualified.

| Check                        | Observed result                                                                                                                                      |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| System layout                | Separate ETCD, LOG, EPHEMERAL, KUBELET and CRI filesystems mounted at their intended paths                                                           |
| Kubelet accounting           | Node filesystem capacity 137371844608 bytes; image/container filesystem capacity 206057766912 bytes                                                  |
| Disk-backed emptyDir limit   | Kubelet evicted a pod after a 32 MiB write exceeded its 16 MiB ephemeral-storage limit                                                               |
| Whole data disk provisioning | All three 32 GiB disks became LINSTOR LVM_THIN pools; no manual VG/LV creation                                                                       |
| Pool geometry                | 32208060416-byte data LV, 1 GiB metadata LV, 1 GiB metadata spare, 64 KiB chunks; no free VG extents                                                 |
| Layer selection              | Default class creates DRBD,STORAGE; `layerList: storage` creates STORAGE only                                                                        |
| Filesystem and Block         | Both modes provisioned, attached and accepted a 16 MiB random-data write on both layer stacks                                                        |
| Filesystem boundary          | Both 64 MiB ext4 claims filled to ENOSPC; non-root UID 26 with fsGroup 26 could create and read its seed                                             |
| Discard                      | After deletion and fstrim, backing-volume allocation fell from about 97% to 14% on both stacks; the retained seed hash matched                       |
| Block boundary               | Both reject writes beyond their actual block-device end with ENOSPC                                                                                  |
| Online expansion             | Both Block claims grew 1 to 2 GiB; both ext4 filesystems grew 4 to 6 GiB                                                                             |
| Snapshots                    | Restored Filesystem and Block claims on both layer stacks; all four 16 MiB seed hashes match the original data                                       |
| Reboot                       | All original seed hashes still match after shutdown, disk-file relocation and restart of the three VMs                                               |
| Control-plane replacement    | Selective system-volume reset removed one etcd member; saved cluster credentials rejoined it as a new member without bootstrap                       |
| Repartition                  | Rebuilt a full-disk legacy EPHEMERAL layout into the five accepted fixed system partitions                                                           |
| Maintenance access           | Reapplied config with the current serial-console certificate fingerprint through a relay Pod on a survivor; all three etcd members healthy afterward |
| Capacity scheduling          | A 50 GiB claim could not schedule onto a 30 GiB pool with default CSI capacity reporting                                                             |

The DRBD device can exceed the requested byte count slightly because LINSTOR
aligns its backing allocation to VG extents and reserves DRBD metadata. The
1 GiB DRBD claim initially exposed 1077665792 bytes; the storage-only device
exposed exactly 1073741824 bytes. These are bounded block devices, unlike an
unlimited local-path directory; they are not byte-exact filesystem quotas.

The empty pool's metadata usage was approximately 1.6%; formatting two ext4
volumes and seeding all four claims used under 2% of the 30 GiB physical data
pool. Thin allocation does not make pool exhaustion safe. Snapshots also retain
blocks after the source deletes or discards them. Production alerts and
first-node acceptance are still required. The STORAGE-only aggregate data-pool
exhaustion/recovery result is recorded below.

The four initial snapshots exceeded the 120-second wait, then became ready
without a controller restart. During that interval the Kubernetes API logged
etcd request timeouts; etcd logged delayed heartbeats and slow requests. A Java
thread dump showed LINSTOR waiting in its Kubernetes CRD transaction cleanup
request. Wyrm2 reported host I/O pressure around 40% while its CPU pressure was
zero; the VM disks were on the busy `/tmp` filesystem. This establishes a lab
control-plane stall, not its complete underlying cause or a Piraeus defect.
The three VMs were shut down and their writable disk files copied to the existing
Bazel-cache filesystem (`/dev/vde`); `/tmp` is on `/dev/vdh`. Each VM returned to
Ready with its existing etcd membership before the next was shut down. Subsequent
snapshots and the restore/hash checks completed. This does not establish
production latency or eliminate the need to investigate any recurring stalls.

The first test overlapped expansion with snapshots still in progress. Restoring
the older Block snapshots into 2 GiB claims left provisioning retries reporting
`AlreadyExists` because LINSTOR recorded a 1 GiB volume definition. The affected
claims never bound. New snapshots taken after expansion finished restored
successfully into matching 2 GiB claims. Larger-than-snapshot restore and concurrent
snapshot/resize behavior remain unqualified; do not infer support from the
successful sequential checks. The initial failed requests remain in the lab for
inspection. [Recorded hashes](pvc_restore_hashes.json) cover the successful
Filesystem restores and the later Block restores.

## Declarative preparation

- Talos provider 0.12 uses the 1.14 SDK needed by the additional system volumes;
  0.11 uses the 1.13 SDK, whose VolumeConfig validator accepts only STATE,
  EPHEMERAL and IMAGE-CACHE. The provider 0.12 pin was updated in PR #11464;
  this Terraform provider change is not applied by Flux. First-node preparation
  merged in PR #11507.
- Only the target's desired Talos configuration removes its old whole-disk
  UserVolumeConfig, selects the verified system disk, adds the five fixed system
  volumes and uses the existing DRBD image schematic. Other nodes keep their
  existing configuration and rescue image.
- Apply that configuration only after evacuation and disk preparation. Its
  `storage.allegedly.works/piraeus-ready=true` label opts into the existing Piraeus
  cluster. The raw-device pool additionally requires the target hostname and
  uses the verified data-disk by-id path. It requests 4 GiB metadata plus its LVM
  spare and 64 KiB chunks; actual geometry still needs first-node acceptance.
- Existing FILE_THIN workers remain on their canary pool. Controller components
  remain on those workers, independently of the first production data disk.
- An explicit empty local-path `nodePathMap` entry denies new local-path claims
  on the target instead of silently placing them on the system disk.

At the 08:20 checkpoint, no target Talos machine configuration had been applied;
the target's `piraeus-ready` label was absent and the preserved configuration
matched the original byte-for-byte. Direct authenticated
`talosctl -e 51.81.245.180 -n 51.81.245.180 version` passed from Wyrm2 after
earlier timeouts. The temporary relay Pod, ConfigMap and NetworkPolicy have
been removed; no relay is needed. All four exact source PVs were changed to
`Retain` with PV/claim UID guards and verified readback. The CNPG and Seaweed
off-node handoffs have since completed as recorded above. The later 08:35
checkpoint records the completed drain, cordon, and started image upgrade;
reset and data-disk wipe have not occurred.

Same-member Seaweed master recovery and CNPG/volume off-node recovery are now
qualified. Production Piraeus pool acceptance, alert delivery, and any Piraeus
application PVC creation/restoration remain outstanding. Aggregate DATA
exhaustion/recovery passed on the lab STORAGE-only stack, with the limits
recorded below.

### Rehearsal friction

- The new CLI generates multi-document configuration, including EPHEMERAL,
  HostnameConfig and KubeNodeConfig. Merge volume documents through
  `talosctl machineconfig patch`; appending another EPHEMERAL document is invalid.
  Mixing generated documents with duplicate legacy fields is also rejected.
- The QEMU DHCP DNS proxy timed out. Explicit resolvers initially allowed
  DNS/NTS and installer progress; the final lab config uses Cloudflare DNS over
  HTTPS to avoid intermittent UDP resolver failures.
- The VMs share the same NAT address on their WAN interfaces. Flannel must use
  the separate lab mesh interface (`--iface=eth1`); duplicate NAT tunnel addresses
  otherwise break cross-node Pod traffic and admission-webhook requests.
- A cached factory ISO redirect returned HTTP 403. A fresh public image URL
  downloaded successfully; certificate verification remained enabled.
- QEMU's emulated NVMe path timed out on discard/read operations during install
  and formatting. The issue also occurred with explicit `aio=threads` and UEFI
  disk-image boot. It is not evidence of a production physical-NVMe problem.
  Only owned disposable VMs were stopped; failed overlays remain local.
- The current attempt uses UEFI plus virtio block devices at the same capacities
  and with the same serial selectors. It cannot establish physical NVMe behavior
  or OVH performance. Those need checks on the actual first node.

### Reset and maintenance procedure

The successful legacy-to-split rehearsal used `talosctl reset` with explicit
`--wipe-mode=system-disk --system-labels-to-wipe=STATE,EPHEMERAL`, graceful member
removal and reboot. Those were the legacy system partitions requiring recreation;
EFI, META and the separate Piraeus data disk were preserved. When resetting an
already split lab node, the explicit list also included ETCD, LOG, KUBELET and CRI.
Do not apply the split-node label list blindly to the legacy production layout.
Talos wiped the selected partitions and removed their GPT entries, then booted into
maintenance mode. Applying the preserved identity plus the new volume configuration
recreated the system partitions and rejoined the existing cluster without bootstrap.

A staged-config shortcut does not work: Talos's runtime validation rejects changing
ETCD/CRI/KUBELET/LOG between directory and dedicated-partition backing after creation,
including in staged mode. The [runtime validator](https://github.com/siderolabs/talos/blob/v1.14.0/pkg/machinery/config/types/block/volume_config.go)
runs before apply-mode dispatch. Preserve the original machine configuration outside
git, verify a maintenance access route before clearing STATE, then apply the intended
configuration through maintenance mode. Nebula credentials must be restored from the
preserved config; they are unavailable while STATE is empty. The lab's relay used
an ordinary nonroot Alpine Pod on a survivor, a localhost-only port-forward and a
fixed destination. Production uses direct public-IP access, verified from Wyrm2; the relay is unnecessary.

After the latest lab reset, the fresh CRI filesystem initially had no Piraeus
images. The satellite and CSI init containers waited for image download and then
LINSTOR reconnection. A later check found all satellites Available/Configured,
all pools online and the three etcd members caught up without issuing a reconnect
command. The earlier OFFLINE observation was during convergence, not a demonstrated
persistent rejoin failure.

### Aggregate data-pool exhaustion and recovery

Two 20 GiB STORAGE-only Block claims were provisioned on the empty 30 GiB cp3 pool.
Paced 64 MiB writes filled B, then A after its retained 16 MiB seed. At data 100%,
metadata was 2.37%, device-mapper reported `out_of_data_space` and `queue_if_no_space`,
and LINSTOR reported zero free bytes while pool state remained `Ok`. The writer was
stopped at that boundary; no application-visible error or timeout duration was measured.
Interrupting the local kubectl exec left one remote dd child; the exact child was
terminated and absence of writers verified before recovery.

Discarding only B returned the pool to 20 GiB free, data about 33.34% and metadata
1.85%. A's original seed hash matched, and a new bounded 4 MiB write/readback matched.
All three etcd members and satellites were healthy afterward. The two test claims
remain bound and idle. [Recorded results](thin_pool_exhaustion.json) include the exact
resource identities and hashes. This did not exercise metadata exhaustion or the
DRBD layer's behavior at aggregate pool exhaustion.

CSIStorageCapacity lagged actual physical usage: at exhaustion it still advertised
about 9.79 GiB; immediately after recovery it temporarily advertised zero before
converging. Use direct thin data/metadata gauges for physical headroom alerts.

### Thin-pool metrics prototype

A disposable lab DaemonSet using the pinned LINSTOR image queried the exact VG/pool
with reporting-only `lvs`, mounting host `/dev` read-only. A nonroot node-exporter
container exposed only textfile metrics over the Pod network. Direct satellite,
collector and HTTP-scrape samples agreed: data 1.57%, metadata 1.61% at that checkpoint.
Injecting a failed collector command produced success=0 and removed stale percentage
samples. `lvs --readonly` suppresses device-mapper kernel status queries and returned
blank percentages here; ordinary `lvs` is needed even though the operation only reports.
This proves the collection mechanism, not production scrape wiring or delivered alerts.

Do not infer capacity exhaustion or recovery from successful individual PVC limits,
configuration validation or a partially formatted disk.
