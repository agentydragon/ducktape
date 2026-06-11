"""Session-scoped logging system for Claude Code hooks."""

import json
import logging
from datetime import datetime
from typing import Any, NewType
from uuid import UUID

from llm.claude_code_api import SessionID
from llm.hook_session_state import get_session_dir

InvocationID = NewType("InvocationID", UUID)


class JSONFormatter(logging.Formatter):
    """JSON formatter for hook logs with session/invocation context."""

    def __init__(self, hook_name: str, session_id: SessionID, invocation_id: InvocationID):
        super().__init__()
        self.hook_name = hook_name
        self.session_id = session_id
        self.invocation_id = invocation_id

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON."""
        log_entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "hook_name": self.hook_name,
            "session_id": str(self.session_id),
            "invocation_id": str(self.invocation_id),
            "message": record.getMessage(),
            "logger_name": record.name,
        }

        # Add exception info if present
        if record.exc_info:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else None,
                "message": str(record.exc_info[1]) if record.exc_info[1] else None,
                "traceback": self.formatException(record.exc_info) if record.exc_info != (None, None, None) else None,
            }

        # Add standard logging fields that are useful for debugging
        standard_fields = [
            "module",
            "lineno",
            "filename",
            "funcName",
            "pathname",
            "process",
            "processName",
            "thread",
            "threadName",
        ]
        for field in standard_fields:
            log_entry[field] = getattr(record, field)

        # Add any extra fields under 'extra' key to avoid conflicts
        extra_fields = {}
        for key, value in record.__dict__.items():
            if key not in [
                "name",
                "msg",
                "args",
                "levelname",
                "levelno",
                "created",
                "msecs",
                "relativeCreated",
                "getMessage",
                "exc_info",
                "exc_text",
                "stack_info",
                "message",
                *standard_fields,
            ]:
                extra_fields[key] = value

        if extra_fields:
            log_entry["extra"] = extra_fields

        return json.dumps(log_entry, default=str)


def get_session_logger(hook_name: str, session_id: SessionID, invocation_id: InvocationID) -> logging.Logger:
    """
    Get a session-scoped logger that logs to the session directory.

    Returns a proper Python logger with exc_info support.

    Args:
        hook_name: Name of the hook
        session_id: Claude session ID
        invocation_id: Unique ID for this hook invocation

    Returns:
        Logger instance that logs to session directory in JSON format
    """
    # Create logger name that's unique to this invocation
    logger_name = f"hook.{hook_name}.{session_id}.{invocation_id}"
    logger = logging.getLogger(logger_name)

    # Only configure if not already configured
    if not logger.handlers:
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

        # Set up file logging to session directory
        session_dir = get_session_dir(hook_name, session_id)
        log_file = session_dir / "hook.log"

        handler = logging.FileHandler(log_file)
        handler.setFormatter(JSONFormatter(hook_name, session_id, invocation_id))
        logger.addHandler(handler)

    return logger
