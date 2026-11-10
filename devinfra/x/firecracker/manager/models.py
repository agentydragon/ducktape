"""Pydantic models for the Firecracker VM manager API."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class VMStatus(StrEnum):
    """VM lifecycle status. Includes k8s pod phases + our custom states."""

    CREATING = "creating"
    PENDING = "Pending"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    UNKNOWN = "Unknown"


class CreateVMRequest(BaseModel):
    cpus: int = Field(default=2, description="Number of vCPUs")
    mem_mib: int = Field(default=4096, description="Memory in MiB")


class VMInfo(BaseModel):
    id: str
    pod_name: str
    status: VMStatus
    pod_ip: str | None = Field(default=None, description="Pod IP — all ports proxied from here")


class CreateVMResponse(BaseModel):
    vm: VMInfo


class ListVMsResponse(BaseModel):
    vms: list[VMInfo]


class SnapshotRequest(BaseModel):
    name: str = Field(description="Name for the snapshot")


class RestoreRequest(BaseModel):
    name: str | None = Field(default=None, description="Name for the restored VM. None = auto-generated.")
    cpus: int = Field(default=2, description="Number of vCPUs")
    mem_mib: int = Field(default=4096, description="Memory in MiB")
