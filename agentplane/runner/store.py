"""Session records and directories under the runner's state directory."""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from agentplane.proto import runner_pb2
from agentplane.runner.harness import Harness

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class StateOwnershipError(RuntimeError):
    """Another runner owns the retained state directory, or the filesystem cannot fence it."""


class StateOwner:
    """Lifetime exclusive writer ownership for one retained runner state directory."""

    def __init__(self, root: Path) -> None:
        make_directory(root)
        # Linux permits an exclusive flock on a read-only descriptor. Native harnesses inherit this
        # descriptor so the ownership fence survives runner death, but they must not be able to
        # write or truncate the lock file after switching to the unprivileged agent UID.
        descriptor = os.open(root / ".agentplane-runner-owner", os.O_RDONLY | os.O_CREAT, 0o600)
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
    harness: Harness
    cwd: str
    model: str
    # Resolved from the LLM ingress when this route was selected; None means the harness default.
    total_context_budget_tokens: int | None = Field(default=None, gt=0)
    reasoning_effort: str
    instructions: str = Field(description="SessionSpec.instructions; empty for a session without any")
    setup_script_sha256: str | None = Field(
        default=None, description="Creation-time setup identity; the source is never echoed in SessionSpec."
    )
    event_source_id: UUID = Field(
        description="The runner session's stable EventOrigin.source_id across runner restarts."
    )
    native_session_id: str | None = Field(
        default=None, description="Claude session id or Codex thread id, once the harness has assigned one"
    )

    @classmethod
    def from_spec(
        cls, spec: runner_pb2.SessionSpec, *, setup_script: str = "", total_context_budget_tokens: int | None = None
    ) -> SessionRecord:
        return cls(
            harness=Harness(runner_pb2.Harness.Name(spec.harness)),
            cwd=spec.cwd,
            model=spec.model,
            total_context_budget_tokens=total_context_budget_tokens,
            reasoning_effort=spec.reasoning_effort,
            instructions=spec.instructions,
            setup_script_sha256=hashlib.sha256(setup_script.encode()).hexdigest() if setup_script else None,
            event_source_id=uuid4(),
        )

    def spec(self) -> runner_pb2.SessionSpec:
        return runner_pb2.SessionSpec(
            harness=cast(runner_pb2.Harness, runner_pb2.Harness.Value(self.harness)),
            cwd=self.cwd,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            instructions=self.instructions,
        )


class IngressAcknowledgement(BaseModel):
    """The latest acknowledgement the runner's ingress sent for a Session's journal."""

    cursor: int = Field(ge=0, description="Every entry through this cursor is committed at the ingress.")
    sealed: bool = Field(description="The ingress committed the journal's seal at `cursor`.")


def validate_session_id(session_id: str) -> str:
    if not _SESSION_ID.match(session_id):
        raise ValueError(f"invalid {session_id=}: expected [A-Za-z0-9][A-Za-z0-9._-]{{0,127}}")
    return session_id


class SessionStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        # None until a `Welcome` names the incarnation.
        self._incarnation_uid: str | None = None
        make_directory(root)

    def directory(self, session_id: str) -> Path:
        return self.root / validate_session_id(session_id)

    def session_ids(self) -> list[str]:
        return sorted(path.name for path in self.root.iterdir() if (path / "session.json").is_file())

    # The marker file keeps its name `sealed`: a state volume outlives runner releases.
    @property
    def lameduck(self) -> bool:
        return (self.root / "sealed").is_file()

    @property
    def incarnation_uid(self) -> str | None:
        return self._incarnation_uid

    def adopt_incarnation(self, incarnation_uid: str) -> None:
        """Learn which incarnation of its Sandbox this runner is, from the gateway's `Welcome`, before
        any call is served. The state volume outlives incarnations, so a lameduck recorded by an earlier
        one is dropped; one recorded by this one, such as before a runner restart, stands.
        """
        self._incarnation_uid = incarnation_uid
        marker = self.root / "sealed"
        if marker.is_file() and marker.read_text() != incarnation_uid:
            marker.unlink()
            sync_directory(self.root)

    def enter_lameduck(self) -> None:
        """Durably refuse new and resumed sessions in this incarnation, across runner restarts."""
        if self._incarnation_uid is None:
            raise RuntimeError("the runner cannot quiesce before the gateway names its incarnation UID")
        marker = self.root / "sealed"
        if not marker.is_file():
            staged = self.root / "sealed.tmp"
            with staged.open("w") as file:
                file.write(self._incarnation_uid)
                file.flush()
                os.fsync(file.fileno())
            staged.replace(marker)
            sync_directory(self.root)

    def exists(self, session_id: str) -> bool:
        return (self.directory(session_id) / "session.json").is_file()

    def read(self, session_id: str) -> SessionRecord:
        return SessionRecord.model_validate_json((self.directory(session_id) / "session.json").read_bytes())

    def write(self, session_id: str, record: SessionRecord) -> None:
        self._replace(session_id, "session.json", record)

    def read_acknowledgement(self, session_id: str) -> IngressAcknowledgement | None:
        """None until the ingress first acknowledged the Session."""
        path = self.directory(session_id) / "acknowledgement.json"
        return IngressAcknowledgement.model_validate_json(path.read_bytes()) if path.is_file() else None

    def write_acknowledgement(self, session_id: str, acknowledgement: IngressAcknowledgement) -> None:
        self._replace(session_id, "acknowledgement.json", acknowledgement)

    def _replace(self, session_id: str, name: str, model: BaseModel) -> None:
        directory = self.directory(session_id)
        make_directory(directory)
        staged = directory / f"{name}.tmp"
        with staged.open("wb") as output:
            output.write(model.model_dump_json(indent=2).encode())
            output.flush()
            os.fsync(output.fileno())
        staged.replace(directory / name)
        sync_directory(directory)
