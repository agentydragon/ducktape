"""Validate and persist independent provider sessions."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from atomicwrites import atomic_write
from pydantic import TypeAdapter


class ProviderSessionStore[T]:
    def __init__(self, directory: Path, filename: str, adapter: TypeAdapter[T], verify: Callable[..., Any]):
        self.directory = directory
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        self.path = directory / filename
        self.lock = threading.Lock()
        self.adapter = adapter
        self.verify = verify

    def load(self) -> T | None:
        with self.lock:
            return self._load()

    def snapshot(self) -> tuple[T | None, int | None]:
        """Read a session and its import time consistently with installs."""
        with self.lock:
            session = self._load()
            return session, self.path.stat().st_mtime_ns if session else None

    def _load(self) -> T | None:
        if not self.path.exists():
            return None
        if self.path.stat().st_mode & 0o077:
            raise ValueError("Session file must be private")
        return self.adapter.validate_json(self.path.read_bytes())

    def install(self, candidate: T, *, transport=None) -> None:
        # Do not disturb a working session if validation fails.
        self.verify(candidate, transport=transport)
        with self.lock, atomic_write(self.path, mode="wb", overwrite=True, prefix=".session-") as stream:
            stream.write(self.adapter.dump_json(candidate))
