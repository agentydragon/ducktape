"""Build a newc-format cpio archive from a list of files.

Usage: cpio_tool.py <output.cpio> <dest_path> <src_file> [<dest_path> <src_file> ...]

Adds a root directory entry, one entry per dest_path/src_file pair (mode 0755),
then closes with a TRAILER!!! entry. mtime is zeroed for reproducibility.
"""

import sys
from pathlib import Path


def _pad4(n: int) -> int:
    return (4 - n % 4) % 4


def _entry(ino: int, mode: int, nlink: int, name: str, data: bytes) -> bytes:
    name_bytes = name.encode() + b"\x00"
    namesize = len(name_bytes)
    filesize = len(data)
    # newc header: magic + 13 x 8-hex-digit fields (ino, mode, uid, gid, nlink,
    # mtime, filesize, devmajor, devminor, rdevmajor, rdevminor, namesize, check).
    # mtime zeroed for reproducibility; uid/gid/dev*/check always 0.
    header = (
        "070701" + "".join(f"{v:08X}" for v in [ino, mode, 0, 0, nlink, 0, filesize, 0, 0, 0, 0, namesize, 0])
    ).encode()
    assert len(header) == 110
    return header + name_bytes + bytes(_pad4(110 + namesize)) + data + bytes(_pad4(filesize))


def main() -> None:
    args = sys.argv[1:]
    if not args or len(args[1:]) % 2 != 0:
        sys.exit(f"Usage: {sys.argv[0]} <output.cpio> [<dest_path> <src_file> ...]")

    output_path, *pairs = args

    with Path(output_path).open("wb") as out:
        ino = 1
        out.write(_entry(ino, 0o040755, 2, ".", b""))
        ino += 1
        for dest_path, src_file in zip(pairs[::2], pairs[1::2], strict=False):
            data = Path(src_file).read_bytes()
            out.write(_entry(ino, 0o100755, 1, dest_path, data))
            ino += 1
        out.write(_entry(0, 0, 1, "TRAILER!!!", b""))


if __name__ == "__main__":
    main()
