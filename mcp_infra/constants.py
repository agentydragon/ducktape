"""Shared constants for MCP infrastructure."""

from typing import Final

SLEEP_FOREVER_CMD: Final[list[str]] = ["/bin/sh", "-lc", "sleep infinity"]
