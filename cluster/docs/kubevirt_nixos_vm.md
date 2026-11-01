# KubeVirt NixOS VM Runbook

KubeVirt and CDI are installed by <../cdk8s/kubevirt/>. New VM root images use
OCI `containerDisk` images rather than CDI imports from SeaweedFS. See
<kubevirt_vm_image_artifacts.md> for the current image build and publishing
examples.

## Image and VM setup

1. Build the guest disk as an OCI image using an existing recipe such as
   <../../cpap/gateway/container-disk.nix> or
   <../../agentplane/env/images/container-disk.nix>.
2. Publish it to the Forgejo OCI registry from a GitHub Actions workflow; use
   the repository's image automation to update the manifest tag.
3. Set the VM root volume's `containerDisk.image` to that registry reference.
   Add the needed image pull credentials to the VM's namespace.
4. Keep durable guest state on a PVC or another durable store. A `containerDisk`
   root is ephemeral.
5. If image updates should roll out automatically, configure a restart path.
   KubeVirt does not change the disk of an already running VMI when only its
   template image changes.

The old SeaweedFS `vm-images` bucket and publisher are retired. The parked
`gecko` and `agent-box` CDI manifests remain as historical configs and cannot be
resumed until migrated to OCI images.

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

VMs that use `local-path-ovh-hdd` (or any local-path class) are tied to one node
and are not live-migratable; node loss means availability loss. For durable
VMs, use a CSI backend with RWX/block support and `VolumeSnapshotClass` before
relying on KubeVirt migration, snapshots, or node-failure recovery.
