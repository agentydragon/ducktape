# Firecracker Dev VMs — TODO

## Architecture

The pod is infrastructure-only: Firecracker process + networking + port
proxies. The manager is the VMM brain — it creates PVCs + pods, then
drives boot/restore through the Firecracker API proxy on each pod.

Storage: LVM thin provisioning via OpenEBS LVM LocalPV on wyrm2. Rootfs
PVCs are `volumeMode: Block` (Firecracker uses raw LV as drive). Work
and snapshot PVCs are `volumeMode: Filesystem` (Firecracker snapshot API
requires regular files — uses `ftruncate` + `metadata().len()`). All
cloning is CoW via LVM thin snapshots. No PVC sharing between VMs.

See <DESIGN.md> for full architecture, <vm_alternatives.md> for decision
rationale.

## Next Steps (in priority order)

1. **Block-mode StorageClass for LVM**: OpenEBS LVM LocalPV is deployed
   on wyrm2 (VG: `openebs-lvmvg`, SC: `lvm-proxmox`). Need a second SC
   `lvm-proxmox-block` for `volumeMode: Block` PVCs (same VG, no fstype).
   Also ensure the VG uses thin provisioning (`lvcreate --thinpool`).
   _Owner: manual setup in devel, not on this branch._

2. **Base rootfs LV**: Create the base thin LV, then run
   `./devinfra/firecracker/provision-rootfs.sh` on wyrm2 to build
   the NixOS rootfs via Nix and `dd` it into the LV.

3. **Rootfs boot test on wyrm2**: Boot kernel + initramfs + rootfs with
   raw Firecracker CLI on wyrm2 (has `/dev/kvm`). Verify process_api
   starts, pivot_roots to NixOS, WebSocket listener accepts connections.
   First real end-to-end validation.

4. **Manager auth token Secret**: Create `firecracker-manager-auth-token`
   via SOPS in `claude-sandbox`.

5. **Firecracker binary in VM pod**: Verify `@firecracker_release`
   http_archive extracts correctly and `firecracker_layer` pkg_tar
   places the binary at `/usr/local/bin/firecracker`.

6. **Reconciliation loop**: Manager watches pod status, drives boot when
   a pod reaches Running, handles pod failures/restarts, cleans up
   orphaned PVCs. Currently boot is a manual `POST /vms/{id}/boot`.
   Also drives restore-boot for pods created via
   `POST /snapshots/{name}/restore`.

## Done

- [x] Initramfs with process_api (3.2MB binary, cpio archive, OCI layer)
- [x] Networking aligned with process_api (192.0.2.0/24)
- [x] Dumb entrypoint (Firecracker + TAP/NAT + 3 TCP proxies)
- [x] Smart manager (boot/restore via Firecracker API proxy)
- [x] Typed client classes (FirecrackerClient, ProcessApiControl, K8sVMClient)
- [x] process_api WS client (streaming async, typed protocol)
- [x] Snapshot workflow (freeze → pause → snapshot → thaw → resume)
- [x] Restore via CoW PVC cloning (rootfs + snapshot + work, no sharing)
- [x] Per-VM PVCs: rootfs (Block), work (Filesystem), snapshot clone (Filesystem)
- [x] OCI image push targets in CI (manager + vm_pod)
- [x] Rootfs provisioning script (Nix build + dd on wyrm2)
- [x] NixOS rootfs config (Bazel, Python, JDK, no /init symlink)
- [x] K8s manifests (manager deployment, RBAC, KVM device plugin)
- [x] Storage design: LVM thin (block device findings, volumeMode selection)

## Future

- [ ] Warm snapshot script: boot VM, clone repo,
      `bazel query 'tests(//...)'`, snapshot. Automate as manager endpoint.
- [ ] Restore fixups: re-seed entropy, update clock, refresh tokens.
- [ ] Auto-cleanup: reconciler GCs Failed/Completed pods + orphaned PVCs.
- [ ] Multiple VM support: allocate from subnet pool instead of fixed
      192.0.2.0/24.
- [ ] CRD controller: `FirecrackerVM` CRD if this grows beyond prototype.
