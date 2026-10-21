"""Build a newc-format cpio archive from a rules_pkg-format JSON manifest.

Usage: cpio_tool.py <output.cpio> <manifest.json>

Only "file" entries are supported. Non-zero uid/gid and user/group names
(which cpio does not represent) are rejected.
"""

import sys
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from pkg.private import manifest as pkg_manifest


def _check_dest(dest: str) -> None:
    p = PurePosixPath(dest)
    if p.is_absolute():
        sys.exit(f"{dest!r}: absolute destination path not allowed")
    depth = 0
    for part in p.parts:
        if part == "..":
            depth -= 1
            if depth < 0:
                sys.exit(f"{dest!r}: path traversal not allowed")
        elif part != ".":
            depth += 1


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


def write_archive(output: Path, files: Mapping[str, tuple[int, bytes]]) -> None:
    """Write `files`, archive path to (permission bits, contents), with every parent directory."""
    directory_names = {"."}
    for name in files:
        _check_dest(name)
        directory_names.update(str(parent) for parent in PurePosixPath(name).parents if str(parent) != ".")
    if conflicting_dirs := directory_names & files.keys():
        sys.exit(f"archive paths are both files and directories: {sorted(conflicting_dirs)!r}")
    # newc extraction does not create missing parent directories. Emit them explicitly,
    # shallowest first, so nested package destinations exist when the kernel unpacks files.
    cpio_entries = [
        (name, 0o040755, 2, b"")
        for name in sorted(directory_names, key=lambda item: (len(PurePosixPath(item).parts), item))
    ]
    cpio_entries.extend((name, 0o100000 | mode, 1, data) for name, (mode, data) in files.items())
    with output.open("wb") as out:
        for ino, (name, mode, nlink, data) in enumerate(cpio_entries, start=1):
            out.write(_entry(ino, mode, nlink, name, data))
        out.write(_entry(0, 0, 1, "TRAILER!!!", b""))


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(f"Usage: {sys.argv[0]} <output.cpio> <manifest.json>")

    output_path, manifest_path = sys.argv[1], sys.argv[2]
    files = {}
    for e in pkg_manifest.read_entries_from(manifest_path):
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
        files[e.dest] = (int(e.mode or "0755", 8), Path(e.src).read_bytes())
    write_archive(Path(output_path), files)


if __name__ == "__main__":
    main()
