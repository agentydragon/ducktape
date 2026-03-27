# TODO: Consider factoring out more shared utilities into agent_core
from __future__ import annotations

import logging
import os
from logging.config import dictConfig
from pathlib import Path

import structlog
from structlog.typing import Processor

# Valid log levels (single source of truth)
VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def configure_logging(log_output: str = "stderr", log_level: str = "WARNING") -> None:
    """Single source of truth for logging configuration.

    Routes ALL logs through stdlib logging (structlog uses stdlib LoggerFactory).
    Configurable output destination and level. No prints; no library-specific handlers.
    """
    # Validate log level
    log_level_upper = log_level.upper()
    if log_level_upper not in VALID_LOG_LEVELS:
        raise ValueError(f"Invalid log level: {log_level}. Must be one of {', '.join(VALID_LOG_LEVELS)}")

    # Determine handler configuration based on log_output
    if log_output == "none":
        handlers_config: dict = {"null": {"class": "logging.NullHandler"}}
        root_handlers = ["null"]
    elif log_output in ("stdout", "stderr"):
        stream = "ext://sys.stdout" if log_output == "stdout" else "ext://sys.stderr"
        handlers_config = {
            "console": {
                "class": "logging.StreamHandler",
                "level": log_level_upper,
                "formatter": "console",
                "stream": stream,
            }
        }
        root_handlers = ["console"]
    else:
        # Treat as file path
        handlers_config = {
            "file": {
                "class": "logging.FileHandler",
                "level": log_level_upper,
                "formatter": "file",
                "filename": str(Path(log_output).resolve()),
                "encoding": "utf-8",
            }
        }
        root_handlers = ["file"]

    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "console": {"format": "%(levelname)s %(name)s: %(message)s"},
                "file": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
            },
            "handlers": handlers_config,
            "root": {"level": log_level_upper, "handlers": root_handlers},
        }
    )

    # Route structlog through stdlib, obeying the same handlers
    procs: list[Processor] = [structlog.processors.TimeStamper(fmt="iso"), structlog.processors.add_log_level]
    if os.getenv("MINICODEX_DEBUG"):
        procs.append(structlog.processors.JSONRenderer())
    else:
        procs.append(structlog.processors.KeyValueRenderer(key_order=["event"]))

    structlog.configure(
        processors=procs,
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )
