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
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

from devinfra.cpio.cpio_tool import write_archive

# virtio-pci, virtiofs and ext4 are built in.
_MODULES = ("virtio_blk", "virtio_net", "isofs", "erofs", "overlay", "button", "evdev")


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


def _depends(module: bytes) -> list[str]:
    match = re.search(rb"\x00depends=([^\x00]*)\x00", module)
    return [name.decode() for name in match.group(1).split(b",") if name] if match else []


def main() -> None:
    kernel_deb, busybox_tar, init, vmlinuz_out, initramfs_out = map(Path, sys.argv[1:])
    vmlinuz = None
    compressed: dict[str, bytes] = {}
    with _deb_data(kernel_deb) as data:
        for member in data:
            path = PurePosixPath(member.name)
            if path.parent.name == "boot" and path.name.startswith("vmlinuz-"):
                vmlinuz = _read(data, member)
            elif path.name.endswith(".ko.xz"):
                compressed[path.name.removesuffix(".ko.xz")] = _read(data, member)
    if vmlinuz is None:
        raise ValueError(f"{kernel_deb=} has no boot/vmlinuz-*")

    modules: dict[str, bytes] = {}

    def add(name: str) -> None:
        if name not in modules:
            module = lzma.decompress(compressed[name])
            for dependency in _depends(module):
                add(dependency)
            modules[name] = module

    for name in _MODULES:
        add(name)

    with tarfile.open(busybox_tar) as data:
        busybox = _read(data, next(member for member in data if member.name.endswith("bin/busybox")))

    vmlinuz_out.write_bytes(vmlinuz)
    write_archive(
        initramfs_out,
        {
            "init": (0o755, init.read_bytes()),
            "bin/busybox": (0o755, busybox),
            "lib/modules/order": (0o644, "".join(f"{name}\n" for name in modules).encode()),
            **{f"lib/modules/{name}.ko": (0o644, module) for name, module in modules.items()},
        },
    )


if __name__ == "__main__":
    main()
