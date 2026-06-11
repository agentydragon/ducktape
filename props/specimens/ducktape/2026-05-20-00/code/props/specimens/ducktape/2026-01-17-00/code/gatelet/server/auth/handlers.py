"""Authentication handlers for Gatelet endpoints."""

import logging
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlencode

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatelet.server.auth.key_auth import KeyAuthError, validate_key
from gatelet.server.config import Settings, get_settings
from gatelet.server.database import get_db_session
from gatelet.server.models import AdminSession, AuthCRSession, AuthKey

logger = logging.getLogger(__name__)


class AuthType(StrEnum):
    """Authentication method types."""

    KEY_PATH = "key_path"
    SESSION = "session"
    ADMIN = "admin"
    NONE = "none"
    BEARER = "bearer"


class AuthHandlerError(Exception):
    """Common exception for all authentication errors."""


class AuthContext(Protocol):
    """Authentication context with information for navigation."""

    @property
    def auth_type(self) -> AuthType:
        """Authentication method type."""
        ...

    def create_url(self, path: str) -> str:
        """Create authenticated URL for a given path.

        Args:
            path: Path to authenticate (should not start with /)

        Returns:
            URL with authentication component
        """
        ...

    def create_url_with_params(self, path: str, **query_params) -> str:
        """Create authenticated URL with query parameters.

        Args:
            path: Path to authenticate (should not start with /)
            **query_params: Query parameters as keyword arguments

        Returns:
            URL with authentication component and query parameters
        """
        base_url = self.create_url(path)
        if not query_params:
            return base_url

        return f"{base_url}?{urlencode(query_params)}"


class KeyPathAuthContext:
    """Authentication context for key-in-path."""

    def __init__(self, auth_key: AuthKey):
        self.key = auth_key

    @property
    def auth_type(self) -> AuthType:
        return AuthType.KEY_PATH

    @property
    def key_value(self) -> str:
        """Get the authentication key value."""
        return self.key.key_value

    def create_url(self, path: str) -> str:
        return f"/k/{self.key_value}/{path}"

    def create_url_with_params(self, path: str, **query_params) -> str:
        """Create authenticated URL with query parameters."""
        base_url = self.create_url(path)
        if not query_params:
            return base_url

        return f"{base_url}?{urlencode(query_params)}"


class SessionAuthContext:
    """Authentication context for session-based auth."""

    def __init__(self, session: AuthCRSession):
        self.session = session

    @property
    def auth_type(self) -> AuthType:
        return AuthType.SESSION

    @property
    def session_token(self) -> str:
        """Get the session token."""
        return self.session.session_token

    def create_url(self, path: str) -> str:
        return f"/s/{self.session_token}/{path}"

    def create_url_with_params(self, path: str, **query_params) -> str:
        """Create authenticated URL with query parameters."""
        base_url = self.create_url(path)
        if not query_params:
            return base_url

        return f"{base_url}?{urlencode(query_params)}"


class AdminAuthContext:
    """Authentication context for admin sessions."""

    def __init__(self, session: AdminSession):
        self.session = session

    @property
    def auth_type(self) -> AuthType:
        return AuthType.ADMIN

    def create_url(self, path: str) -> str:
        return f"/{path}"

    def create_url_with_params(self, path: str, **query_params) -> str:
        base_url = self.create_url(path)
        if not query_params:
            return base_url

        return f"{base_url}?{urlencode(query_params)}"


async def key_path_auth(
    key: str, db_session: AsyncSession = Depends(get_db_session), settings: Settings = Depends(get_settings)
) -> KeyPathAuthContext:
    """Authenticate using key in path."""
    logger.debug("key_path_auth called with key: %s...", key[:4])

    try:
        logger.debug("Validating key")
        auth_key = await validate_key(key, db_session, settings.auth.key_in_url.key_validity)
        logger.debug("Key validation successful")
        return KeyPathAuthContext(auth_key)
    except KeyAuthError:
        logger.warning("Key authentication failed for key: %s...", key[:4])
        raise AuthHandlerError


async def session_auth(
    session_token: str, db_session: AsyncSession = Depends(get_db_session), settings: Settings = Depends(get_settings)
) -> SessionAuthContext:
    """Authenticate using challenge-response session token."""
    # Find session
    query = select(AuthCRSession).where(AuthCRSession.session_token == session_token)
    session = (await db_session.execute(query)).scalar_one_or_none()

    if not session or not session.is_valid:
        raise AuthHandlerError

    # Extend session if needed
    now = datetime.now()
    session.last_activity_at = now
    new_exp = now + settings.auth.challenge_response.session_extension
    max_exp = session.created_at + settings.auth.challenge_response.session_max_duration
    if new_exp > session.expires_at:
        session.expires_at = min(new_exp, max_exp)

    # Flush changes without committing if in an external transaction
    # This ensures changes are visible within the transaction but don't
    # interfere with external transaction management
    await db_session.flush()

    # Create SessionAuthContext with the updated session
    return SessionAuthContext(session)


async def admin_auth(session_token: str, db_session: AsyncSession = Depends(get_db_session)) -> AdminAuthContext:
    """Authenticate using admin session token."""
    query = select(AdminSession).where(AdminSession.session_token == session_token)
    admin_session = (await db_session.execute(query)).scalar_one_or_none()
    if not admin_session or admin_session.expires_at <= datetime.now():
        raise AuthHandlerError

    return AdminAuthContext(admin_session)


def create_auth_dependency(auth_type: AuthType) -> Callable:
    """Create an authentication dependency based on auth type."""
    if auth_type == AuthType.KEY_PATH:
        return key_path_auth
    if auth_type == AuthType.SESSION:
        return session_auth
    if auth_type == AuthType.ADMIN:
        return admin_auth
    raise ValueError(f"Unsupported {auth_type = }")
