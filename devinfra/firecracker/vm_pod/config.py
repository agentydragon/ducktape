"""VM pod configuration loaded from a YAML file."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class VMPodConfig(BaseModel):
    """Configuration for the Firecracker VM pod entrypoint.

    The pod is infrastructure-only: it starts Firecracker, sets up
    networking, and exposes the FC API on :2026. All VM configuration
    (boot, drives, snapshot/load) is done by the manager via the proxy.

    The rootfs PVC is created and attached by the manager before the pod
    starts — the entrypoint doesn't need to know about it.
    """

    firecracker_bin: Path = Field(
        default=Path("/usr/local/bin/firecracker"), description="Path to the Firecracker binary"
    )


def load_config(path: Path) -> VMPodConfig:
    """Load VM pod config from a YAML file."""
    data = yaml.safe_load(path.read_text())
    return VMPodConfig.model_validate(data)
