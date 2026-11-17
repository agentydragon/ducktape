"""Exclusive process ownership of a provider instance's local state."""

from pathlib import Path

from filelock import FileLock


class InstanceOwnership:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        # Keep this lock path stable across provider upgrades.
        self._lock = FileLock(directory / ".import-journal.lock", timeout=0, thread_local=False)
        self._lock.acquire()

    def close(self) -> None:
        self._lock.release()
