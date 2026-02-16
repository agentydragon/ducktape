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

    files_to_add: dict[Path, Path] = {}  # arcname -> src_path
    for src_path in args.sources:
        if not src_path.exists():
            raise FileNotFoundError(f"Source file does not exist: {src_path}")

        # Strip the prefix to get the path relative to the code root.
        rel_path = src_path.relative_to(args.strip_prefix)
        if not rel_path.parts:
            raise ValueError(f"Source {src_path} resolved to empty path after stripping {args.strip_prefix}")

        # Handle .specimen rename
        if src_path.suffix == ".specimen":
            rel_path = rel_path.with_name(rel_path.stem)

        files_to_add[rel_path] = src_path

    # Create uncompressed tar with deterministic properties (sorted by arcname)
    with tarfile.open(args.output_tar, "w") as tar:
        for arcname in sorted(files_to_add.keys(), key=str):
            src_path = files_to_add[arcname]
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
