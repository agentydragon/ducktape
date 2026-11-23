# Wyrm2 storage inventory, 2026-10-19

Read-only observations collected at **07:08–07:16 UTC**, against source revision
`882f2c3ce72148cd463aaa86256972e5aac6ec25`. No nodes were rolled, devices
formatted, workloads stopped, volumes deleted, or configuration applied.
This snapshot is allocation evidence, not permission to reuse a device.

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
[current OpenEBS volume objects](openebs-volumes.json), and
[Wyrm2 PVs with their claims and Pod consumers](wyrm2-pv-consumers.tsv).
Its fields use `-` for absent values or no current consumers.

## Unavailable live inventory

- **iguana and rugged:** Kubernetes Ready was Unknown; one bounded SSH probe per
  Nebula address timed out. Source declares encrypted btrfs root/local-path
  subvolumes. Live disks, free space, free extents and available hardware slots
  remain unverified.
- **Atlas:** LAN SSH timed out. Nebula SSH reached the host but authentication
  was denied with strict host-key checking. Datastore capacity, physical media,
  actual VM disk mapping and attachment slots remain unverified.

No authentication policy or host-key checks were relaxed, and no global SSH
configuration was edited.

## Refresh method

Run from the repository Nix devshell with the normal authenticated kubeconfig.
For Kubernetes, read PVs, PVCs, Pods and OpenEBS LVMVolumes; join PVCs by
namespace/name **and UID**. The committed projection excludes Pod environments,
Secret objects and credentials.

On wyrm2, use `lsblk --json --bytes`, `findmnt --json --bytes`, and read-only
`sudo -n pvs/vgs/lvs --reportformat json --units b --nosuffix`. Deduplicate LVs by
VG/name because the LVM segment report repeats striped LVs. Do not infer the
media of a virtio device from its guest rotational flag.

[capture.json](capture.json) identifies the collection window and source revision.
Full Pod specifications and unsanitized API responses were kept private; only
the selected inventory fields are committed. Refresh before any allocation.
