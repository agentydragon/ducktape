"""Build a newc-format cpio archive from a JSON manifest on stdin.

Usage: cpio_tool.py <output.cpio> < <manifest.json>

stdin: [[dest_path, src_file, mode_octal], ...]  (mode_octal e.g. "0755")

Adds a root directory entry, one entry per pair (mode 0755), then closes
with a TRAILER!!! entry. mtime is zeroed for reproducibility.
"""

import json
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
    if len(sys.argv) != 2:
        sys.exit(f"Usage: {sys.argv[0]} <output.cpio> < <manifest.json>")

    output_path = sys.argv[1]
    manifest: list[list[str]] = json.load(sys.stdin)

    entries = [(".", None, 0o040755, 2)] + [(dest, src, 0o100000 | int(mode, 8), 1) for dest, src, mode in manifest]
    with Path(output_path).open("wb") as out:
        for ino, (name, src_file, mode, nlink) in enumerate(entries, start=1):
            data = Path(src_file).read_bytes() if src_file else b""
            out.write(_entry(ino, mode, nlink, name, data))
        out.write(_entry(0, 0, 1, "TRAILER!!!", b""))


if __name__ == "__main__":
    main()
