"""Logging configuration and Typer callback for CLI applications."""

from __future__ import annotations

import os
from enum import StrEnum
from logging.config import dictConfig
from pathlib import Path
from typing import Annotated

import typer


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


def configure_logging(log_output: str | None = None, log_level: str | LogLevel | None = None) -> None:
    """Single source of truth for logging configuration.

    - Routes ALL logs through stdlib logging
    - Configurable output destination and level
    - No prints; no library-specific handlers

    If not specified, reads from ADGN_LOG_OUTPUT and ADGN_LOG_LEVEL environment
    variables, defaulting to stderr/INFO.
    """
    if log_output is None:
        log_output = os.environ.get("ADGN_LOG_OUTPUT", "stderr")
    if log_level is None:
        log_level = os.environ.get("ADGN_LOG_LEVEL", LogLevel.INFO)

    # Normalize and validate log level
    log_level_upper = log_level.upper() if isinstance(log_level, str) else log_level
    try:
        log_level_enum = LogLevel(log_level_upper)
    except ValueError:
        raise ValueError(f"Invalid log level: {log_level}. Must be one of {', '.join(LogLevel)}") from None

    # Determine handler configuration based on log_output
    if log_output == "none":
        handlers_config: dict = {"null": {"class": "logging.NullHandler"}}
        root_handlers = ["null"]
    elif log_output in ("stdout", "stderr"):
        stream = "ext://sys.stdout" if log_output == "stdout" else "ext://sys.stderr"
        handlers_config = {
            "console": {
                "class": "logging.StreamHandler",
                "level": log_level_enum,
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
                "level": log_level_enum,
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
            "root": {"level": log_level_enum, "handlers": root_handlers},
        }
    )


def make_logging_callback(default_level: LogLevel = LogLevel.INFO):
    """Create a Typer callback that configures logging."""

    def _callback(
        log_output: Annotated[
            str,
            typer.Option(
                "--log-output",
                envvar="ADGN_LOG_OUTPUT",
                help="Where to send logs: 'stderr', 'stdout', 'none', or a file path",
            ),
        ] = "stderr",
        log_level: Annotated[
            str, typer.Option("--log-level", envvar="ADGN_LOG_LEVEL", help="Log level")
        ] = default_level,
    ) -> None:
        """Configure logging for all subcommands."""
        configure_logging(log_output=log_output, log_level=log_level)

    return _callback
