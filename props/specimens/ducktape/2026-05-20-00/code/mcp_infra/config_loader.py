from __future__ import annotations

import json
from pathlib import Path

from fastmcp.mcp_config import MCPConfig, MCPServerTypes


def load_mcp_file(path: Path) -> MCPConfig:
    """Load a single .mcp.json file into an MCPConfig (strict validation)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return MCPConfig.model_validate(data)


def build_mcp_config(mcp_configs: list[Path]) -> MCPConfig:
    """Merge baseline `./.mcp.json` (if present) with additional configs.

    Later configs override earlier ones by server name.
    """
    baseline = Path.cwd() / ".mcp.json"
    servers: dict[str, MCPServerTypes] = {}
    if baseline.exists():
        base = load_mcp_file(baseline)
        servers.update(base.mcpServers)
    for p in mcp_configs:
        if not p.exists():
            raise FileNotFoundError(f"--mcp-config not found: {p}")
        cfg = load_mcp_file(p)
        servers.update(cfg.mcpServers)
    return MCPConfig.model_validate({"mcpServers": servers})
