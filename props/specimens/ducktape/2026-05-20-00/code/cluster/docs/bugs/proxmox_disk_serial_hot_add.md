# Bug: Proxmox disk hot-add drops serial parameter

## Summary

When adding a SCSI disk with `serial` set to a running VM via the `bpg/proxmox` Terraform
provider, Proxmox hot-plugs the disk via QMP `device_add` **without** the serial parameter.
The guest OS sees the disk but `/dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_<serial>` symlink
uses the `device_id` (e.g., `drive-scsi30`) instead of the custom serial string.

A full VM stop/start is required for the custom serial to appear in the QEMU command line.
However, even then, `virtio-scsi-single` uses `device_id=drive-scsi<N>` as the by-id
identifier, making the custom `serial` attribute effectively useless for disk identification.

## Observed behavior

1. Set `serial = "LONGHORN"` on a disk in `proxmox_virtual_environment_vm`
2. `tofu apply` hot-adds the disk to the running VM
3. Guest sees the disk (e.g., `/dev/sde`) but with NO serial
4. `/dev/disk/by-id/` shows `scsi-0QEMU_QEMU_HARDDISK_drive-scsi30` (device_id, not serial)
5. After VM stop/start: QEMU cmdline has `serial=LONGHORN` AND `device_id=drive-scsi30`
6. `/dev/disk/by-id/` still shows `scsi-0QEMU_QEMU_HARDDISK_drive-scsi30` (device_id wins)

## Workaround

Use the predictable `device_id`-based path instead of a custom serial:

```text
/dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_drive-scsi<N>
```

This is stable because we pin the disk to a fixed SCSI slot (`interface = "scsi30"`).

## Impact

Talos Linux `machine.disks` config using `/dev/disk/by-id/...` paths with custom serial
fails on boot if you use the serial string. Use the `drive-scsi<N>` device_id path instead.

## TODO

- [ ] Check if the `device_id` overriding `serial` in by-id naming is intentional QEMU
      behavior or a Proxmox-specific issue
- [ ] Consider reporting to `bpg/proxmox` that `serial` is not included in QMP hot-add
      (<https://github.com/bpg/terraform-provider-proxmox/issues>)
- [ ] Consider reporting to Proxmox that `device_id` takes precedence over `serial` for
      SCSI disk identification in the guest
