"""Build Firecracker boot files from the pinned Debian cloud kernel package."""

from __future__ import annotations

import sys
import tarfile
from pathlib import Path

from agentplane.system_tests.vm.guest.boot_files import build_initramfs, read_kernel_package

_BUILTIN_CONFIG = {
    # Firecracker's ACPI tables discover MMIO devices when this driver is loaded.
    "CONFIG_ACPI": "y",
    "CONFIG_BLK_DEV_INITRD": "y",
    "CONFIG_CGROUPS": "y",
    "CONFIG_CGROUP_BPF": "y",
    "CONFIG_CGROUP_CPUACCT": "y",
    "CONFIG_CGROUP_DEVICE": "y",
    "CONFIG_CGROUP_FREEZER": "y",
    "CONFIG_CGROUP_PIDS": "y",
    "CONFIG_CGROUP_SCHED": "y",
    "CONFIG_CPUSETS": "y",
    "CONFIG_DEVTMPFS": "y",
    "CONFIG_EXT4_FS": "y",
    "CONFIG_MEMCG": "y",
    "CONFIG_MODULES": "y",
    "CONFIG_PROC_FS": "y",
    "CONFIG_SERIAL_8250": "y",
    "CONFIG_SERIAL_8250_CONSOLE": "y",
    "CONFIG_SYSFS": "y",
    "CONFIG_TMPFS": "y",
}
_MODULE_CONFIG = {
    "erofs": "CONFIG_EROFS_FS",
    "isofs": "CONFIG_ISO9660_FS",
    "virtio_blk": "CONFIG_VIRTIO_BLK",
    "virtio_mmio": "CONFIG_VIRTIO_MMIO",
    "virtio_net": "CONFIG_VIRTIO_NET",
    "vmw_vsock_virtio_transport": "CONFIG_VIRTIO_VSOCKETS",
    "vsock": "CONFIG_VSOCKETS",
}


def _busybox_bytes(busybox_tar: Path) -> bytes:
    with tarfile.open(busybox_tar) as archive:
        member = next(member for member in archive if member.name.endswith("bin/busybox"))
        stream = archive.extractfile(member)
        if stream is None:
            raise ValueError(f"{member.name=} is not a regular file")
        return stream.read()


def main() -> None:
    deb, busybox_tar, init, vmlinux_out, initramfs_out, config_out = map(Path, sys.argv[1:])
    kernel = read_kernel_package(deb)

    for name, expected in _BUILTIN_CONFIG.items():
        if kernel.config_options.get(name) != expected:
            raise ValueError(f"kernel config requires {name}={expected}, found {kernel.config_options.get(name)}")

    required_modules = []
    for module, symbol in _MODULE_CONFIG.items():
        value = kernel.config_options.get(symbol)
        if value == "m":
            required_modules.append(module)
        elif value != "y":
            raise ValueError(f"kernel config requires {symbol}=y or m, found {value}")

    vmlinux_out.write_bytes(kernel.vmlinuz)
    config_out.write_bytes(kernel.config)
    build_initramfs(kernel, _busybox_bytes(busybox_tar), init.read_bytes(), initramfs_out, required_modules)


if __name__ == "__main__":
    main()
