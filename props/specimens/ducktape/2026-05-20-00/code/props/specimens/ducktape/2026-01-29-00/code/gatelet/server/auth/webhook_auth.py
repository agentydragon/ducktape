"""Webhook authentication handlers for Gatelet."""

from abc import ABC, abstractmethod
from typing import Any

from fastapi import Request
from fastapi.security import HTTPAuthorizationCredentials

from gatelet.server.auth.handlers import AuthType
from gatelet.server.config import BearerAuth, NoAuth, WebhookAuthConfig


class AuthError(Exception):
    """Base exception for authentication errors."""

    def __init__(self, detail: str, headers: dict[str, str] | None = None):
        self.detail = detail
        self.headers = headers or {}
        super().__init__(detail)


class WebhookAuthHandler(ABC):
    """Base class for webhook authentication handlers."""

    @abstractmethod
    async def validate(self, request: Request, credentials: HTTPAuthorizationCredentials | None) -> None:
        """Validate authentication for a webhook."""


class NoAuthHandler(WebhookAuthHandler):
    """Authentication handler that requires no authentication."""

    def __init__(self, config: NoAuth):
        self.config = config

    async def validate(self, request: Request, credentials: HTTPAuthorizationCredentials | None) -> None:
        """Always approve - no authentication required."""


class BearerAuthHandler(WebhookAuthHandler):
    """Authentication handler using Bearer token."""

    def __init__(self, config: BearerAuth):
        self.config = config

    async def validate(self, request: Request, credentials: HTTPAuthorizationCredentials | None) -> None:
        """Validate Bearer token authentication."""
        bearer_headers = {"WWW-Authenticate": "Bearer"}

        if not credentials:
            raise AuthError("Missing Authorization header", headers=bearer_headers)

        if credentials.scheme.lower() != "bearer":
            raise AuthError("Invalid authentication scheme", headers=bearer_headers)

        if credentials.credentials != self.config.token:
            raise AuthError("Invalid token", headers=bearer_headers)


def create_auth_handler(config: WebhookAuthConfig | dict[str, Any]) -> WebhookAuthHandler:
    """Factory function to create appropriate authentication handler."""
    if isinstance(config, dict):
        auth_type = config.get("type")
        if auth_type == AuthType.NONE:
            return NoAuthHandler(NoAuth())
        if auth_type == AuthType.BEARER:
            return BearerAuthHandler(BearerAuth(token=config.get("token", "")))
        raise ValueError(f"Unknown authentication type in dict: {auth_type}")
    if isinstance(config, NoAuth):
        return NoAuthHandler(config)
    if isinstance(config, BearerAuth):
        return BearerAuthHandler(config)
    raise ValueError(f"Unknown authentication type: {type(config)}")
