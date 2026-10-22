# First Piraeus node: execution evidence

Started 2026-10-21 PDT (2026-10-22 UTC). Target: `ovh-ns1001419` only.
The operator authorized executing the conversion, without an etcd snapshot,
using existing rehearsal capacity. No production node has been drained, reset
or repartitioned at this checkpoint. Rehearsal and implementation remain in progress.

The operator resumed through node rebuild/storage acceptance, with an explicit
stop before restoring application data onto Piraeus (step 6 of the execution
outline). Application placement, replacement claims and restore method will be
discussed before that step. Preserve application data off-node through this pause.

## Production preflight

Kubernetes/Talos observations (application/storage operations remain read-only):

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
  27 GB (`104963`). Refresh filesystem free space, Seaweed placement and replica
  health before evacuation; directory totals alone do not authorize a wipe.
- Piraeus controller/operator/CSI controller run on `ovh-ns102453`, independently
  of the target. Existing satellites are still the two FILE_THIN canary workers.
- The Seaweed operator's previous exit reported loss of its Kubernetes leader
  lease. It was Ready at inspection; this does not establish sustained stability.

Attic's 2 GiB local-path request is smaller than its observed directory usage.
Do not recreate that allocation unchanged on a size-enforcing backend. Determine
database/WAL usage and provide headroom (initial candidate: 8 GiB). Study Casino's
1 GiB allocation also needs headroom review (initial candidate: 4 GiB).

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
performance or production-readiness result. Aggregate pool exhaustion and recovery
remain outstanding.

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
blocks after the source deletes or discards them. Alerting, bounded exhaustion
and recovery acceptance are still required.

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
  EPHEMERAL and IMAGE-CACHE. The provider upgrade is a separate prerequisite PR.
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

No production Talos/provider configuration has been applied. Direct authenticated `talosctl -e 51.81.245.180 -n 51.81.245.180 version`
passed from Wyrm2 after earlier timeouts. The temporary relay Pod, ConfigMap and
NetworkPolicy have been removed; no relay is needed. No cordon, drain, promotion, PVC reclaim change or disk write has occurred.
Capacity alert delivery, aggregate thin-pool exhaustion/recovery, same-member Seaweed
master restoration and the application handoff are not yet qualified.

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
