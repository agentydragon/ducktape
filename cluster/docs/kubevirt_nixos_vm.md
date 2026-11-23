# KubeVirt NixOS VM Runbook

KubeVirt and CDI are installed by <../cdk8s/kubevirt/>.

## Image and VM setup

Follow <kubevirt_vm_image_artifacts.md> for OCI `containerDisk` build/publishing
recipes, durable-state placement, image-update restart requirements, and the
retired SeaweedFS/parked-CDI migration boundary. Set the VM root volume's
`containerDisk.image` to the published registry reference and add its image pull
credentials to the VM's namespace. Use the repository's
[image automation](container-images.md#adding-a-new-container-image) to update manifest tags.

## Verification

```bash
# Guest agent boot check (also exposes IP, hostname, kernel)
kubectl -n <ns> get vmi <name> -o jsonpath='{.status.guestOSInfo}{"\n"}'

# SSH via port-forward (use before public exposure is wired)
kubectl -n <ns> port-forward svc/<name>-ssh 2222:22
ssh -p 2222 agentydragon@127.0.0.1
```

For the former gecko public SSH example, see
<../parked/gecko/app/ciliumenvoyconfig.yaml>. It is parked with that VM and is
not a current deployment recipe.

## Caveats

VMs that use node-local storage are tied to one node
and are not live-migratable; node loss means availability loss. For durable
VMs, use a CSI backend with RWX/block support and `VolumeSnapshotClass` before
relying on KubeVirt migration, snapshots, or node-failure recovery.

## What KubeVirt does and does not provide

KubeVirt was chosen as the existing add-on for persistent VMs in this cluster. The alternatives:
Harvester is a packaged but separate VMware-like platform with its own UI, Longhorn storage and
backups; OpenShift Virtualization is practical only on OpenShift; Virtink is too immature (live
migration is roadmap-only); Kata is a VM-backed container runtime, not a persistent VM platform; raw
Firecracker or Cloud Hypervisor is the right primitive for sandboxes forked from warm memory
snapshots but means a custom orchestrator (<../../devinfra/x/firecracker/vm_alternatives.md>).

- **No durable suspend.** Pause stops CPU and IO but leaves the memory allocated on the node, and the
  memory dump feature is for diagnostics only. A durable stop is a guest shutdown and a later boot.
- **Snapshots are storage snapshots.** They need a `VolumeSnapshotClass`, and a restore needs the VM
  target stopped.
- **Live migration** needs a migratable RWX disk and only helps for planned evictions.
- **Sudden node loss** is a restart from replicated storage with the RAM lost. KubeVirt can take up
  to about five minutes to mark an unresponsive virt-handler path unhealthy.
- **Not on SeaweedFS FUSE.** Its semantics and observed fsync latency rule it out for VM disks.
- **Not yet validated:** snapshot, live migration and node-loss recovery on the Piraeus classes.
