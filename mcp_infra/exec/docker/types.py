"""Docker container types.

Pure Pydantic models with no heavy dependencies (no fastmcp, aiodocker, etc.).
This is intentional — see assert_no_deps test in BUILD.bazel.
"""

from __future__ import annotations

from dataclasses import dataclass
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


# -- CwdPolicy: controls how the `cwd` field is exposed in the exec tool schema --
#
# Discriminated union via Pydantic's tagged union support. JSON-serializable
# so it can be embedded in ContainerExecServerConfig.


class ModelChooses(BaseModel, frozen=True):
    """Model picks the cwd via a required tool input field."""

    type: Literal["model_chooses"] = "model_chooses"


class DefaultValue(BaseModel, frozen=True):
    """A null cwd selects this default value."""

    type: Literal["default"] = "default"
    value: Path


class AlwaysSetTo(BaseModel, frozen=True):
    """cwd is hidden from model; always uses this value."""

    type: Literal["always"] = "always"
    value: Path


CwdPolicy = Annotated[ModelChooses | DefaultValue | AlwaysSetTo, Field(discriminator="type")]


# -- ContainerExecServerConfig --


class ContainerExecServerConfig(BaseModel):
    """Container configuration; image must supply GNU coreutils /usr/bin/timeout.

    Required timeout options: --signal=KILL and fractional-second durations.
    Session startup also requires /bin/sh and sleep infinity.
    """

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


# Corresponds to Docker Engine API v1.52 ContainerExec:
# https://docs.docker.com/reference/api/engine/version/v1.52/#tag/Exec/operation/ContainerExec
# Specifically: Cmd (command array) and Env (environment variables as "NAME=value" strings)

# Type alias for environment variable validation
EnvVar = Annotated[
    str,
    Field(
        description="Environment variable in 'NAME=value' format",
        pattern=r"^[^=]+=.*$",  # Must have at least one char before '=', anything after
    ),
]


@dataclass(frozen=True)
class ResolvedExecInput:
    """Resolved execution parameters for docker container exec (internal).

    Constructed by the server handler after applying CwdPolicy and field gating.
    Not the tool's input schema — the LLM-facing schema is generated from the
    native FastMCP exec function signature.
    """

    cmd: list[str]
    cwd: str | None
    env: list[str] | None
    user: str | None
    timeout_ms: int

    def env_dict(self) -> dict[str, str]:
        """Convert env list to dict for docker exec API."""
        if not self.env:
            return {}
        return {k: v for s in self.env for k, _, v in [s.partition("=")]}
