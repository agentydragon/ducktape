"""Create a workspace path below an already-open root without following symlinks."""

from __future__ import annotations

import os
import sys
from contextlib import suppress


def create_beneath(root: str, parts: list[str]) -> None:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open(root, directory_flags)
    try:
        for part in parts:
            if not part or part in {".", ".."} or "/" in part:
                raise ValueError(f"invalid workspace path component: {part!r}")
            with suppress(FileExistsError):
                os.mkdir(part, mode=0o770, dir_fd=descriptor)
            child = os.open(part, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    root, *parts = sys.argv[1:]
    if not parts:
        raise SystemExit("usage: workspace_create.py ROOT COMPONENT [COMPONENT ...]")
    create_beneath(root, parts)
