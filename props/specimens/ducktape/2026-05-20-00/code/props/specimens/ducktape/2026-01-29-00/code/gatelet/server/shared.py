"""Shared resources and utilities for the Gatelet server.

This module provides template helper utilities.
App-level resources (database, templates) are managed via FastAPI app.state in lifespan.py.
"""

from gatelet.server.config import Settings


def make_ha_history_url(settings: Settings):
    """Create ha_history_url helper function bound to settings.

    Returns a function that can be passed to template context.
    Usage in templates: {{ ha_history_url(entity_id) }}

    Args:
        settings: Settings instance to bind to the helper

    Returns:
        Callable that takes entity_id and returns URL string
    """

    def helper(entity_id: str) -> str:
        base = settings.home_assistant.api_url.rstrip("/")
        return f"{base}/history?entity_id={entity_id}"

    return helper
