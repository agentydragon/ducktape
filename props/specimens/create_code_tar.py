"""Create specimen code tar with BUILD file renaming."""

import sys
import tarfile
from pathlib import Path


def _parse_args(argv):
    """Parse arguments: output_tar [--root-marker MARKER] source_files..."""
    output_tar = Path(argv[0])
    root_marker = "code"
    source_files = []

    i = 1
    while i < len(argv):
        if argv[i] == "--root-marker" and i + 1 < len(argv):
            root_marker = argv[i + 1]
            i += 2
        else:
            source_files.append(argv[i])
            i += 1

    return output_tar, root_marker, source_files


def main():
    output_tar, root_marker, source_files = _parse_args(sys.argv[1:])

    # Group files by their path relative to root_marker/
    files_to_add = []
    for src in source_files:
        src_path = Path(src)
        if not src_path.exists():
            continue

        # Extract relative path after the root_marker segment.
        # Match suffix to handle bzlmod canonical names (e.g.,
        # "external/+_repo_rules+specimen_crush_code" matches "specimen_crush_code").
        parts = src_path.parts
        code_idx = None
        for i, part in enumerate(parts):
            if part == root_marker or part.endswith("+" + root_marker):
                code_idx = i
                break
        if code_idx is None:
            continue
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
