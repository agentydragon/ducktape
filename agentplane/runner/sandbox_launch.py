"""What the Sandbox gives its runner to launch Sessions with: its identity, its once-per-Sandbox
bootstrap, and the deployment-wide platform instructions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class SandboxIdentity(BaseModel):
    """The Sandbox as the Notification Service addresses it."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    namespace: str = Field(min_length=1)
    name: str = Field(min_length=1)
    uid: str = Field(min_length=1)


class SandboxLaunchFile(BaseModel):
    """The file the Sandbox Controller mounts for its Sandbox (agentplane/sandbox_controller/launch.go)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    sandbox: SandboxIdentity
    bootstrap: str | None = Field(default=None, min_length=1, description="Shell source the Sandbox runs once.")


@dataclass(frozen=True, slots=True)
class SandboxLaunch:
    sandbox: SandboxIdentity
    bootstrap: str | None
    platform_instructions: str

    @classmethod
    def read(cls, launch_file: Path, *, platform_instructions: str) -> SandboxLaunch:
        parsed = SandboxLaunchFile.model_validate_json(launch_file.read_bytes())
        return cls(sandbox=parsed.sandbox, bootstrap=parsed.bootstrap, platform_instructions=platform_instructions)

    def instructions(self, session_id: str, task: str) -> str:
        """A Session's standing instructions: the platform guidance, then where the Session runs, then
        the task's own."""
        destination = json.dumps({"destination_ref": self.sandbox.model_dump(), "session_id": session_id})
        return "\n\n".join(
            part.strip()
            for part in (
                self.platform_instructions,
                f"Your explicit notification destination is:\n{destination}\n"
                "Use these identifiers when addressing this session; they are not credentials.",
                task,
            )
            if part.strip()
        )
