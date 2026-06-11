"""Docker container types.

Pure Pydantic models with no heavy dependencies (no fastmcp, aiodocker, etc.).
This is intentional — see assert_no_deps test in BUILD.bazel.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

# -- BindMount --


class BindMount(BaseModel, frozen=True):
    """Docker volume bind mount specification."""

    host_path: Path
    container_path: Path
    mode: Literal["ro", "rw"] = "rw"

    def to_docker_spec(self) -> str:
        """Convert to Docker bind spec string: 'host:container:mode'."""
        return f"{self.host_path}:{self.container_path}:{self.mode}"

    @classmethod
    def parse_bind(cls, spec: str) -> BindMount:
        """Parse a single bind mount spec string (host:container[:mode])."""
        parts = spec.split(":")
        if len(parts) < 2 or len(parts) > 3:
            raise ValueError(f"Invalid bind mount spec '{spec}'. Use host:container[:mode].")
        host, container = parts[0], parts[1]
        mode = parts[2] if len(parts) == 3 else "rw"
        return cls(host_path=Path(host).resolve(), container_path=Path(container), mode=mode)

    @classmethod
    def parse_binds(cls, values: list[str]) -> list[BindMount]:
        """Parse bind mount specifications (comma-separated) into BindMount objects."""
        entries = [entry for value in values for entry in value.split(",") if entry]
        return [cls.parse_bind(entry) for entry in entries]


# -- CwdPolicy: controls how the `cwd` field is exposed in the exec tool schema --
#
# Discriminated union via Pydantic's tagged union support. JSON-serializable
# so it can be embedded in ContainerExecServerConfig.


class ModelChooses(BaseModel, frozen=True):
    """Model picks the cwd via a required tool input field."""

    type: Literal["model_chooses"] = "model_chooses"


class DefaultValue(BaseModel, frozen=True):
    """cwd is optional in schema; falls back to this value when omitted."""

    type: Literal["default"] = "default"
    value: Path


class AlwaysSetTo(BaseModel, frozen=True):
    """cwd is hidden from model; always uses this value."""

    type: Literal["always"] = "always"
    value: Path


CwdPolicy = Annotated[ModelChooses | DefaultValue | AlwaysSetTo, Field(discriminator="type")]


# -- ContainerExecServerConfig --


class ContainerExecServerConfig(BaseModel):
    """Complete JSON-serializable configuration for a ContainerExecServer."""

    image: str
    binds: list[BindMount] = Field(default_factory=list)
    network_mode: str = "none"
    environment: dict[str, str] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)
    name: str | None = None
    # Docker WorkingDir for the container (used by the initial command,
    # not by exec calls which get explicit workdir from cwd_policy).
    working_dir: Path
    allow_user_field: bool
    allow_env_field: bool
    cwd_policy: CwdPolicy

    def to_container_config(self, *, cmd: list[str], auto_remove: bool = False) -> dict[str, Any]:
        """Build Docker container config dict for containers.create()."""
        host_config: dict[str, Any] = {
            "NetworkMode": self.network_mode,
            "Binds": [bind.to_docker_spec() for bind in self.binds],
        }
        if auto_remove:
            host_config["AutoRemove"] = True
        return {
            "Image": self.image,
            "Cmd": cmd,
            "WorkingDir": str(self.working_dir),
            "Env": [f"{k}={v}" for k, v in self.environment.items()],
            "Labels": dict(self.labels),
            "AttachStdout": True,
            "AttachStderr": True,
            "Tty": False,
            "HostConfig": host_config,
        }


# -- Container info (for MCP resource responses) --


class ContainerImageInfo(BaseModel):
    name: str | None = None
    id: str | None = None
    tags: list[str] | None = None


class NetworkMode(StrEnum):
    NONE = "none"
    BRIDGE = "bridge"
    HOST = "host"


class ContainerImageHistoryEntry(BaseModel):
    """One line from Docker image history (docker API).

    Docker engine returns keys with specific casing; we accept them via aliases and
    normalize to snake_case on our JSON output.
    """

    id: str | None = Field(default=None, alias="Id")
    created: int | None = Field(default=None, alias="Created")
    created_by: str | None = Field(default=None, alias="CreatedBy")
    tags: list[str] | None = Field(default=None, alias="Tags")
    size: int | None = Field(default=None, alias="Size")
    comment: str | None = Field(default=None, alias="Comment")


class ContainerInfo(BaseModel):
    """JSON shape for the runtime container.info resource."""

    image: ContainerImageInfo | dict
    container_id: str | None = None
    binds: dict | list | None = None
    working_dir: str | None = None
    network_mode: str | None = None
    image_history: list[ContainerImageHistoryEntry] | None = None
