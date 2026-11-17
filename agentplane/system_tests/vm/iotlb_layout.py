"""Extract the exact downstream QEMU DWARF used to validate IOTLB probe offsets."""

import argparse
import io
import struct
import tarfile
from pathlib import Path

import zstandard
from pydantic import BaseModel

from agentplane.system_tests.vm import iotlb_profile, qemu_elf

DEBUG_PATH = "usr/lib/debug/usr/libexec/qemu-kvm-10.1.0-10.el9.x86_64.debug"
SHARED_PATH = "usr/lib/debug/.dwz/qemu-kvm-10.1.0-10.el9.x86_64"


def extract_debug(rpm: bytes, path: str = DEBUG_PATH) -> bytes:
    if rpm[:4] != b"\xed\xab\xee\xdb":
        raise ValueError("Invalid RPM lead")
    offset = 96
    for index in range(2):
        magic, entries, size = struct.unpack_from(">8sII", rpm, offset)
        if magic != b"\x8e\xad\xe8\x01\0\0\0\0":
            raise ValueError("Invalid RPM header")
        offset += 16 + entries * 16 + size
        if offset > len(rpm):
            raise ValueError("Truncated RPM header")
        if index == 0:
            offset = (offset + 7) & ~7
    with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(rpm[offset:])) as stream:
        archive = stream.read(128 * 1024 * 1024 + 1)
        if len(archive) > 128 * 1024 * 1024:
            raise ValueError("RPM payload exceeds extraction budget")
    offset = 0
    while offset < len(archive):
        if len(archive) - offset < 110 or archive[offset : offset + 6] != b"070701":
            raise ValueError("Invalid newc header")
        size = int(archive[offset + 54 : offset + 62], 16)
        namesize = int(archive[offset + 94 : offset + 102], 16)
        if not 1 <= namesize <= 4096 or offset + 110 + namesize > len(archive):
            raise ValueError("Invalid newc filename")
        name = archive[offset + 110 : offset + 110 + namesize - 1].decode()
        start = (offset + 110 + namesize + 3) & ~3
        if start + size > len(archive):
            raise ValueError("Truncated newc member")
        if name.removeprefix("./") == path:
            return archive[start : start + size]
        offset = (start + size + 3) & ~3
    raise ValueError("QEMU DWARF absent from RPM")


class Descriptor(BaseModel):
    digest: str


class Index(BaseModel):
    manifests: list[Descriptor]


class Manifest(BaseModel):
    layers: list[Descriptor]


def image_binary(layout: Path) -> bytes:
    def blob(digest: str) -> Path:
        algorithm, value = digest.split(":", 1)
        if algorithm != "sha256":
            raise ValueError("Expected OCI SHA-256 blob")
        return layout / "blobs" / algorithm / value

    index = Index.model_validate_json((layout / "index.json").read_bytes())
    if len(index.manifests) != 1:
        raise ValueError("Expected one amd64 launcher manifest")
    manifest = Manifest.model_validate_json(blob(index.manifests[0].digest).read_bytes())
    binary: bytes | None = None
    for layer in manifest.layers:
        with tarfile.open(blob(layer.digest)) as archive:
            for member in archive:
                name = member.name.removeprefix("./")
                if name in {"usr/libexec/.wh.qemu-kvm", "usr/libexec/.wh..wh..opq"}:
                    binary = None
                if name == "usr/libexec/qemu-kvm":
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ValueError("QEMU OCI member is not a file")
                    if member.size > 64 * 1024 * 1024:
                        raise ValueError("QEMU OCI member exceeds binary budget")
                    binary = stream.read()
    if binary is None:
        raise ValueError("QEMU absent from launcher OCI layout")
    return binary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rpm", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("shared_rpm", type=Path)
    parser.add_argument("shared_output", type=Path)
    parser.add_argument("image", type=Path)
    parser.add_argument("profile", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("apic_profile", type=Path)
    parser.add_argument("apic_manifest", type=Path)
    args = parser.parse_args()
    profile = iotlb_profile.Profile.model_validate_json(args.profile.read_bytes())
    debug = extract_debug(args.rpm.read_bytes())
    if qemu_elf.build_id(debug) != profile.build_id:
        raise ValueError("QEMU debuginfo build-id mismatch")
    if not {".debug_info", ".debug_loc", ".debug_line"} <= qemu_elf.section_headers(debug).keys():
        raise ValueError("QEMU DWARF incomplete")
    shared = extract_debug(args.shared_rpm.read_bytes(), SHARED_PATH)
    expected = qemu_elf.section(debug, ".gnu_debugaltlink").split(b"\0", 1)[1].hex()
    if qemu_elf.build_id(shared) != expected or expected != profile.supplementary_build_id:
        raise ValueError("QEMU supplementary DWARF build-id mismatch")
    binary = image_binary(args.image)
    profile.verify(binary)
    apic_profile = iotlb_profile.Profile.model_validate_json(args.apic_profile.read_bytes())
    if (
        apic_profile.build_id != profile.build_id
        or apic_profile.supplementary_build_id != profile.supplementary_build_id
    ):
        raise ValueError("APIC and IOTLB QEMU debuginfo identities differ")
    apic_profile.verify(binary)
    args.manifest.write_text(profile.model_dump_json(indent=2) + "\n")
    args.apic_manifest.write_text(apic_profile.model_dump_json(indent=2) + "\n")
    args.output.write_bytes(debug)
    args.shared_output.write_bytes(shared)


if __name__ == "__main__":
    main()
