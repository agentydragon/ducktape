"""Constants and enums for the Habitify MCP Server."""

from enum import IntEnum


class HTTPStatus(IntEnum):
    """HTTP status codes used in the application."""

    OK = 200
    BAD_REQUEST = 400
    UNAUTHORIZED = 401
    NOT_FOUND = 404
    INTERNAL_SERVER_ERROR = 500


# Error messages
ERROR_MESSAGES = {
    HTTPStatus.UNAUTHORIZED: "Authentication failed. Please check your API key.",
    HTTPStatus.NOT_FOUND: "Resource not found.",
    HTTPStatus.INTERNAL_SERVER_ERROR: "Internal server error occurred.",
}

# Default values
DEFAULT_DATE_FORMAT = "%Y-%m-%d"
DEFAULT_TIMEZONE = "UTC"
