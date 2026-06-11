"""Configuration utilities for Habitify MCP server."""

import os

from dotenv import load_dotenv


def load_api_key(exit_on_missing: bool = True) -> str | None:
    """Load the Habitify API key from environment.

    Returns the API key if found, None if not found and exit_on_missing is False.
    Raises SystemExit if exit_on_missing is True and no API key is found.
    """
    load_dotenv()
    api_key = os.environ.get("HABITIFY_API_KEY")

    if not api_key and exit_on_missing:
        raise SystemExit(
            "HABITIFY_API_KEY environment variable is required. Set it in .env or export HABITIFY_API_KEY=your_key"
        )

    return api_key


def get_api_base_url() -> str | None:
    """Get the optional Habitify API base URL from environment."""
    return os.environ.get("HABITIFY_API_BASE_URL")
