"""Build the test guest's kernel and initramfs from the Debian cloud kernel package and busybox.

Usage: boot_files.py <kernel.deb> <busybox data tar> <init> <vmlinuz out> <initramfs out>

The initramfs holds a static busybox, the init script and the modules the guest needs that the cloud
kernel does not build in, decompressed, with `lib/modules/order` listing them dependencies first.
"""

import contextlib
import lzma
import re
import sys
import tarfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from devinfra.cpio.cpio_tool import write_archive

# virtio-pci, virtiofs and ext4 are built in.
_MODULES = ("virtio_blk", "virtio_net", "isofs", "erofs", "overlay", "button", "evdev")


@dataclass(frozen=True)
class KernelPackage:
    """Boot image and loadable modules extracted from a Debian kernel package."""

    vmlinuz: bytes
    config: bytes
    config_options: dict[str, str]
    compressed_modules: dict[str, bytes]
    builtins: frozenset[str]


@contextlib.contextmanager
def _deb_data(deb: Path) -> Iterator[tarfile.TarFile]:
    """The `data.tar.*` member of a .deb, which is an ar archive."""
    with deb.open("rb") as archive:
        if archive.read(8) != b"!<arch>\n":
            raise ValueError(f"{deb=} is not an ar archive")
        while header := archive.read(60):
            name, size = header[:16].decode().strip().rstrip("/"), int(header[48:58])
            if name.startswith("data.tar"):
                with tarfile.open(fileobj=archive, mode="r|*") as data:
                    yield data
                return
            archive.seek(size + size % 2, 1)
    raise ValueError(f"{deb=} has no data.tar member")


def _read(data: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    file = data.extractfile(member)
    if file is None:
        raise ValueError(f"{member.name=} is not a regular file")
    return file.read()


def _config_options(config: bytes) -> dict[str, str]:
    options: dict[str, str] = {}
    for line in config.decode().splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            name, value = line.split("=", 1)
            options[name] = value
        elif line.startswith("# CONFIG_") and line.endswith(" is not set"):
            options[line[2 : line.index(" is not set")]] = "n"
    return options


def read_kernel_package(deb: Path) -> KernelPackage:
    """Read the kernel image, config, built-in list, and modules from a Debian package."""
    vmlinuz = None
    config = None
    compressed_modules: dict[str, bytes] = {}
    builtins: set[str] = set()
    with _deb_data(deb) as data:
        for member in data:
            path = PurePosixPath(member.name)
            if path.parent.name == "boot" and path.name.startswith("vmlinuz-"):
                vmlinuz = _read(data, member)
            elif path.parent.name == "boot" and path.name.startswith("config-"):
                config = _read(data, member)
            elif path.name == "modules.builtin":
                builtins.update(
                    PurePosixPath(line.decode()).stem.replace("-", "_")
                    for line in _read(data, member).splitlines()
                    if line
                )
            elif path.name.endswith(".ko.xz"):
                compressed_modules[path.name.removesuffix(".ko.xz").replace("-", "_")] = _read(data, member)
    if vmlinuz is None or config is None:
        raise ValueError(f"{deb=} has no boot/vmlinuz-* or boot/config-*")
    return KernelPackage(
        vmlinuz=vmlinuz,
        config=config,
        config_options=_config_options(config),
        compressed_modules=compressed_modules,
        builtins=frozenset(builtins),
    )


def _depends(module: bytes) -> list[str]:
    match = re.search(rb"\x00depends=([^\x00]*)\x00", module)
    return [name.decode().replace("-", "_") for name in match.group(1).split(b",") if name] if match else []


def build_initramfs(
    kernel: KernelPackage, busybox: bytes, init: bytes, initramfs_out: Path, module_names: Sequence[str]
) -> None:
    """Write a busybox initramfs containing the requested modules and their dependencies."""
    modules: dict[str, bytes] = {}
    visiting: set[str] = set()

    def add(name: str) -> None:
        if name in modules or name in kernel.builtins:
            return
        if name in visiting:
            raise ValueError(f"cyclic kernel module dependency at {name}")
        if name not in kernel.compressed_modules:
            raise ValueError(f"kernel package has no module {name}.ko.xz")
        visiting.add(name)
        module = lzma.decompress(kernel.compressed_modules[name])
        for dependency in _depends(module):
            add(dependency)
        visiting.remove(name)
        modules[name] = module

    for name in module_names:
        add(name)

    write_archive(
        initramfs_out,
        {
            "init": (0o755, init),
            "bin/busybox": (0o755, busybox),
            "lib/modules/order": (0o644, "".join(f"{name}\n" for name in modules).encode()),
            **{f"lib/modules/{name}.ko": (0o644, module) for name, module in modules.items()},
        },
    )


def main() -> None:
    kernel_deb, busybox_tar, init, vmlinuz_out, initramfs_out = map(Path, sys.argv[1:])
    kernel = read_kernel_package(kernel_deb)
    with tarfile.open(busybox_tar) as data:
        busybox = _read(data, next(member for member in data if member.name.endswith("bin/busybox")))
    vmlinuz_out.write_bytes(kernel.vmlinuz)
    build_initramfs(kernel, busybox, init.read_bytes(), initramfs_out, _MODULES)


if __name__ == "__main__":
    main()
