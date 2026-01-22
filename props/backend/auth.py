"""Shared authentication utilities and middleware for LLM and Registry proxies.

Both proxies use Basic auth with Postgres credentials validation:
- Admin users: Any valid Postgres user (non-agent_* username)
- Agent users: Format agent_{uuid} with temp credentials
- Localhost admin: Empty/no creds from localhost = admin (for local dev and dashboard)

This module provides:
- Common auth parsing utilities
- Starlette middleware for request-level auth
- Auth context attached to request.state
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from uuid import UUID

import psycopg
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

logger = logging.getLogger(__name__)

# Postgres configuration from environment
PGHOST = os.environ.get("PGHOST", "props-postgres")
PGPORT = os.environ.get("PGPORT", "5432")
PGDATABASE = os.environ.get("PGDATABASE", "eval_results")

# Localhost admin access - allow empty creds from localhost to act as admin
# This is useful for local development and dashboard/backend access
ALLOW_LOCALHOST_ADMIN = os.environ.get("PROPS_ALLOW_LOCALHOST_ADMIN", "true").lower() == "true"

# Trusted localhost addresses
LOCALHOST_ADDRESSES = {"127.0.0.1", "localhost", "::1"}


def validate_postgres_credentials(username: str, password: str) -> bool:
    """Validate credentials by attempting Postgres connection.

    Returns True if credentials are valid, False otherwise.
    """
    try:
        with psycopg.connect(
            host=PGHOST, port=PGPORT, dbname=PGDATABASE, user=username, password=password, connect_timeout=5
        ):
            return True
    except psycopg.OperationalError:
        return False


def parse_basic_auth_header(authorization: str | None) -> tuple[str, str] | None:
    """Parse Basic auth header into (username, password).

    Returns None if header is missing or invalid.
    """
    if not authorization or not authorization.startswith("Basic "):
        return None

    try:
        encoded = authorization.removeprefix("Basic ")
        decoded = base64.b64decode(encoded).decode("utf-8")
        username, password = decoded.split(":", 1)
        return (username, password)
    except (ValueError, UnicodeDecodeError) as e:
        logger.warning(f"Failed to parse Basic auth: {e}")
        return None


def extract_agent_run_id(username: str) -> UUID | None:
    """Extract agent_run_id from username if it matches agent_{uuid} pattern.

    Returns None if username doesn't match the pattern.
    """
    if not username.startswith("agent_"):
        return None

    try:
        return UUID(username.removeprefix("agent_"))
    except ValueError:
        logger.warning(f"Invalid UUID in agent username: {username}")
        return None


def is_localhost_request(request: Request) -> bool:
    """Check if request is from localhost.

    Checks the direct client address.
    """
    client = request.client
    return bool(client and client.host in LOCALHOST_ADDRESSES)


@dataclass
class AuthContext:
    """Authentication context attached to request.state.auth.

    Attributes:
        is_authenticated: True if valid credentials were provided or localhost admin
        is_admin: True if authenticated as admin (non-agent user or localhost)
        is_localhost_admin: True if authenticated via localhost without creds
        username: Authenticated username (None if anonymous or localhost admin)
        password: Authenticated password (None if anonymous or localhost admin)
        agent_run_id: UUID if authenticated as agent (None for admin/anonymous)
        error: Error message if auth failed (None if success or anonymous)
    """

    is_authenticated: bool = False
    is_admin: bool = False
    is_localhost_admin: bool = False
    username: str | None = None
    password: str | None = None
    agent_run_id: UUID | None = None
    error: str | None = None

    @classmethod
    def anonymous(cls) -> AuthContext:
        """Create anonymous (unauthenticated) context."""
        return cls(is_authenticated=False)

    @classmethod
    def localhost_admin(cls) -> AuthContext:
        """Create localhost admin context (no creds needed)."""
        return cls(is_authenticated=True, is_admin=True, is_localhost_admin=True)

    @classmethod
    def admin(cls, username: str, password: str) -> AuthContext:
        """Create admin context with credentials."""
        return cls(is_authenticated=True, is_admin=True, username=username, password=password)

    @classmethod
    def agent(cls, username: str, password: str, agent_run_id: UUID) -> AuthContext:
        """Create agent context."""
        return cls(
            is_authenticated=True, is_admin=False, username=username, password=password, agent_run_id=agent_run_id
        )

    @classmethod
    def failed(cls, error: str) -> AuthContext:
        """Create failed auth context."""
        return cls(is_authenticated=False, error=error)


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware that parses and validates auth, attaching context to request.state.

    After this middleware runs, request.state.auth will contain an AuthContext:
    - Localhost Admin: if no auth AND request is from localhost (and ALLOW_LOCALHOST_ADMIN)
    - Anonymous: if no Authorization header (and not localhost admin)
    - Admin: if valid non-agent credentials
    - Agent: if valid agent_{uuid} credentials
    - Failed: if invalid credentials (error message in context)

    Routes can then check request.state.auth and decide how to handle each case.
    This middleware does NOT reject requests - it only parses and validates.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Parse auth header and attach context to request.state."""
        authorization = request.headers.get("authorization")

        if not authorization:
            # No auth header - check for localhost admin access
            if ALLOW_LOCALHOST_ADMIN and is_localhost_request(request):
                logger.debug("Localhost admin access granted (no auth required)")
                request.state.auth = AuthContext.localhost_admin()
            else:
                # Anonymous access
                request.state.auth = AuthContext.anonymous()
        else:
            parsed = parse_basic_auth_header(authorization)
            if not parsed:
                # Malformed auth header
                request.state.auth = AuthContext.failed("Invalid authorization format")
            else:
                username, password = parsed

                # Validate credentials against Postgres
                if not validate_postgres_credentials(username, password):
                    logger.warning(f"Invalid postgres credentials for user: {username}")
                    request.state.auth = AuthContext.failed("Invalid credentials")
                else:
                    # Valid credentials - determine if admin or agent
                    agent_run_id = extract_agent_run_id(username)
                    if agent_run_id:
                        request.state.auth = AuthContext.agent(username, password, agent_run_id)
                    else:
                        request.state.auth = AuthContext.admin(username, password)

        return await call_next(request)


def get_auth_context(request: Request) -> AuthContext:
    """Get auth context from request.state.

    Use this in route handlers to access the auth context set by middleware.
    """
    return getattr(request.state, "auth", AuthContext.anonymous())
