"""Build a newc-format cpio archive from a rules_pkg-format JSON manifest.

Usage: cpio_tool.py <output.cpio> < <manifest.json>

Only "file" entries are supported. Non-zero uid/gid and user/group names
(which cpio does not represent) are rejected.
"""

import sys
from pathlib import Path

from pkg.private import manifest as pkg_manifest


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
    entries = pkg_manifest.read_entries_from(sys.stdin)

    cpio_entries = [(".", None, 0o040755, 2)]
    for e in entries:
        if e.type != pkg_manifest.ENTRY_IS_FILE:
            sys.exit(f"{e.dest!r}: unsupported entry type {e.type!r} (only 'file' is supported)")
        if e.user is not None:
            sys.exit(f"{e.dest!r}: user={e.user!r} not supported (cpio has no username field; use uid=0)")
        if e.group is not None:
            sys.exit(f"{e.dest!r}: group={e.group!r} not supported (cpio has no group name field; use gid=0)")
        if e.uid not in (None, 0):
            sys.exit(f"{e.dest!r}: uid={e.uid!r} not supported (only uid=0 is supported)")
        if e.gid not in (None, 0):
            sys.exit(f"{e.dest!r}: gid={e.gid!r} not supported (only gid=0 is supported)")
        cpio_entries.append((e.dest, e.src, 0o100000 | int(e.mode or "0755", 8), 1))
    with Path(output_path).open("wb") as out:
        for ino, (name, src_file, mode, nlink) in enumerate(cpio_entries, start=1):
            data = Path(src_file).read_bytes() if src_file else b""
            out.write(_entry(ino, mode, nlink, name, data))
        out.write(_entry(0, 0, 1, "TRAILER!!!", b""))


if __name__ == "__main__":
    main()
