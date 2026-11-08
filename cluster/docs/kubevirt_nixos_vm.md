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
