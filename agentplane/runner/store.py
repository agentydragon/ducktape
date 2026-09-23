"""Session records and directories under the runner's state directory."""

from __future__ import annotations

import fcntl
import os
import re
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from agentplane.runner import protocol_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class StateOwnershipError(RuntimeError):
    """Another runner owns the retained state directory, or the filesystem cannot fence it."""


class StateOwner:
    """Lifetime exclusive writer ownership for one retained runner state directory."""

    def __init__(self, root: Path) -> None:
        make_directory(root)
        descriptor = os.open(root / ".agentplane-runner-owner", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(descriptor)
            raise StateOwnershipError(f"runner state directory {root} is already owned") from error
        except OSError as error:
            os.close(descriptor)
            raise StateOwnershipError(f"cannot fence runner state directory {root}: {error}") from error
        self._descriptor: int | None = descriptor

    @property
    def descriptor(self) -> int:
        if self._descriptor is None:
            raise RuntimeError("runner state ownership is already closed")
        return self._descriptor

    def close(self) -> None:
        if self._descriptor is not None:
            fcntl.flock(self._descriptor, fcntl.LOCK_UN)
            os.close(self._descriptor)
            self._descriptor = None


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


class SessionRecord(BaseModel):
    harness: str = Field(description="Harness enum name, e.g. HARNESS_CLAUDE")
    cwd: str
    model: str
    reasoning_effort: str
    instructions: str = Field(default="", description="SessionSpec.instructions; empty for a session without any")
    event_source_id: UUID = Field(
        default_factory=uuid4, description="The runner session's stable EventOrigin.source_id across runner restarts."
    )
    native_session_id: str | None = Field(
        default=None, description="Claude session id or Codex thread id, once the harness has assigned one"
    )

    @classmethod
    def from_spec(cls, spec: protocol_pb2.SessionSpec) -> SessionRecord:
        return cls(
            harness=protocol_pb2.Harness.Name(spec.harness),
            cwd=spec.cwd,
            model=spec.model,
            reasoning_effort=spec.reasoning_effort,
            instructions=spec.instructions,
        )

    def spec(self) -> protocol_pb2.SessionSpec:
        return protocol_pb2.SessionSpec(
            harness=cast(protocol_pb2.Harness, protocol_pb2.Harness.Value(self.harness)),
            cwd=self.cwd,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            instructions=self.instructions,
        )


def validate_session_id(session_id: str) -> str:
    if not _SESSION_ID.match(session_id):
        raise ValueError(f"invalid {session_id=}: expected [A-Za-z0-9][A-Za-z0-9._-]{{0,127}}")
    return session_id


class SessionStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        make_directory(root)

    def directory(self, session_id: str) -> Path:
        return self.root / validate_session_id(session_id)

    def session_ids(self) -> list[str]:
        return sorted(path.name for path in self.root.iterdir() if (path / "session.json").is_file())

    def exists(self, session_id: str) -> bool:
        return (self.directory(session_id) / "session.json").is_file()

    def read(self, session_id: str) -> SessionRecord:
        return SessionRecord.model_validate_json((self.directory(session_id) / "session.json").read_bytes())

    def write(self, session_id: str, record: SessionRecord) -> None:
        directory = self.directory(session_id)
        make_directory(directory)
        staged = directory / "session.json.tmp"
        with staged.open("wb") as output:
            output.write(record.model_dump_json(indent=2).encode())
            output.flush()
            os.fsync(output.fileno())
        staged.replace(directory / "session.json")
        sync_directory(directory)
