"""A power-loss model at the filesystem durability boundary, using real file operations.

Only fsync-completed file contents and directory entries reach the recovered image. This models
loss of unsynced writes, not device corruption, a lying fsync implementation, or loss of the volume.
"""

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DirectoryEntry:
    inode: int
    is_directory: bool


class StorageImage:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.fail_path: Path | None = None
        self.before_sync: Callable[[Path], None] | None = None
        self._fsync = os.fsync
        self._root_inode = root.stat().st_ino
        self._files: dict[int, bytes] = {}
        self._directories: dict[int, dict[str, DirectoryEntry]] = {self._root_inode: {}}

    def fsync(self, descriptor: int) -> None:
        path = Path(f"/proc/self/fd/{descriptor}").readlink()
        if not path.is_relative_to(self.root):
            self._fsync(descriptor)
            return
        if self.before_sync is not None:
            self.before_sync(path)
        if path == self.fail_path:
            raise OSError("injected storage fence failure")
        self._fsync(descriptor)
        metadata = os.fstat(descriptor)
        if stat.S_ISDIR(metadata.st_mode):
            self._directories[metadata.st_ino] = {
                child.name: DirectoryEntry(child.stat().st_ino, child.is_dir()) for child in path.iterdir()
            }
        else:
            assert stat.S_ISREG(metadata.st_mode)
            self._files[metadata.st_ino] = path.read_bytes()

    def recover(self, destination: Path) -> None:
        """Materialize only fence-committed state under a fresh simulated mount point."""
        destination.mkdir()
        self._recover_directory(self._root_inode, destination)

    def _recover_directory(self, inode: int, destination: Path) -> None:
        for name, entry in self._directories.get(inode, {}).items():
            path = destination / name
            if entry.is_directory:
                path.mkdir()
                self._recover_directory(entry.inode, path)
            else:
                path.write_bytes(self._files.get(entry.inode, b""))
