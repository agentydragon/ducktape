"""Typed loader for the Nebula mesh host roster (`nebula-mesh.json`).

Single source of truth for the mesh. See cluster/docs/mesh_membership.md for
add/remove/re-IP flow. Other consumers (Nix, Terraform) read the JSON directly
via builtins.fromJSON / jsondecode.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Role = Literal["control-plane", "worker", "laptop", "non-k8s"]
ManagedBy = Literal["tofu-ovh", "tofu-proxmox", "nixos", "ansible", "mobile"]

# Cilium depends on the mesh-wide Nebula TUN MTU staying at 1420. A host with a
# smaller path can instead advertise a conservative destination-specific route
# MTU without changing that shared interface contract.
MIN_DESTINATION_MTU = 500
MESH_TUN_MTU = 1420


class Host(BaseModel):
    """One entry in the mesh host roster."""

    model_config = ConfigDict(extra="forbid")

    nebula_ip: str = Field(description="Mesh IP, no mask, e.g. '10.42.0.30'")
    endpoint: str | None = Field(
        default=None, description="Public 'host:port' if the node accepts inbound; absent for behind-NAT hosts"
    )
    role: Role = Field(description="K8s scheduling class, or 'non-k8s' for mesh-only participants")
    lighthouse: bool = Field(default=False, description="Acts as a Nebula lighthouse for others")
    relay: bool = Field(default=False, description="Acts as a Nebula relay for NAT'd peers")
    managed_by: ManagedBy = Field(description="Who provisions this host and its cert")
    cert_groups: list[str] = Field(
        default_factory=list, description="Groups embedded in the Nebula cert (currently unused by firewall rules)"
    )
    destination_mtu: int | None = Field(
        default=None,
        strict=True,
        ge=MIN_DESTINATION_MTU,
        le=MESH_TUN_MTU,
        description="Optional MTU all mesh paths involving this host must use",
    )

    @field_validator("destination_mtu", mode="before")
    @classmethod
    def destination_mtu_must_not_be_null(cls, value: object) -> object:
        """Keep direct JSON consumers from interpreting null as an MTU."""
        if value is None:
            raise ValueError("omit destination_mtu instead of setting it to null")
        return value


class Mesh(BaseModel):
    """The full mesh roster."""

    model_config = ConfigDict(extra="ignore")  # ignore _comment

    hosts: dict[str, Host]

    def lighthouses(self) -> list[Host]:
        return [h for h in self.hosts.values() if h.lighthouse]

    def lighthouse_ips(self) -> list[str]:
        return [h.nebula_ip for h in self.lighthouses()]

    def static_host_map(self) -> dict[str, list[str]]:
        return {h.nebula_ip: [h.endpoint] for h in self.hosts.values() if h.endpoint is not None}

    def control_plane_endpoints(self, port: int = 6443) -> list[str]:
        return [f"{h.nebula_ip}:{port}" for h in self.hosts.values() if h.role == "control-plane"]

    def minimum_path_mtu(self, default: int) -> int:
        """Return a global fallback for consumers without per-peer MTU routes."""
        constraints = [h.destination_mtu for h in self.hosts.values() if h.destination_mtu is not None]
        return min([default, *constraints])


def repo_root() -> Path:
    """Locate the repo root (the directory containing nebula-mesh.json)."""
    workspace = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    if workspace:
        return Path(workspace)
    path = Path.cwd().resolve()
    for candidate in [path, *path.parents]:
        if (candidate / "nebula-mesh.json").exists():
            return candidate
    raise RuntimeError("could not find repo root containing nebula-mesh.json")


def load(path: Path | None = None) -> Mesh:
    """Load and validate the mesh roster. Defaults to <repo_root>/nebula-mesh.json."""
    if path is None:
        path = repo_root() / "nebula-mesh.json"
    return Mesh.model_validate(json.loads(path.read_text()))
