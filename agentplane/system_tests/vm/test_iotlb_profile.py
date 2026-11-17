"""Reject probe recipes that do not describe the executable being instrumented."""

import hashlib
import struct

import pytest

from agentplane.system_tests.vm import qemu_elf
from agentplane.system_tests.vm.iotlb_profile import Probe, Profile


def executable() -> bytes:
    image = bytearray(512)
    struct.pack_into("<16sHHIQQQIHHHHHH", image, 0, b"\x7fELF\x02\x01", 2, 62, 1, 0, 0, 256, 0, 64, 0, 0, 64, 4, 1)
    names = b"\0.shstrtab\0.text\0.note.gnu.build-id\0"
    image[64 : 64 + len(names)] = names
    image[128:132] = b"\x90\x90\xc3\0"
    note = struct.pack("<III", 4, 4, 3) + b"GNU\0\x12\x34\x56\x78"
    image[160 : 160 + len(note)] = note
    for index, (name, kind, flags, address, offset, size) in enumerate(
        [(0, 0, 0, 0, 0, 0), (1, 3, 0, 0, 64, len(names)), (11, 1, 6, 0x400080, 128, 4), (17, 7, 0, 0, 160, len(note))]
    ):
        struct.pack_into("<IIQQQQIIQQ", image, 256 + index * 64, name, kind, flags, address, offset, size, 0, 0, 1, 0)
    return bytes(image)


def profile(binary: bytes) -> Profile:
    return Profile(
        build_id="12345678",
        supplementary_build_id="test-shared",
        binary_sha256=hashlib.sha256(binary).hexdigest(),
        probes=[Probe(name="failure", address=0x400080, offset=128, instruction="9090", arguments={})],
    )


def test_offsets_use_elf_mapping_and_probe_bytes() -> None:
    binary = executable()
    profile(binary).verify(binary)
    assert qemu_elf.text_offset(binary, 0x400082) == 130
    with pytest.raises(ValueError, match="outside"):
        qemu_elf.text_offset(binary, 0x400084)


@pytest.mark.parametrize("change", ["identity", "offset", "instruction"])
def test_rejects_incompatible_executable_or_recipe(change: str) -> None:
    binary = executable()
    expected = profile(binary)
    if change == "identity":
        binary = binary[:128] + b"\xcc" + binary[129:]
    elif change == "offset":
        expected.probes[0].offset += 1
    else:
        expected.probes[0].instruction = "cc"
    with pytest.raises(ValueError, match="mismatch"):
        expected.verify(binary)
