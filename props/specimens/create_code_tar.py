"""Create specimen code tar with BUILD file renaming."""

import sys
import tarfile
from pathlib import Path


def main():
    output_tar = Path(sys.argv[1])
    source_files = sys.argv[2:]

    # Group files by their path relative to code/
    files_to_add = []
    for src in source_files:
        src_path = Path(src)
        if not src_path.exists():
            continue

        # Extract relative path after code/
        parts = src_path.parts
        if "code" not in parts:
            continue
        code_idx = parts.index("code")
        rel_parts = parts[code_idx + 1 :]
        if not rel_parts:
            continue

        # Handle .specimen rename
        if src_path.suffix == ".specimen":
            # Remove .specimen extension
            new_name = src_path.stem
            rel_path = Path(*rel_parts[:-1]) / new_name
        else:
            rel_path = Path(*rel_parts)

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
