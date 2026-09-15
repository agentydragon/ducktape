"""Persistence fences for runner journals and the directories which retain them."""

import os
from pathlib import Path


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def make_directory(path: Path) -> None:
    """Persist each newly created directory's entry in its existing parent."""
    missing = []
    parent = path
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    # A previous creation may have failed at its parent fence while leaving the directory in
    # the kernel cache. Establish that existing entry before extending its path.
    sync_directory(parent.parent)
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        sync_directory(directory.parent)


class JournalFile:
    """Append only through a durable fence; a failed writer must be reopened for recovery."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._failure: OSError | None = None
        self._file = path.open("ab")
        try:
            # A recovered complete append may have survived a process crash only in the kernel
            # cache. Sync it before callers publish recovered records, and retain a new filename.
            os.fsync(self._file.fileno())
            sync_directory(path.parent)
        except BaseException:
            self._file.close()
            raise

    def check_writable(self) -> None:
        if self._failure is not None:
            raise OSError(f"journal {self.path} failed; reopen it for recovery") from self._failure

    def append(self, record: bytes) -> None:
        self.check_writable()
        try:
            self._file.write(record)
            self._file.flush()
            os.fsync(self._file.fileno())
        except OSError as error:
            # Some bytes may already be persistent. Appending again from the old in-memory
            # cursor could reuse its position or duplicate a command admission.
            self._failure = error
            raise

    def close(self) -> None:
        self._file.close()
