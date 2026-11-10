# Exercise cloud-init's existing growpart + resizefs behavior on first boot of the resized disk.
{
  pkgs,
  disk,
}:
pkgs.runCommand "buildbuddy-kvm-worker-root-disk-check"
  {
    nativeBuildInputs = [
      pkgs.cloud-utils
      pkgs.python3
      pkgs.qemu_kvm
    ];
  }
  ''
    python3 ${./root_disk_boot_check.py} ${disk}/disk/disk.qcow2 "$out" ${pkgs.OVMF.fd}
  ''
