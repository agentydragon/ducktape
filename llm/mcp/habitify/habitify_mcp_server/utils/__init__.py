"""
Utility functions for the Habitify MCP server.
"""

import functools
import os
from typing import Any, Callable, Optional, TypeVar, cast

from .date_utils import (
    create_date_range,
    format_date_for_api,
    format_date_human,
    format_date_yyyy_mm_dd,
    parse_date,
    validate_date_format,
)
from .error_utils import (
    classify_error,
    create_auth_error,
    create_error_response,
    create_not_found_error,
    create_validation_error,
)

# Avoid circular import by not importing from habit_resolver.py here

# Define status colors mapping
STATUS_COLORS = {
    "completed": "green",
    "skipped": "yellow",
    "failed": "red",
    "none": "blue",
}

# Define type variables for function annotations
T = TypeVar("T")
F = TypeVar("F", bound=Callable[..., Any])


def get_status_color(status: str) -> str:
    """
    Get the color code for a habit status.

    Args:
        status: The status string (completed, skipped, failed, none)

    Returns:
        Color name for the given status
    """
    return STATUS_COLORS.get(status.lower(), "white")


def format_rich_status(status: str) -> str:
    """
    Format a status string with Rich formatting.

    Args:
        status: The status string (completed, skipped, failed, none)

    Returns:
        Rich-formatted status string with appropriate color
    """
    color = get_status_color(status)
    return f"[{color}]{status.capitalize()}[/]"


def get_api_key_from_param_or_env(api_key_param: Optional[str] = None) -> Optional[str]:
    """
    Get API key from parameter or environment.

    Args:
        api_key_param: Optional API key from command line parameter

    Returns:
        API key from parameter or environment variable
    """
    return api_key_param or os.environ.get("HABITIFY_API_KEY")


def get_server_api_key() -> Optional[str]:
    """
    Get API key from MCP server metadata context.

    Returns:
        API key from server metadata or None
    """
    try:
        from mcp.shared.context import get_server_metadata

        server_metadata = get_server_metadata()
        if isinstance(server_metadata, dict):
            return server_metadata.get("api_key")
        # Handle newer FastMCP that might use a different metadata structure
        api_key = getattr(server_metadata, "api_key", None)
        if api_key:
            return api_key
        return None
    except Exception:
        return None


def validate_required_params(
    *param_names: str, **params: Any
) -> Optional[dict[str, Any]]:
    """
    Validate that at least one of the specified parameters is not None.

    Args:
        *param_names: Names of parameters to check
        **params: Parameter values to validate

    Returns:
        None if validation passed, or dict with error info if failed
    """
    # Filter to only include the specified parameters
    if param_names:
        filtered_params = {
            name: params.get(name) for name in param_names if name in params
        }
    else:
        filtered_params = params

    # Check if at least one parameter is not None
    if not any(value is not None for value in filtered_params.values()):
        param_list = ", ".join(filtered_params.keys())
        return {
            "error": f"At least one of these parameters is required: {param_list}",
            "params": list(filtered_params.keys()),
        }

    return None


def with_api_key(func: F) -> F:
    """
    Decorator to inject the API key from server context or environment.

    Args:
        func: Function to decorate

    Returns:
        Decorated function
    """

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        # Get API key
        api_key = get_server_api_key()
        if not api_key:
            from ..habitify_client import HabitifyError

            raise HabitifyError(
                "API key is required. Set HABITIFY_API_KEY environment variable or configure server metadata."
            )

        # Add API key to kwargs if not already present
        if "api_key" not in kwargs:
            kwargs["api_key"] = api_key

        return await func(*args, **kwargs)

    return cast(F, wrapper)


def with_client(func: F) -> F:
    """
    Decorator to handle common client creation and error handling.

    Args:
        func: Function to decorate

    Returns:
        Decorated function
    """

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            # Get API key
            api_key = get_server_api_key()
            if not api_key:
                return create_auth_error(
                    "API key is required. Set HABITIFY_API_KEY environment variable or configure server metadata."
                )

            # Import here to avoid circular imports
            from ..habitify_client import HabitifyClient

            # Create client and call function
            with HabitifyClient(api_key=api_key) as client:
                # Add client to kwargs
                kwargs["client"] = client
                return await func(*args, **kwargs)
        except Exception as e:
            return create_error_response(e)

    return cast(F, wrapper)


__all__ = [
    # Date utilities
    "parse_date",
    "format_date_yyyy_mm_dd",
    "format_date_for_api",
    "format_date_human",
    "create_date_range",
    "validate_date_format",
    # Error handling utilities
    "create_error_response",
    "create_validation_error",
    "create_not_found_error",
    "create_auth_error",
    "classify_error",
    # Status formatting
    "STATUS_COLORS",
    "get_status_color",
    "format_rich_status",
    # API key helpers
    "get_api_key_from_param_or_env",
    "get_server_api_key",
    # Parameter validation
    "validate_required_params",
    # Function decorators
    "with_api_key",
    "with_client",
    # Type variables
    "T",
    "F",
]
