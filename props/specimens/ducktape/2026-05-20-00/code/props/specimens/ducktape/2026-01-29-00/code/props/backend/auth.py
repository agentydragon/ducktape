"""Shared authentication utilities and middleware for backend APIs.

Authentication uses Basic auth with Postgres credentials validation:
- Admin users: Any valid Postgres user (non-agent_* username)
- Agent users: Format agent_{uuid} with temp credentials
- Localhost admin: Empty/no creds from localhost = admin (for local dev and dashboard)

This module provides:
- Credential validation with access level determination
- Caller type determination for ACL enforcement
- Starlette middleware for request-level auth
- Auth context attached to request.state
- Dependency functions for ACL enforcement (require_read, require_push, require_eval_access)
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

import psycopg
from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from props.db.config import get_database_config
from props.db.models import AgentRun, AgentType
from props.db.session import get_session

logger = logging.getLogger(__name__)

# Localhost admin access - allow empty creds from localhost to act as admin
# This is useful for local development and dashboard/backend access
ALLOW_LOCALHOST_ADMIN = os.environ.get("PROPS_ALLOW_LOCALHOST_ADMIN", "true").lower() == "true"

# Trusted localhost addresses
LOCALHOST_ADDRESSES = {"127.0.0.1", "localhost", "::1"}


class AccessLevel(StrEnum):
    """Access level for authenticated users."""

    ADMIN = "admin"  # Full access (postgres user or localhost)
    AGENT = "agent"  # Agent access (agent_{uuid} pattern)


class CallerType(StrEnum):
    """Type of caller accessing APIs - used for fine-grained ACL enforcement.

    While AccessLevel distinguishes admin vs agent at the auth layer,
    CallerType provides more granular distinctions for ACL decisions:
    - Different agent types have different permissions
    - ANONYMOUS represents unauthenticated callers (e.g., for /v2/ check)
    """

    ANONYMOUS = "anonymous"  # No auth - limited access (e.g., /v2/ check only)
    ADMIN = "admin"  # postgres user - full access
    PROMPT_OPTIMIZER = "prompt-optimizer"  # PO agent - can read/push
    PROMPT_IMPROVER = "prompt-improver"  # PI agent - can read/push
    CRITIC = "critic"  # Critic agent - limited access (LLM only)
    GRADER = "grader"  # Grader agent - limited access (LLM only)
    UNKNOWN = "unknown"  # Invalid/unrecognized caller


# ACL permission sets - which CallerTypes can perform each operation
ACL_CAN_READ_REGISTRY = {CallerType.ADMIN, CallerType.PROMPT_OPTIMIZER, CallerType.PROMPT_IMPROVER}
ACL_CAN_PUSH_REGISTRY = {CallerType.ADMIN, CallerType.PROMPT_OPTIMIZER, CallerType.PROMPT_IMPROVER}
ACL_CAN_PUSH_TAGS = {CallerType.ADMIN}  # Only admin can push by tag
ACL_CAN_USE_EVAL_API = {CallerType.ADMIN, CallerType.PROMPT_OPTIMIZER, CallerType.PROMPT_IMPROVER}


@dataclass(frozen=True)
class CredentialValidationResult:
    is_valid: bool
    access_level: AccessLevel | None = None
    agent_run_id: UUID | None = None
    error: str | None = None

    @classmethod
    def invalid(cls, error: str) -> CredentialValidationResult:
        return cls(is_valid=False, error=error)

    @classmethod
    def admin(cls) -> CredentialValidationResult:
        return cls(is_valid=True, access_level=AccessLevel.ADMIN)

    @classmethod
    def agent(cls, agent_run_id: UUID) -> CredentialValidationResult:
        return cls(is_valid=True, access_level=AccessLevel.AGENT, agent_run_id=agent_run_id)


def extract_agent_run_id_from_username(username: str) -> UUID | None:
    """Extract agent_run_id from username if it matches agent_{uuid} pattern.

    Uses the same pattern as TempUserManager.generate_username() which creates
    usernames in the format "agent_{uuid}".
    """
    prefix = "agent_"
    if not username.startswith(prefix):
        return None

    try:
        return UUID(username[len(prefix) :])
    except ValueError:
        logger.warning(f"Invalid UUID in agent username: {username}")
        return None


def validate_postgres_credentials(username: str, password: str) -> CredentialValidationResult:
    """Validate credentials by attempting Postgres connection."""
    # First, try to extract agent run ID from username pattern
    agent_run_id = extract_agent_run_id_from_username(username)

    # Get database config (uses PGHOST, PGPORT, PGDATABASE from env)
    try:
        db_config = get_database_config()
    except ValueError as e:
        logger.error(f"Database config not available: {e}")
        return CredentialValidationResult.invalid("Server configuration error")

    # Validate credentials against Postgres
    try:
        with psycopg.connect(
            host=db_config.host,
            port=db_config.port,
            dbname=db_config.database,
            user=username,
            password=password,
            connect_timeout=5,
        ):
            pass  # Connection succeeded
    except psycopg.OperationalError as e:
        logger.warning(f"Postgres auth failed for user {username}: {e}")
        return CredentialValidationResult.invalid("Invalid credentials")

    # Credentials valid - determine access level
    if agent_run_id is not None:
        return CredentialValidationResult.agent(agent_run_id)
    return CredentialValidationResult.admin()


def parse_basic_auth_header(authorization: str | None) -> tuple[str, str] | None:
    """Parse Basic auth header into (username, password). Returns None if invalid."""
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


def is_localhost_request(request: Request) -> bool:
    client = request.client
    return bool(client and client.host in LOCALHOST_ADDRESSES)


@dataclass
class AuthContext:
    """Authentication context attached to request.state.auth."""

    is_authenticated: bool = False
    is_admin: bool = False
    is_localhost_admin: bool = False
    username: str | None = None
    password: str | None = None
    agent_run_id: UUID | None = None
    error: str | None = None

    @classmethod
    def anonymous(cls) -> AuthContext:
        return cls(is_authenticated=False)

    @classmethod
    def localhost_admin(cls) -> AuthContext:
        return cls(is_authenticated=True, is_admin=True, is_localhost_admin=True)

    @classmethod
    def admin(cls, username: str, password: str) -> AuthContext:
        return cls(is_authenticated=True, is_admin=True, username=username, password=password)

    @classmethod
    def agent(cls, username: str, password: str, agent_run_id: UUID) -> AuthContext:
        return cls(
            is_authenticated=True, is_admin=False, username=username, password=password, agent_run_id=agent_run_id
        )

    @classmethod
    def failed(cls, error: str) -> AuthContext:
        return cls(is_authenticated=False, error=error)


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware that parses and validates auth, attaching context to request.state.

    After this middleware runs, request.state.auth will contain an AuthContext with
    one of: localhost_admin, anonymous, admin, agent, or failed. Routes check
    request.state.auth and decide how to handle each case. This middleware does
    NOT reject requests - it only parses and validates.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
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

                # Validate credentials and determine access level
                result = validate_postgres_credentials(username, password)
                if not result.is_valid:
                    logger.warning(f"Invalid postgres credentials for user: {username}")
                    request.state.auth = AuthContext.failed(result.error or "Invalid credentials")
                elif result.access_level == AccessLevel.AGENT:
                    assert result.agent_run_id is not None
                    request.state.auth = AuthContext.agent(username, password, result.agent_run_id)
                else:
                    request.state.auth = AuthContext.admin(username, password)

        return await call_next(request)


def get_auth_context(request: Request) -> AuthContext:
    return getattr(request.state, "auth", AuthContext.anonymous())


def get_caller_type(auth: AuthContext) -> tuple[CallerType, UUID | None]:
    """Determine caller type from auth context. Does DB lookup for agent users."""
    if auth.error:
        raise HTTPException(status_code=401, detail=auth.error)

    if not auth.is_authenticated:
        return CallerType.ANONYMOUS, None

    if auth.is_admin:
        return CallerType.ADMIN, None

    # For agents, look up run in database to determine type
    assert auth.agent_run_id is not None
    with get_session() as session:
        agent_run = session.get(AgentRun, auth.agent_run_id)
        if agent_run is None:
            raise HTTPException(status_code=401, detail="Invalid agent token")

        agent_type = agent_run.type_config.agent_type
        caller_type_map = {
            AgentType.PROMPT_OPTIMIZER: CallerType.PROMPT_OPTIMIZER,
            AgentType.IMPROVEMENT: CallerType.PROMPT_IMPROVER,
            AgentType.CRITIC: CallerType.CRITIC,
            AgentType.GRADER: CallerType.GRADER,
        }
        return caller_type_map.get(agent_type, CallerType.UNKNOWN), auth.agent_run_id


# =============================================================================
# Dependency functions for ACL enforcement
# =============================================================================


def require_registry_read(request: Request) -> tuple[CallerType, UUID | None]:
    """FastAPI dependency requiring registry read permission. Raises HTTPException 403 if not allowed."""
    auth = get_auth_context(request)
    caller_type, agent_run_id = get_caller_type(auth)
    if caller_type not in ACL_CAN_READ_REGISTRY:
        raise HTTPException(status_code=403, detail=f"{caller_type} not allowed to read from registry")
    return caller_type, agent_run_id


def require_registry_push(request: Request) -> tuple[CallerType, UUID | None]:
    """FastAPI dependency requiring registry push permission. Raises HTTPException 403 if not allowed."""
    auth = get_auth_context(request)
    caller_type, agent_run_id = get_caller_type(auth)
    if caller_type not in ACL_CAN_PUSH_REGISTRY:
        raise HTTPException(status_code=403, detail=f"{caller_type} not allowed to push to registry")
    return caller_type, agent_run_id


def require_eval_api_access(request: Request) -> tuple[CallerType, UUID | None]:
    """FastAPI dependency requiring eval API access. Raises HTTPException 403 if not allowed."""
    auth = get_auth_context(request)
    caller_type, agent_run_id = get_caller_type(auth)
    if caller_type not in ACL_CAN_USE_EVAL_API:
        raise HTTPException(status_code=403, detail=f"{caller_type} not allowed to access eval endpoints")
    return caller_type, agent_run_id


def require_admin_access(request: Request) -> None:
    """FastAPI dependency requiring admin access. Raises HTTPException 403 if not admin."""
    auth = get_auth_context(request)
    caller_type, _ = get_caller_type(auth)
    if caller_type != CallerType.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required")
