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

1. **Base rootfs LV**: Create the base thin LV, then run
   `./devinfra/firecracker/provision-rootfs.sh` on wyrm2 to build
   the NixOS rootfs via Nix and `dd` it into the LV.

2. **Rootfs boot test on wyrm2**: Boot kernel + initramfs + rootfs with
   raw Firecracker CLI on wyrm2 (has `/dev/kvm`). Verify process_api
   starts, pivot_roots to NixOS, WebSocket listener accepts connections.
   First real end-to-end validation.

3. **Reconciliation loop**: Manager watches pod status, drives boot when
   a pod reaches Running, handles pod failures/restarts, cleans up
   orphaned PVCs. Currently boot is a manual `POST /vms/{id}/boot`.
   Also drives restore-boot for pods created via
   `POST /snapshots/{name}/restore`.

## Future

- [ ] Warm snapshot script: boot VM, clone repo,
      `bazel query 'tests(//...)'`, snapshot. Automate as manager endpoint.
- [ ] Restore fixups: re-seed entropy, update clock, refresh tokens.
- [ ] Auto-cleanup: reconciler GCs Failed/Completed pods + orphaned PVCs.
- [ ] Multiple VM support: allocate from subnet pool instead of fixed
      192.0.2.0/24.
- [ ] CRD controller: `FirecrackerVM` CRD if this grows beyond prototype.
