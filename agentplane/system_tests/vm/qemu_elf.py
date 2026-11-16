"""Read the ELF identity and file offsets needed for pinned QEMU uprobes."""

import struct


def section_headers(elf: bytes) -> dict[str, tuple[int, ...]]:
    if elf[:6] != b"\x7fELF\x02\x01":
        raise ValueError("Expected ELF64 little endian")
    header = struct.unpack_from("<16sHHIQQQIHHHHHH", elf)
    entries = [struct.unpack_from("<IIQQQQIIQQ", elf, header[6] + index * header[11]) for index in range(header[12])]
    names = entries[header[13]]
    strings = elf[names[4] : names[4] + names[5]]
    return {strings[entry[0] :].split(b"\0", 1)[0].decode(): entry for entry in entries}


def section(elf: bytes, name: str) -> bytes:
    entry = section_headers(elf)[name]
    return elf[entry[4] : entry[4] + entry[5]]


def build_id(elf: bytes) -> str:
    note = section(elf, ".note.gnu.build-id")
    namesize, size, kind = struct.unpack_from("<III", note)
    if kind != 3 or note[12 : 12 + namesize] != b"GNU\0":
        raise ValueError("Invalid GNU build-id note")
    offset = 12 + ((namesize + 3) & ~3)
    return note[offset : offset + size].hex()


def text_offset(elf: bytes, address: int) -> int:
    text = section_headers(elf)[".text"]
    if text[1] != 1 or not text[3] <= address < text[3] + text[5]:
        raise ValueError("Probe address is outside executable .text")
    return address - text[3] + text[4]
