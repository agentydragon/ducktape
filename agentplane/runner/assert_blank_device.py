"""Exit 0 if a block device reads as zeros end to end, 3 at the first non-zero byte.

The guest's workspace preparation (vm.nix) formats the disk only when this holds. It reads in large
chunks: byte-wise tools such as `cmp` issue millions of tiny reads per GiB, which dominates on the
guest's single vCPU.
"""

import sys
from pathlib import Path

CHUNK_BYTES = 16 << 20
# vm.nix tells a non-blank disk from an unreadable one by this exit code.
NOT_BLANK_EXIT_CODE = 3


def first_nonzero_offset(device: Path, *, chunk_bytes: int = CHUNK_BYTES) -> int | None:
    zeros = bytes(chunk_bytes)
    offset = 0
    with device.open("rb", buffering=0) as stream:
        while chunk := stream.read(chunk_bytes):
            if chunk != (zeros if len(chunk) == chunk_bytes else bytes(len(chunk))):
                return offset + len(chunk) - len(chunk.lstrip(b"\0"))
            offset += len(chunk)
    return None


def main() -> None:
    (device,) = sys.argv[1:]
    if (offset := first_nonzero_offset(Path(device))) is not None:
        print(f"{device} holds a non-zero byte at offset {offset}", file=sys.stderr)
        raise SystemExit(NOT_BLANK_EXIT_CODE)


if __name__ == "__main__":
    main()
