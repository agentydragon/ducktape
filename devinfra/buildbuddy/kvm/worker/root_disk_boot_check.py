"""Boot a disposable containerDisk copy and verify cloud-init expands GPT root and ext4."""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

EXPECTED_DISK_BYTES = 5 * 1024**3
GIB = 1024**3


def run(*args: str, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout)


def partition_one(raw_disk: Path) -> tuple[int, int]:
    with raw_disk.open("rb") as disk:
        disk.seek(512)
        header = disk.read(512)
        if header[:8] != b"EFI PART":
            raise AssertionError("missing GPT header")
        entries_lba = struct.unpack_from("<Q", header, 72)[0]
        count, entry_size = struct.unpack_from("<II", header, 80)
        if count < 1 or entry_size < 128:
            raise AssertionError(f"invalid GPT entry geometry: {count=}, {entry_size=}")
        disk.seek(entries_lba * 512)
        entries = disk.read(count * entry_size)
    first_entry = entries[:entry_size]
    if first_entry[:16] == bytes(16):
        raise AssertionError("GPT partition 1 is empty")
    start_lba, end_lba = struct.unpack_from("<QQ", first_entry, 32)
    return start_lba, end_lba


def ext4_size(raw_disk: Path, start_lba: int) -> int:
    with raw_disk.open("rb") as disk:
        disk.seek(start_lba * 512 + 1024)
        superblock = disk.read(1024)
    if struct.unpack_from("<H", superblock, 56)[0] != 0xEF53:
        raise AssertionError("partition 1 does not contain an ext4 superblock")
    block_count = struct.unpack_from("<I", superblock, 4)[0]
    block_size = 1024 << struct.unpack_from("<I", superblock, 24)[0]
    return block_count * block_size


def main(disk_image: Path, output: Path, ovmf: Path) -> None:
    size = json.loads(run("qemu-img", "info", "--output=json", str(disk_image)).stdout)["virtual-size"]
    if size != EXPECTED_DISK_BYTES:
        raise AssertionError(f"expected 5 GiB virtual disk, got {size} bytes")

    with tempfile.TemporaryDirectory(prefix="buildbuddy-root-resize-") as temporary:
        temp = Path(temporary)
        overlay = temp / "overlay.qcow2"
        seed = temp / "seed.iso"
        user_data = temp / "user-data"
        meta_data = temp / "meta-data"
        network_config = temp / "network-config"
        serial_log = temp / "serial.log"
        raw_disk = temp / "expanded.raw"
        ovmf_vars = temp / "OVMF_VARS.fd"

        run("qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", str(disk_image), str(overlay))
        user_data.write_text("#cloud-config\npower_state:\n  mode: poweroff\n  delay: now\n")
        meta_data.write_text("instance-id: buildbuddy-worker-root-resize-check\nlocal-hostname: bb-worker-check\n")
        network_config.write_text(
            "version: 2\n"
            "ethernets:\n"
            "  worker-test-nic:\n"
            "    match:\n"
            "      name: 'en*'\n"
            "    dhcp4: true\n"
            "    dhcp6: false\n"
            "    optional: true\n"
        )
        run("cloud-localds", "--network-config", str(network_config), str(seed), str(user_data), str(meta_data))
        shutil.copyfile(ovmf / "FV/OVMF_VARS.fd", ovmf_vars)

        qemu = shutil.which("qemu-system-x86_64")
        if qemu is None:
            raise AssertionError("qemu-system-x86_64 is not available")
        try:
            subprocess.run(
                [
                    qemu,
                    "-machine",
                    "q35,accel=tcg",
                    "-m",
                    "2048",
                    "-smp",
                    "2",
                    "-display",
                    "none",
                    "-monitor",
                    "none",
                    "-serial",
                    f"file:{serial_log}",
                    "-no-reboot",
                    "-nic",
                    # QEMU user networking is restricted: DHCP works for a quick clean boot, while guest
                    # packets cannot reach the host or external network.
                    "user,restrict=on,model=virtio-net-pci",
                    "-drive",
                    f"if=pflash,format=raw,readonly=on,file={ovmf / 'FV/OVMF_CODE.fd'}",
                    "-drive",
                    f"if=pflash,format=raw,file={ovmf_vars}",
                    "-drive",
                    f"file={overlay},format=qcow2,if=virtio",
                    "-drive",
                    f"file={seed},format=raw,if=virtio,readonly=on",
                ],
                check=True,
                timeout=180,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            serial = serial_log.read_text(errors="replace") if serial_log.exists() else "<no serial output>"
            stderr = error.stderr
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            raise AssertionError(
                f"QEMU boot/shutdown failed: {error}\n"
                f"serial log tail:\n{serial[-12000:]}\n"
                f"QEMU stderr tail:\n{(stderr or '')[-4000:]}"
            ) from error

        run("qemu-img", "convert", "-q", "-f", "qcow2", "-O", "raw", str(overlay), str(raw_disk))
        start_lba, end_lba = partition_one(raw_disk)
        partition_bytes = (end_lba - start_lba + 1) * 512
        filesystem_bytes = ext4_size(raw_disk, start_lba)
        if partition_bytes < 39 * GIB // 10:
            raise AssertionError(f"cloud-init left root partition too small: {partition_bytes} bytes")
        if filesystem_bytes < 39 * GIB // 10:
            raise AssertionError(f"cloud-init left ext4 too small: {filesystem_bytes} bytes")
        if (end_lba + 34) * 512 < EXPECTED_DISK_BYTES - 1024**2:
            raise AssertionError(f"root partition does not extend to the end of the disk: {end_lba=}")

        output.write_text(
            "First boot expanded the final GPT root partition and ext4 filesystem.\n"
            f"virtual_disk_bytes={size}\n"
            f"root_partition_bytes={partition_bytes}\n"
            f"root_ext4_bytes={filesystem_bytes}\n"
        )


if __name__ == "__main__":
    import sys

    main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
