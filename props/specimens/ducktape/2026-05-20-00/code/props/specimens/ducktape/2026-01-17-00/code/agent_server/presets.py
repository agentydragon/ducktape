from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from fastmcp.mcp_config import MCPConfig
from platformdirs import user_config_dir
from pydantic import BaseModel, Field

from agent_server.persist.types import AgentMetadata

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from agent_server.agent_types import AgentID
    from agent_server.persist.sqlite import SQLitePersistence


def _xdg_presets_dir() -> Path:
    """Return the default XDG-compliant presets directory.

    Uses platformdirs to resolve the user configuration directory for app "adgn",
    then appends the "presets" subfolder.
    """
    cfg_root = Path(user_config_dir("adgn"))
    return cfg_root / "presets"


class AgentPreset(BaseModel):
    name: str
    description: str | None = None
    system: str | None = None
    mcp: MCPConfig = Field(default_factory=lambda: MCPConfig(mcpServers={}))
    approval_policy: str | None = None
    # Source metadata (filled by loader; used by UI)
    file_path: str | None = None
    modified_at: str | None = None  # ISO-8601 string


def load_presets_from_dir(root: Path) -> dict[str, AgentPreset]:
    out: dict[str, AgentPreset] = {}
    if not root.exists() or not root.is_dir():
        return out
    for p in sorted(root.glob("*.y*ml")):
        with p.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ValueError(f"preset must be a mapping: {p}")
        # Default name from filename when missing in YAML
        if "name" not in data or not data.get("name"):
            data["name"] = p.stem
        preset = AgentPreset.model_validate(data)
        stat = p.stat()  # Fail fast on OS errors
        preset.file_path = str(p)
        preset.modified_at = datetime.fromtimestamp(stat.st_mtime).isoformat()
        if preset.name in out:
            logger.warning(
                f"Preset name collision: '{preset.name}' from {p} overrides preset from {out[preset.name].file_path}"
            )
        out[preset.name] = preset
    return out


def discover_presets(*, override_dir: str | Path | None = None) -> dict[str, AgentPreset]:
    """Search for preset files in configured and default directories.

    Args:
        override_dir: Optional directory to use instead of env var + defaults.
                     Useful for testing. If None, uses ADGN_AGENT_PRESETS_DIR
                     env var (if set) followed by XDG config directory.

    Precedence: override_dir > ADGN_AGENT_PRESETS_DIR env > XDG config
    Later directories do not override earlier names.
    """
    out: dict[str, AgentPreset] = {}
    roots: list[Path] = []

    # Handle override for testing
    if override_dir is not None:
        roots.append(Path(override_dir))
    else:
        # Read env var internally (production path)
        env_dir = os.getenv("ADGN_AGENT_PRESETS_DIR")
        if env_dir:
            roots.append(Path(env_dir))

    # Always check XDG directory
    roots.append(_xdg_presets_dir())

    for r in roots:
        for name, preset in load_presets_from_dir(r).items():
            if name not in out:
                out[name] = preset
            else:
                logger.warning(
                    f"Preset name collision: '{name}' from {preset.file_path} skipped (already loaded from {out[name].file_path})"
                )
    # Always include a built-in default if none present
    if "default" not in out:
        out["default"] = AgentPreset(name="default", description="Default UI agent", system=None)
    return out


async def create_agent_from_preset(
    persistence: SQLitePersistence, preset_name: str | None, base_mcp_config: MCPConfig
) -> tuple[AgentID, MCPConfig, str | None]:
    """Create a new agent from a preset.

    Args:
        persistence: SQLite persistence for storing agent record
        preset_name: Name of preset to use (defaults to "default")
        base_mcp_config: Base MCP config to merge with preset specs

    Returns:
        Tuple of (agent_id, mcp_config, system_prompt)
    """
    # Load preset
    presets = discover_presets()
    requested_name = preset_name or "default"
    if requested_name not in presets:
        available = ", ".join(sorted(presets.keys()))
        raise ValueError(f"Unknown preset '{requested_name}'. Available presets: {available}")
    preset = presets[requested_name]

    # Merge preset MCP config with base config
    mcp_config = base_mcp_config.model_copy(deep=True)
    # Merge preset's MCP servers into the config
    if preset.mcp and preset.mcp.mcpServers:
        for name, spec in preset.mcp.mcpServers.items():
            mcp_config.mcpServers[name] = spec

    # Create agent record in DB
    metadata = AgentMetadata(preset=preset.name)
    agent_id = await persistence.create_agent(mcp_config=mcp_config, metadata=metadata)

    return agent_id, mcp_config, preset.system
