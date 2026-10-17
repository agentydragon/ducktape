"""Shared constants for MCP infrastructure."""

from pathlib import Path
from typing import Final

WORKING_DIR: Final[Path] = Path("/workspace")

SLEEP_FOREVER_CMD: Final[list[str]] = ["/bin/sh", "-lc", "sleep infinity"]
