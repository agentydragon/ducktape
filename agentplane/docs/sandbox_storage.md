# Sandbox storage layouts

A Sandbox owns its retained data independently of its current Pod or VM. Ownership does not make
the contents portable: a directory tree, a disk image file, and a filesystem on a block device
require different attachments. Changing the workload kind must preserve both the runner's native
state and the workspace, or refuse the change before starting an incompatible workload.

## Roles, layouts and attachments

`stateVolume` holds runner journals and native harness persistence. `workspaceVolume` holds the
agent's working files independently of that state. For templates declaring `workspaceVolume`, the
Sandbox owns both PVCs; stopping or replacing an incarnation never deletes them. Deleting the Sandbox removes its owned PVCs after quiesce.

`workspaceVolume.layout` declares the contents, separately from the claim's StorageClass and
`volumeMode`:

| Layout            | PVC mode     | Contents                                                            | Pod attachment                                                | VM attachment       |
| ----------------- | ------------ | ------------------------------------------------------------------- | ------------------------------------------------------------- | ------------------- |
| `DirectoryTree`   | `Filesystem` | Ordinary files and directories                                      | Volume mount                                                  | virtiofs filesystem |
| `RawDiskImage`    | `Filesystem` | Raw `disk.img`, containing ext4 directly, without a partition table | Unsupported; conversion required                              | Virtual disk        |
| `BlockFilesystem` | `Block`      | ext4 directly on the device, without a partition table              | Unsupported; explicit mounting support or conversion required | Virtual disk        |

`DirectoryTree` has no `filesystem` member: the provisioner owns its backing filesystem.
`RawDiskImage` and `BlockFilesystem` require `filesystem: ext4`; other filesystems and partition
layouts are not supported. Unknown or inconsistent declarations are rejected. The template's
workload must attach the named volume in the way the layout permits. Pod containers mount its
name explicitly. VM disk layouts require a disk device of that name; for `DirectoryTree` the
controller adds a virtiofs filesystem. The guest must mount the corresponding disk or virtiofs tag:
the checked-in runner guest currently expects a disk workspace, so a VM directory-tree workspace
also needs suitable guest configuration before activation.

The state volume has always used the fixed `DirectoryTree` contract: a Pod mounts its PVC and a VM
mounts it through virtiofs at `/state`. The accepting controller adds optional
`stateVolume.layout`; omission remains `DirectoryTree` for existing templates, and this release
accepts only an explicit `DirectoryTree` on a `Filesystem` claim. It records the layout on the PVC
and pins that PVC's name, UID and layout in `Sandbox.status.volumes.state` before attaching it.

The first accepting controller also records the historical `DirectoryTree` contract for an
existing controller-owned filesystem state PVC that has no record yet. It derives that legacy
layout from the old controller contract and the PVC itself, never from the mutable template. A
foreign or deleting PVC, a non-filesystem claim, an invalid record, a lost record after status has
bound the PVC, or a changed PVC UID blocks workload start without replacing the claim. These
records make later explicit layout changes fail closed; they do not convert or inspect volume
contents. Do not declare `stateVolume.layout` in templates until the accepting CRD schema and
controller images are pinned. The later fallback-removal step makes the field required after all
templates have explicit declarations and all accepting images are pinned. A block StorageClass
alone does not enable another state layout.

Kubernetes presents a `Filesystem` claim as a mounted directory and a `Block` claim as a device.
For a filesystem PVC attached as a KubeVirt disk, KubeVirt creates or opens a raw file named
`disk.img`. A Pod mounting that PVC ordinarily sees the image file, not the files inside it.
See [Kubernetes volume modes](https://kubernetes.io/docs/concepts/storage/persistent-volumes/#volume-mode)
and [KubeVirt PVC disks](https://kubevirt.io/user-guide/storage/disks_and_volumes/#persistentvolumeclaim).

## Template examples

These are workspace excerpts; the surrounding template still declares its Pod or VM and state
volume. They do not change deployed templates or existing PVCs.

After the accepting CRD schema and controller images are pinned, a state volume can declare its
currently supported layout explicitly:

```yaml
spec:
  stateVolume:
    name: state
    layout:
      type: DirectoryTree
    claim:
      storageClassName: local-path-ovh-hdd
      volumeMode: Filesystem
      accessModes: [ReadWriteOnce]
      resources:
        requests:
          storage: 20Gi
```

A Pod workspace containing ordinary files:

```yaml
spec:
  workspaceVolume:
    name: workspace
    layout:
      type: DirectoryTree
    claim:
      storageClassName: lvm-proxmox-ssd
      volumeMode: Filesystem
      accessModes: [ReadWriteOnce]
      resources:
        requests:
          storage: 20Gi
```

A VM workspace backed by a disk image on a filesystem PVC:

```yaml
spec:
  workspaceVolume:
    name: workspace
    layout:
      type: RawDiskImage
      filesystem: ext4
    claim:
      storageClassName: local-path-ovh-hdd
      volumeMode: Filesystem
      accessModes: [ReadWriteOnce]
      resources:
        requests:
          storage: 20Gi
```

A VM workspace backed by a block device:

```yaml
spec:
  workspaceVolume:
    name: workspace
    layout:
      type: BlockFilesystem
      filesystem: ext4
    claim:
      storageClassName: lvm-proxmox-ssd-block
      volumeMode: Block
      accessModes: [ReadWriteOnce]
      resources:
        requests:
          storage: 20Gi
```

The Proxmox class names illustrate existing provisioner choices, not availability on every node.
A node-local volume retains its node affinity when its workload stops. Making node-local,
capacity-enforcing storage available across the cluster is independent of choosing a layout.

## Retained volume identity

The template describes what to provision; it is not evidence of what an existing PVC contains.
The controller records a new workspace's layout durably with its creation and binds the retained
workspace to that PVC's identity. The creation-time
`agentplane.allegedly.works/workspace-record` annotation records the declared layout and claim;
`status.volumes.workspace` projects its `name`, `uid` and `layout`. Restarting the controller must
recover the recorded contract,
not copy the current template onto an unclassified existing PVC.

A template change that disagrees with the retained layout is `VolumeLayoutMismatch`. A foreign or
deleting PVC is a conflict. Reusing a name does not make a replacement PVC the old volume. Neither
a workload-kind switch nor a StorageClass or `volumeMode` edit converts existing contents. A
retained workspace must not disappear from the workload merely because its new template omits
the workspace declaration.

The recorded layout is a provisioning contract, not a claim that formatting or mounting has
succeeded. Guest initialization checks the declared filesystem before using a disk; the controller
does not inspect or repair filesystems through the Kubernetes API.

## Initial rollout

The operator accepts an incompatible cutoff dropping existing Sandboxes rather than migrating
their old storage layouts. The [task DAG](../plans/task_dag.md) separates the controller/image pin,
testing comparison, a data-drop PR listing the affected Sandboxes and owned volumes, template
activation, and retirement of the losing DataVolume/PVC path. #10440 implements the contract and
does not delete existing data. The template activation uses the accepted controller and guest pins;
refresh both namespace inventories immediately before applying it. The cutoff is not a policy of
automatically deleting idle Sandboxes.

## Formatting and conversion

The VM guest initializes a new disk and mounts the filesystem. The existing guest accepts ext4
labelled `APWORKSPACE`; otherwise it scans the disk for nonzero contents before formatting it.
Replacing that shell with declarative NixOS facilities is separate from PVC ownership. NixOS
`fileSystems.*.autoFormat` tests for recognizable filesystem content, not that every byte is zero;
the initialization contract must be tested rather than assuming those checks are interchangeable.
Existing data is never reformatted merely because the desired layout changes.

Conversion is an accepted option for much later, tracked separately as `VOLUME_LAYOUT_CONVERSION`
in the [task DAG](../plans/task_dag.md#volume_layout_conversion--explicit-offline-volume-conversion).
It does not gate workspace ownership or template rollout; incompatible changes are rejected until
a converter exists. A future conversion must:

- Require a suspended Sandbox, with every old writer terminated before mounting either volume.
- Allocate a destination with an explicit layout, preserving files, ownership, permissions, links
  and any filesystem metadata the source data needs.
- Verify the copied data and the destination's native mount before switching the recorded binding.
- Survive interruption without selecting a partial destination or losing the source.
- Keep the source until its cleanup is explicitly authorized; account for both volumes in quota.

Conversion is separate from first-use formatting and from runner-internal journal schema changes.
Using the same ext4 filesystem type in two layouts does not remove the need for conversion.
