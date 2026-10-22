# Piraeus storage inventory, 2026-10-19

Read-only observations collected at **07:08–07:16 UTC**, against source revision
`882f2c3ce72148cd463aaa86256972e5aac6ec25`. No nodes were rolled, devices
formatted, workloads stopped, volumes deleted, or configuration applied.
This snapshot is allocation evidence, not permission to reuse a device.

## Talos disks and filesystem capacity

Seven Talos nodes answered directly over their Nebula IPs. All run Talos 1.14.0;
the client was 1.14.2. Capacities below are **decimal GB**, rounded by
`talosctl mounts`; they are filesystem sizes, not unallocated disk extents.

| Node          | Data backing                                       | Data filesystem GB | Used GB | Available GB | Install `/var` available GB |
| ------------- | -------------------------------------------------- | -----------------: | ------: | -----------: | --------------------------: |
| ovh-ns102453  | `/dev/sdb`, HDD, whole-device XFS                  |            3998.83 | 1612.86 |      2385.98 |                     3341.89 |
| ovh-ns103656  | `/dev/sdb`, HDD, whole-device XFS                  |            1999.42 | 1120.84 |       878.58 |                     1467.29 |
| ovh-ns103711  | `/dev/sdb`, HDD, whole-device XFS                  |            1999.42 | 1151.25 |       848.17 |                     1609.77 |
| ovh-ns104952  | `/dev/nvme1n1`, SSD, whole-device XFS              |             449.88 |   39.38 |       410.50 |                      259.90 |
| ovh-ns104963  | `/dev/nvme1n1`, SSD, whole-device XFS              |             449.88 |   39.18 |       410.69 |                      261.64 |
| ovh-ns1001419 | `/dev/nvme1n1`, SSD, whole-device XFS              |             511.86 |   12.06 |       499.80 |                      471.19 |
| optiplex      | One 256.06 GB NVMe; XFS `/dev/nvme0n1p4` is `/var` |             253.63 |  192.88 |        60.75 |             same filesystem |

OVH HDD data mounts are `/var/mnt/local-path-ovh-hdd`; OVH SSD data mounts are
still `/var/mnt/seaweedfs-data`. OptiPlex's local-path directory is on its system
`/var`, not a separate disk. Its cached kubelet summary independently reported
253626249216 capacity bytes and 60741558272 available bytes, but returned no
per-PVC volume metrics. Consequently this inventory does not claim per-PVC
physical usage or a complete breakdown of the 192.88 GB used there.

The large free space on OVH install disks is also **inside mounted XFS `/var`**.
No unused device suitable for the proposed pools was identified. Converting
either an occupied data filesystem or the install-disk layout needs its own
evacuation/rebuild procedure; this snapshot does not establish a safe in-place shrink.

Evidence: [disk identities and discovered volume geometry](talos-layout.json),
[filesystem readings](talos-filesystems.tsv), and [node versions/labels](nodes.tsv).

## Consumers and allocation promises

The Kubernetes snapshot contained **98 PVs: 93 Bound and 5 Released**.
[pv-consumers.tsv](pv-consumers.tsv) records every PV's phase, class, declared
capacity, backing path/handle, node constraints, claim-UID match and Pod consumers.
TSV fields use `-` for absent values or no current consumers. Pod consumers are
associated only when the current PVC UID and bound PV match the recorded claim.
Released objects and unmatched claim identities remain visible; their old claim
names do not prove ownership by a current PVC with the same name.

| OVH data node | Bound local-path PVs | Sum of declared GiB |
| ------------- | -------------------: | ------------------: |
| ovh-ns102453  |                   24 |                2536 |
| ovh-ns103656  |                   10 |                1890 |
| ovh-ns103711  |                   19 |                2368 |
| ovh-ns104952  |                   11 |                 311 |
| ovh-ns104963  |                   12 |                 316 |
| ovh-ns1001419 |                    2 |                 251 |

These local-path requests neither reserve nor cap physical space. The 2 TB HDD
nodes already have declared requests above their data filesystem capacity;
shrinking their legacy filesystem is not justified by its current free bytes.
For example, `103711` has both an 1800 GiB SeaweedFS claim and a 500 GiB
ClickHouse claim. Their sum is not measured data usage.

[seaweed-placement.tsv](seaweed-placement.tsv) maps all six volume-server Pods
to their node and PVC. SSD node `1001419` has the smallest observed data footprint:
its two local PVCs belong to SeaweedFS and a study-casino database replica.
[cnpg.json](cnpg.json) records CNPG instance counts and current primary names;
it is a timestamped dependency snapshot, not an ongoing health or backup guarantee.
Singletons such as the testing PostgreSQL instance need explicit downtime handling.
The five Released PVs are retained Home Assistant/OpenClaw/VolSync data; none was removed.

## Wyrm2: observed guest allocation

The local host is wyrm2, running kernel **6.18.54**. Its guest disks are virtual;
their rotational flags are all true and do **not** establish physical media.
The SSD/HDD mapping below comes from the checked-in Proxmox/NixOS declarations.
Live Atlas datastore backing and free space could not be verified.

| Device / owner              | Size or capacity | Observed free/allocation                |
| --------------------------- | ---------------: | --------------------------------------- |
| `/dev/vda`, ext4 local-path |   500 GiB device | 480176246784 filesystem bytes available |
| `/dev/vdc`, OpenEBS SSD VG  |   500 GiB device | 504495079424 VG bytes free              |
| `/dev/vdg`, OpenEBS HDD VG  |   500 GiB device | 72834088960 VG bytes free               |
| `/dev/vdl`, ext4 kubelet    |   500 GiB device | 500050448384 filesystem bytes available |
| `/dev/vdi`, ext4 LLM models |     1 TiB device | 159977308160 filesystem bytes available |

Both VGs remain owned by OpenEBS. Its HDD thin pool is 463126659072 bytes,
72.47% data-used and 27.05% metadata-used; two thin LVs match current Bound PVs.
Its SSD thin pool is 32212254720 bytes, 11.65% data-used and 13.95% metadata-used.
Eight SSD thin LVs have **no matching current PV name/CSI handle or OpenEBS volume
object**. They have not been proven unused, backed up, disposable, or safe to
delete. VG free extents are not a reservation for a competing provider: OpenEBS
has auto-extension enabled.

The source VM has `ignore_changes = [disk, ...]`; editing its Terraform disk
blocks alone does not hot-add a disk. Exact new disk slots, backing media and
Atlas headroom must be established before the attachment implementation.

Evidence: [guest disks](wyrm2-disks.json), [mounted filesystem bytes](wyrm2-filesystems.tsv),
[deduplicated LVM allocation and PV matches](wyrm2-lvm.json),
[current OpenEBS volume objects](openebs-volumes.json).

## Unavailable live inventory

- **iguana and rugged:** Kubernetes Ready was Unknown; one bounded SSH probe per
  Nebula address timed out. Source declares encrypted btrfs root/local-path
  subvolumes. Live disks, free space, free extents and available hardware slots
  remain unverified. The reported node kernels are last-known Kubernetes values.
- **Atlas:** LAN SSH timed out. Nebula SSH reached the host but authentication
  was denied with strict host-key checking. Datastore capacity, physical media,
  actual VM disk mapping and attachment slots remain unverified.

See [reachability.tsv](reachability.tsv). No authentication policy or host-key
checks were relaxed, and no global SSH configuration was edited.

## Refresh method

Run from the repository Nix devshell with the normal authenticated kubeconfig
and talosconfig. Use each node's explicit endpoint; do not use an install or
rescue image. The representative read-only Talos queries were:

```sh
timeout 20s talosctl -e 10.42.0.15 -n 10.42.0.15 get disks -o json
timeout 20s talosctl -e 10.42.0.15 -n 10.42.0.15 get discoveredvolumes -o json
timeout 20s talosctl -e 10.42.0.15 -n 10.42.0.15 get mountstatus -o json
timeout 20s talosctl -e 10.42.0.15 -n 10.42.0.15 get volumestatus -o json
timeout 20s talosctl -e 10.42.0.15 -n 10.42.0.15 mounts
```

For Kubernetes, read Nodes, PVs, PVCs, Pods, CNPG Clusters, OpenEBS LVMVolumes,
StorageClasses and LINSTOR status; join PVCs by namespace/name **and UID**.
The committed projection excludes Pod environments, Secret objects and credentials.
Talos JSON contains a stream of resource objects; parse it using `jq -s`.

On wyrm2, use `lsblk --json --bytes`, `findmnt --json --bytes`, and read-only
`sudo -n pvs/vgs/lvs --reportformat json --units b --nosuffix`. Deduplicate LVs by
VG/name because the LVM segment report repeats striped LVs. Do not infer the
media of a virtio device from its guest rotational flag.

[capture.json](capture.json) identifies the collection window and source revision.
Full Pod specifications and unsanitized API responses were kept private; only
the selected inventory fields are committed. Refresh before any allocation.
