"""Validated launcher inputs supplied by the Sandbox controller and Pod environment."""

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class WorkspaceKind(StrEnum):
    RAW_DISK_IMAGE = "RawDiskImage"
    BLOCK_FILESYSTEM = "BlockFilesystem"


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pod_uid: str = Field(min_length=1)
    sandbox_uid: str = Field(min_length=1)
    vcpu_count: int = Field(ge=1, le=32)
    memory_mib: int = Field(ge=128)
    boot_image: str = Field(min_length=1)
    runner_image: str = Field(min_length=1)
    boot_directory: Path = Path("/images/boot")
    runner_directory: Path = Path("/images/runner")
    workspace_kind: WorkspaceKind
    state_size_mib: int = Field(default=2048, ge=256)
    workspace_size_mib: int = Field(default=8192, ge=256)
    relay_port: int = Field(default=3128, ge=1, le=65535)
    platform_route_ports: list[int] = Field(default_factory=list)

    @field_validator("platform_route_ports")
    @classmethod
    def validate_route_ports(cls, ports: list[int]) -> list[int]:
        if len(ports) != len(set(ports)) or any(port < 1 or port > 65535 or port in {9090, 9092} for port in ports):
            raise ValueError("platform route ports must be unique, valid, and distinct from control ports")
        return ports
