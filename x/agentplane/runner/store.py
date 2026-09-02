"""Session records and directories under the runner's state directory."""

from __future__ import annotations

import os
import re
from pathlib import Path

from pydantic import BaseModel, Field

from x.agentplane.runner import protocol_pb2 as pb

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class SessionRecord(BaseModel):
    provider: str = Field(description="Provider enum name, e.g. PROVIDER_CLAUDE")
    cwd: str
    model: str
    reasoning_effort: str
    native_session_id: str | None = Field(
        default=None, description="Claude session id or Codex thread id, once the harness has assigned one"
    )

    @classmethod
    def from_spec(cls, spec: pb.SessionSpec) -> SessionRecord:
        return cls(
            provider=pb.Provider.Name(spec.provider),
            cwd=spec.cwd,
            model=spec.model,
            reasoning_effort=spec.reasoning_effort,
        )

    def spec(self) -> pb.SessionSpec:
        return pb.SessionSpec(
            provider=pb.Provider.ValueType(pb.Provider.Value(self.provider)),
            cwd=self.cwd,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
        )


def validate_session_id(session_id: str) -> str:
    if not _SESSION_ID.match(session_id):
        raise ValueError(f"invalid {session_id=}: expected [A-Za-z0-9][A-Za-z0-9._-]{{0,127}}")
    return session_id


class SessionStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

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
        directory.mkdir(exist_ok=True)
        staged = directory / "session.json.tmp"
        with staged.open("wb") as output:
            output.write(record.model_dump_json(indent=2).encode())
            output.flush()
            os.fsync(output.fileno())
        staged.replace(directory / "session.json")
