# Wyrm2 SSD model volume, 2026-09-24

Applied online to VM 110 on Atlas:

- Existing `virtio8`, `local-zfs:vm-110-disk-7`, grew from 500 to 1,024 GiB.
- Existing ext4 filesystem on `/dev/vdi` grew online and moved from
  `/var/lib/colibri` to `/var/lib/llm-models-ssd`.
- Filesystem UUID remained `0797d95c-a8e3-4768-bcfc-1009d0b05828`.
- Afterward: 1,007 GiB filesystem, 447 GiB used, 515 GiB available.
- All 636 relative file paths, inode numbers, and sizes matched before/after.
  This checks identity and inventory, not a full content checksum.

Terraform's existing `ignore_changes = [disk]` deliberately protects legacy CSI
attachments; bootstrap cannot apply this disk edit. Used the scoped Atlas command
`qm disk resize 110 virtio8 1024G`, then `resize2fs /dev/vdi` in the guest.
The Terraform declaration records the resulting size; the ignore rule stays intact.

Built the NixOS configuration and staged it with `nixos-rebuild boot`, without
activating the whole system. Compared the generated fstab and tmpfiles definitions:
only the model mount path changed. Installed those two files from the built
generation for the current session. Mounted the same filesystem at the new path,
verified the inventory, then normally unmounted the old path and reloaded systemd's
unit definitions. Removed the empty old mountpoint; no compatibility symlink.

No VM reboot, attachment replacement, full NixOS switch, model deletion, or archival.
Atlas's QEMU PID, the guest boot ID, running NixOS generation, and the SSH, Nebula,
NetworkManager, display-manager, and kubelet PIDs/activation timestamps were unchanged.
The next normal boot uses the staged configuration; the running generation remains
the pre-change one with the two mount-related files applied separately.
