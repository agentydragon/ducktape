"""Logging utilities for structured dual-output logging."""

import logging
import sys
from collections.abc import Callable, Mapping, MutableMapping
from pathlib import Path
from typing import Any, cast

import structlog

# Console arg display truncation length for tool args
ARG_TRUNCATE_LEN = 30


class ToolAwareConsoleRenderer(structlog.dev.ConsoleRenderer):
    """Console renderer that formats tool usage nicely for display."""

    def __call__(self, logger, method_name, event_dict):
        # Handle tool formatting for console display only when shapes are correct
        if event_dict.get("event") == "Tool usage" and "tools" in event_dict:
            tools = event_dict["tools"]
            if (
                isinstance(tools, list)
                and tools
                and all(
                    isinstance(t, dict) and isinstance(t.get("name"), str) and isinstance(t.get("args"), dict)
                    for t in tools
                )
            ):
                formatted_tools: list[str] = []
                # Type narrowing: tools is list[dict] at this point
                for tool in cast(list[dict[str, Any]], tools):
                    # Format args with truncation for console
                    args_parts = []
                    for k, v in tool["args"].items():
                        v_str = str(v)
                        if len(v_str) > ARG_TRUNCATE_LEN:
                            v_str = v_str[:ARG_TRUNCATE_LEN] + "..."
                        args_parts.append(f"{k}={v_str}")
                    args_str = ", ".join(args_parts)
                    formatted_tools.append(f"{tool['name']}({args_str})")
                # Replace tools with formatted version for console
                event_dict = event_dict.copy()
                event_dict["tools"] = formatted_tools

        # Use parent renderer for actual formatting
        return super().__call__(logger, method_name, event_dict)


class DualOutputLogging:
    """Utility class for setting up structured logging with dual output."""

    @staticmethod
    def setup_logging(logs_dir: str = "logs", log_filename: str = "optimizer.jsonl", verbose: bool = False) -> None:
        """Configure structlog with dual output: pretty console + structured file logs.

        Args:
            logs_dir: Directory to store log files
            log_filename: Name of the JSON log file
            verbose: Enable DEBUG level logging to console
        """
        # Create logs directory if it doesn't exist
        logs_path = Path(logs_dir)
        logs_path.mkdir(exist_ok=True)

        # Configure timestamper and shared processors
        timestamper = structlog.processors.TimeStamper(fmt="iso")
        shared_processors: list[
            Callable[
                [Any, str, MutableMapping[str, Any]], Mapping[str, Any] | str | bytes | bytearray | tuple[Any, ...]
            ]
        ] = [
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            timestamper,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
        ]

        # Configure structlog
        structlog.configure(
            processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )

        # Create formatters
        console_formatter = structlog.stdlib.ProcessorFormatter(
            processor=ToolAwareConsoleRenderer(colors=True, pad_event=30, repr_native_str=False)
        )

        file_formatter = structlog.stdlib.ProcessorFormatter(processor=structlog.processors.JSONRenderer())

        # Setup handlers
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(console_formatter)
        console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)

        file_handler = logging.FileHandler(logs_path / log_filename)
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(logging.INFO)

        # Configure root logger
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG if verbose else logging.INFO)
        root_logger.handlers.clear()
        root_logger.addHandler(console_handler)
        root_logger.addHandler(file_handler)

        # If verbose, also configure minicodx logger specifically
        if verbose:
            minicodx_logger = logging.getLogger("minicodex")
            minicodx_logger.setLevel(logging.DEBUG)

    @staticmethod
    def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
        """Get a structured logger instance.

        Args:
            name: Logger name (optional)

        Returns:
            Structured logger instance
        """
        # structlog.get_logger returns an stdlib BoundLogger when configured accordingly
        return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))
