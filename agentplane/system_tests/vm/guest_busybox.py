"""The guest's pinned BusyBox, for host-side tests that run its shell scripts and AWK programs."""

import tarfile
from pathlib import Path


def extract(archive_path: str | Path, directory: Path) -> Path:
    """Write the executable `bin/busybox` of the Debian data archive at `archive_path` into `directory`."""
    busybox = directory / "busybox"
    with tarfile.open(archive_path) as archive:
        member = next(member for member in archive if member.name.endswith("bin/busybox"))
        source = archive.extractfile(member)
        assert source is not None
        busybox.write_bytes(source.read())
    busybox.chmod(0o755)
    return busybox
