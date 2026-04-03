"""Manager configuration loaded from a YAML file."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class ManagerConfig(BaseModel):
    """Configuration for the Firecracker VM manager service."""

    vm_image: str = Field(description="OCI image for VM pods")
    base_rootfs_pvc: str = Field(description="Base PVC to clone for each VM's rootfs")
    node_selector: dict[str, str] | None = Field(
        default=None, description="Node selector for VM pods. None = schedule anywhere."
    )
    port: int = Field(default=8080, description="HTTP port for the manager API")


def load_config(path: Path) -> ManagerConfig:
    """Load manager config from a YAML file."""
    data = yaml.safe_load(path.read_text())
    return ManagerConfig.model_validate(data)
