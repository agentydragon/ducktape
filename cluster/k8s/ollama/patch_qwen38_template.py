"""Derive the Qwen3.8 IQ4 metadata shard with a mid-turn instruction template.

The pinned first GGUF shard contains the tokenizer and chat template. Tensor
offsets are relative to the aligned tensor-data start, so replacing one GGUF
string and realigning that start preserves the tensor table and payload.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import struct
from pathlib import Path

ORIGINAL_SHA256 = "5ce89370720f8bf90890f439361282104c1aa1482d4013bb9a50923e758e71a4"
ORIGINAL_TEMPLATE_SHA256 = "12827f24b742ea4e80cdc12dbcf9622227056b9f797252a3149263d4f9aaadce"
OLD_BRANCH = b"{{- raise_exception('System message must be at the beginning.') }}"
NEW_BRANCH = b"{{- '<|im_start|>system\\n' + content + '<|im_end|>\\n' }}"

_SCALAR_SIZES = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
_STRING = 8
_ARRAY = 9


class Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, size: int) -> bytes:
        end = self.pos + size
        if end > len(self.data):
            raise ValueError("truncated GGUF metadata")
        value = self.data[self.pos : end]
        self.pos = end
        return value

    def u32(self) -> int:
        return int.from_bytes(self.take(4), "little")

    def u64(self) -> int:
        return int.from_bytes(self.take(8), "little")

    def string(self) -> tuple[int, bytes]:
        length_offset = self.pos
        size = self.u64()
        return length_offset, self.take(size)

    def skip_value(self, kind: int) -> None:
        if kind in _SCALAR_SIZES:
            self.take(_SCALAR_SIZES[kind])
        elif kind == _STRING:
            self.string()
        elif kind == _ARRAY:
            item_kind = self.u32()
            count = self.u64()
            for _ in range(count):
                if item_kind == _ARRAY:
                    raise ValueError("nested GGUF arrays are unsupported")
                self.skip_value(item_kind)
        else:
            raise ValueError(f"unknown GGUF value type {kind}")


def replace_template(data: bytes, replacement: bytes) -> bytes:
    # The checked-in text file has a final newline; the GGUF metadata string does not.
    replacement = replacement.removesuffix(b"\n")
    reader = Reader(data)
    if reader.take(4) != b"GGUF" or reader.u32() != 3:
        raise ValueError("expected GGUF version 3")
    tensor_count = reader.u64()
    metadata_count = reader.u64()
    template_offset: int | None = None
    template_end: int | None = None
    alignment = 32
    for _ in range(metadata_count):
        _, key_bytes = reader.string()
        key = key_bytes.decode("utf-8")
        kind = reader.u32()
        if key == "tokenizer.chat_template":
            if kind != _STRING or template_offset is not None:
                raise ValueError("expected exactly one string chat template")
            template_offset, original_template = reader.string()
            template_end = reader.pos
        elif key == "general.alignment":
            if kind != 4:
                raise ValueError("expected uint32 GGUF alignment")
            alignment = reader.u32()
        else:
            reader.skip_value(kind)
    if template_offset is None or template_end is None:
        raise ValueError("GGUF chat template is missing")
    if hashlib.sha256(original_template).hexdigest() != ORIGINAL_TEMPLATE_SHA256:
        raise ValueError("unexpected original Qwen chat template")
    if original_template.count(OLD_BRANCH) != 1 or replacement != original_template.replace(OLD_BRANCH, NEW_BRANCH):
        raise ValueError("replacement changes more than the mid-turn instruction branch")
    for _ in range(tensor_count):
        reader.string()  # name
        for _ in range(reader.u32()):
            reader.u64()  # dimensions
        reader.u32()  # tensor type
        reader.u64()  # offset relative to tensor-data start
    if alignment < 1 or alignment & (alignment - 1):
        raise ValueError("invalid GGUF alignment")
    old_data_start = (reader.pos + alignment - 1) & -alignment
    if old_data_start > len(data):
        raise ValueError("truncated GGUF tensor data")
    header = data[:template_offset] + struct.pack("<Q", len(replacement)) + replacement + data[template_end : reader.pos]
    padding = (-len(header)) % alignment
    return header + bytes(padding) + data[old_data_start:]


def derive(source: Path, template: Path, destination: Path, expected_sha256: str) -> None:
    result = derived_bytes(source, template)
    if hashlib.sha256(result).hexdigest() != expected_sha256:
        raise ValueError("derived GGUF differs from the pinned output digest")
    if destination.exists():
        if destination.read_bytes() != result:
            raise ValueError("refusing to replace a different derived GGUF")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".partial")
    if temporary.exists():
        raise ValueError(f"refusing to replace partial output {temporary}")
    with temporary.open("xb") as output:
        output.write(result)
        output.flush()
        os.fsync(output.fileno())
    temporary.rename(destination)


def derived_bytes(source: Path, template: Path) -> bytes:
    original = source.read_bytes()
    if hashlib.sha256(original).hexdigest() != ORIGINAL_SHA256:
        raise ValueError("source GGUF differs from the pinned first IQ4 shard")
    return replace_template(original, template.read_bytes())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--print-sha256", action="store_true", help="validate inputs and print the derived digest")
    parser.add_argument("source", type=Path)
    parser.add_argument("template", type=Path)
    parser.add_argument("destination", type=Path, nargs="?")
    parser.add_argument("expected_sha256", nargs="?")
    args = parser.parse_args()
    if args.print_sha256:
        if args.destination is not None or args.expected_sha256 is not None:
            parser.error("--print-sha256 takes only source and template")
        print(hashlib.sha256(derived_bytes(args.source, args.template)).hexdigest())
        return
    if args.destination is None or args.expected_sha256 is None:
        parser.error("destination and expected_sha256 are required")
    derive(args.source, args.template, args.destination, args.expected_sha256)


if __name__ == "__main__":
    main()
