"""Create specimen code tar with BUILD file renaming."""

import sys
import tarfile
from pathlib import Path


def _parse_args(argv):
    """Parse arguments: output_tar --strip-prefix PREFIX source_files..."""
    output_tar = Path(argv[0])
    strip_prefix = None
    source_files = []

    i = 1
    while i < len(argv):
        if argv[i] == "--strip-prefix" and i + 1 < len(argv):
            strip_prefix = argv[i + 1]
            i += 2
        else:
            source_files.append(argv[i])
            i += 1

    if strip_prefix is None:
        raise SystemExit("--strip-prefix is required")

    return output_tar, strip_prefix, source_files


def main():
    output_tar, strip_prefix, source_files = _parse_args(sys.argv[1:])
    prefix_path = Path(strip_prefix)

    files_to_add = []
    for src in source_files:
        src_path = Path(src)
        if not src_path.exists():
            continue

        # Strip the prefix to get the path relative to the code root.
        try:
            rel_path = src_path.relative_to(prefix_path)
        except ValueError:
            continue
        if not rel_path.parts:
            continue

        # Handle .specimen rename
        if src_path.suffix == ".specimen":
            rel_path = rel_path.with_name(rel_path.stem)

        files_to_add.append((src_path, rel_path))

    # Sort for deterministic tar
    files_to_add.sort(key=lambda x: str(x[1]))

    # Create uncompressed tar with deterministic properties
    with tarfile.open(output_tar, "w") as tar:
        for src_path, arcname in files_to_add:
            tarinfo = tar.gettarinfo(str(src_path), arcname=str(arcname))
            tarinfo.mtime = 0  # Epoch time for determinism
            tarinfo.uid = 0
            tarinfo.gid = 0
            tarinfo.uname = ""
            tarinfo.gname = ""
            with src_path.open("rb") as f:
                tar.addfile(tarinfo, f)


if __name__ == "__main__":
    main()
