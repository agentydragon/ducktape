"""Utility functions for the Habitify MCP server."""

from ..config import load_api_key

STATUS_COLORS = {"completed": "green", "skipped": "yellow", "failed": "red", "none": "blue"}


def get_status_color(status: str) -> str:
    """Get the color code for a habit status."""
    return STATUS_COLORS.get(status.lower(), "white")


def format_rich_status(status: str) -> str:
    """Format a status string with Rich formatting."""
    color = get_status_color(status)
    return f"[{color}]{status.capitalize()}[/]"


def get_api_key_from_param_or_env(api_key_param: str | None = None) -> str | None:
    """Get API key from CLI parameter or environment."""
    return api_key_param or load_api_key(exit_on_missing=False)
