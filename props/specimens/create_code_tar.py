"""Create specimen code tar with BUILD file renaming."""

import argparse
import tarfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output_tar", type=Path)
    parser.add_argument("--strip-prefix", required=True, type=Path, dest="strip_prefix")
    parser.add_argument("sources", nargs="*", type=Path)
    args = parser.parse_args()

    files_to_add = []
    for src_path in args.sources:
        if not src_path.exists():
            continue

        # Strip the prefix to get the path relative to the code root.
        try:
            rel_path = src_path.relative_to(args.strip_prefix)
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
    with tarfile.open(args.output_tar, "w") as tar:
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
