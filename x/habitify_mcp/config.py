"""Configuration utilities for Habitify MCP server."""

import os

from dotenv import load_dotenv


def load_api_key(explicit: str | None = None) -> str | None:
    """Use a supplied key, then the environment with .env as a fallback."""
    if explicit:
        return explicit
    load_dotenv()
    return os.environ.get("HABITIFY_API_KEY")
