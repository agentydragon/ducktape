"""Agent-specific logging configuration utilities."""

from __future__ import annotations

import logging

from agent_core.logging_utils import configure_logging


def configure_logging_info(*, set_stream_handler_level: bool = True) -> None:
    """Configure logging with INFO level for agent commands.

    Calls the base configure_logging() and optionally sets StreamHandler level to INFO,
    useful for interactive agent commands where we want to see INFO-level logs
    without DEBUG-level noise.

    Args:
        set_stream_handler_level: If True, explicitly sets all StreamHandler instances
            on the root logger to INFO level. This overrides the default WARNING level
            from configure_logging(). Set to False if you want to keep the default
            behavior (e.g., for background services like matrix_bot).
    """
    configure_logging()
    if set_stream_handler_level:
        for h in logging.getLogger().handlers:
            if isinstance(h, logging.StreamHandler):
                h.setLevel(logging.INFO)
