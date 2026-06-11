"""Contextual logging support for Claude hooks."""

import logging
from contextvars import ContextVar
from uuid import UUID

# Context variables for storing hook execution context
hook_invocation_id: ContextVar[str | None] = ContextVar("hook_invocation_id", default=None)
hook_name: ContextVar[str | None] = ContextVar("hook_name", default=None)
hook_session_id: ContextVar[UUID | None] = ContextVar("hook_session_id", default=None)


class HookContextFilter(logging.Filter):
    """Logging filter that injects hook context into log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Add hook context to log record."""
        # Get context values, defaulting to empty string if not set
        invocation_id = hook_invocation_id.get() or ""
        name = hook_name.get() or ""
        session_id = hook_session_id.get()

        # Add context to the record
        record.hook_invocation_id = invocation_id
        record.hook_name = name
        record.hook_session_id = str(session_id) if session_id else ""

        return True


def set_hook_context(invocation_id: str, name: str, session_id: UUID) -> None:
    """Set the current hook execution context."""
    hook_invocation_id.set(invocation_id)
    hook_name.set(name)
    hook_session_id.set(session_id)


def clear_hook_context() -> None:
    """Clear the current hook execution context."""
    hook_invocation_id.set(None)
    hook_name.set(None)
    hook_session_id.set(None)


def get_current_invocation_id() -> str | None:
    """Get the current hook invocation ID."""
    return hook_invocation_id.get()


def setup_hook_logging() -> None:
    """Set up logging with hook context support."""
    # Get root logger and add our context filter
    root_logger = logging.getLogger()

    # Remove any existing HookContextFilter to avoid duplicates
    for handler in root_logger.handlers:
        handler.filters = [f for f in handler.filters if not isinstance(f, HookContextFilter)]

    # Add our context filter to all handlers
    context_filter = HookContextFilter()
    for handler in root_logger.handlers:
        handler.addFilter(context_filter)

    # Update formatter to include context
    formatter = logging.Formatter(
        fmt="%(asctime)s - %(name)s - %(levelname)s - [%(hook_invocation_id)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    for handler in root_logger.handlers:
        handler.setFormatter(formatter)
