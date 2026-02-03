"""Props configuration loaded from TOML file.

The config file path is specified by the PROPS_CONFIG_FILE environment variable.
Loaded at boundaries (CLI entry points, backend lifespan, test fixtures) and
passed explicitly — no singletons.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict

ENV_CONFIG_FILE = "PROPS_CONFIG_FILE"


class PropsConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    agent_env: dict[str, str]


def load_config(path: Path) -> PropsConfig:
    """Load props configuration from a TOML file."""
    data = tomllib.loads(path.read_text())
    return PropsConfig.model_validate(data)


def load_config_from_env() -> PropsConfig:
    """Load props configuration from the path in PROPS_CONFIG_FILE env var."""
    path_str = os.environ.get(ENV_CONFIG_FILE)
    if not path_str:
        raise ValueError(f"{ENV_CONFIG_FILE} environment variable not set")
    return load_config(Path(path_str))
