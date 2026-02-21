"""Shared authentication utilities and FastAPI dependencies for backend APIs.

Authentication uses Bearer or Basic tokens with Postgres credentials validation:
- Admin users: Any valid Postgres user (non-agent_* username)
- Agent users: Format agent_{uuid} with temp credentials

Tokens contain base64-encoded username:password. Both schemes are supported:
- Bearer: OpenAI SDK sends api_key as Bearer token; agent containers encode creds this way
- Basic: OCI/crane tooling doesn't support token auth (Bearer); crane/docker
  send Basic auth from Docker config

This module provides:
- Credential validation with access level determination
- Caller type determination for ACL enforcement
- FastAPI dependency for per-request auth (get_auth_context)
- Dependency functions for ACL enforcement (require_admin_access, require_critic_run_access)
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated
from uuid import UUID

import psycopg
from fastapi import Depends, HTTPException, Request

from props.backend.deps import AdminDb
from props.db.config import DatabaseConfig
from props.db.database import Database
from props.db.models import AgentRun, AgentType

logger = logging.getLogger(__name__)


class AccessLevel(StrEnum):
    """Access level for authenticated users."""

    ADMIN = "admin"  # Full access (postgres user)
    AGENT = "agent"  # Agent access (agent_{uuid} pattern)
    EVALUATOR = "evaluator"  # Evaluator access (evaluator role)


# --- Request identity: discriminated union ---


@dataclass(frozen=True)
class AnonymousIdentity:
    """Unauthenticated request."""


@dataclass(frozen=True)
class AdminIdentity:
    """Admin user with full access."""


@dataclass(frozen=True)
class AgentIdentity:
    """Agent with specific type and run ID."""

    agent_type: AgentType
    agent_run_id: UUID


@dataclass(frozen=True)
class EvaluatorIdentity:
    """Evaluator with read and run permissions."""


RequestIdentity = AnonymousIdentity | AdminIdentity | AgentIdentity | EvaluatorIdentity


def can_run_agent_type(identity: RequestIdentity, allowed_types: set[AgentType]) -> bool:
    """Check if identity's agent type is in the allowed set. Admin always allowed."""
    match identity:
        case AdminIdentity():
            return True
        case AgentIdentity(agent_type=agent_type):
            return agent_type in allowed_types
        case _:
            return False


def can_access_level(identity: RequestIdentity, allowed_levels: set[AccessLevel]) -> bool:
    """Check if identity has one of the allowed access levels."""
    match identity:
        case AdminIdentity():
            return AccessLevel.ADMIN in allowed_levels
        case EvaluatorIdentity():
            return AccessLevel.EVALUATOR in allowed_levels
        case _:
            return False


# ACL permission sets — agent types that can perform each operation (admin always allowed)
_CRITIC_DEV_TYPES = {AgentType.CRITIC_DEV_OPTIMIZE, AgentType.CRITIC_DEV_IMPROVE}
ACL_CAN_READ_REGISTRY: set[AgentType] = _CRITIC_DEV_TYPES
ACL_CAN_PUSH_REGISTRY: set[AgentType] = _CRITIC_DEV_TYPES
ACL_CAN_PUSH_TAGS: set[AgentType] = set()  # Admin only
ACL_CAN_RUN_CRITICS: set[AgentType] = _CRITIC_DEV_TYPES

# Caller types allowed to perform each operation (evaluated with has_access_caller)
ACL_CAN_RUN_VALIDATION_AGENTS: set[AccessLevel] = {AccessLevel.ADMIN, AccessLevel.EVALUATOR}


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

    @classmethod
    def evaluator(cls) -> CredentialValidationResult:
        return cls(is_valid=True, access_level=AccessLevel.EVALUATOR)


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


def validate_postgres_credentials(
    username: str, password: str, db_config: DatabaseConfig
) -> CredentialValidationResult:
    """Validate credentials by attempting Postgres connection."""
    # First, try to extract agent run ID from username pattern
    agent_run_id = extract_agent_run_id_from_username(username)
    is_evaluator = username == "evaluator"

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
    if is_evaluator:
        return CredentialValidationResult.evaluator()
    return CredentialValidationResult.admin()


def parse_credentials(authorization: str | None) -> tuple[str, str] | None:
    """Parse Bearer or Basic token containing base64-encoded username:password.

    Both schemes carry the same payload (base64 of username:password):
    - Bearer: used by OpenAI SDK and agent containers
    - Basic: OCI/crane tooling does not support token auth (Bearer), so we
      accept Basic for Docker/crane clients (crane push/pull, docker push/pull)
    """
    if not authorization:
        return None
    if authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ")
    elif authorization.startswith("Basic "):
        token = authorization.removeprefix("Basic ")
    else:
        return None

    try:
        decoded = base64.b64decode(token).decode("utf-8")
        if ":" not in decoded:
            return None
        username, password = decoded.split(":", 1)
        return (username, password)
    except (ValueError, UnicodeDecodeError):
        return None


def get_request_identity(request: Request, admin_db: AdminDb) -> RequestIdentity:
    """FastAPI dependency that parses Authorization header, validates credentials, and returns request identity.

    Raises HTTPException 401 for malformed or invalid credentials.
    Returns AnonymousIdentity for unauthenticated requests (downstream ACL decides access).
    For agents, looks up agent_type from database.
    FastAPI caches the result per-request.
    """
    authorization = request.headers.get("authorization")

    if not authorization:
        return AnonymousIdentity()

    parsed = parse_credentials(authorization)
    if not parsed:
        raise HTTPException(status_code=401, detail="Invalid authorization format")

    username, password = parsed
    result = validate_postgres_credentials(username, password, admin_db.config)
    if not result.is_valid:
        logger.warning(f"Invalid postgres credentials for user: {username}")
        raise HTTPException(status_code=401, detail=result.error or "Invalid credentials")

    if result.access_level == AccessLevel.ADMIN:
        return AdminIdentity()

    if result.access_level == AccessLevel.EVALUATOR:
        return EvaluatorIdentity()

    # For agents: look up agent_type from database
    assert result.agent_run_id is not None
    with admin_db.session() as session:
        agent_run = session.get(AgentRun, result.agent_run_id)
        if agent_run is None:
            raise HTTPException(status_code=401, detail="Invalid agent token")
        return AgentIdentity(agent_type=agent_run.type_config.agent_type, agent_run_id=result.agent_run_id)


# Type alias for request identity dependency (use in FastAPI route signatures)
Auth = Annotated[RequestIdentity, Depends(get_request_identity)]


# =============================================================================
# Dependency functions for ACL enforcement
# =============================================================================


def require_critic_run_access(auth: Auth) -> RequestIdentity:
    """FastAPI dependency requiring critic run access. Raises HTTPException 403 if not allowed."""
    if not can_run_agent_type(auth, ACL_CAN_RUN_CRITICS):
        raise HTTPException(status_code=403, detail=f"{auth} not allowed to run critics")
    return auth


def require_admin_access(auth: Auth) -> None:
    """FastAPI dependency requiring admin access. Raises HTTPException 403 if not admin."""
    if not isinstance(auth, AdminIdentity):
        raise HTTPException(status_code=403, detail="Admin access required")


def require_evaluator_or_admin_access(auth: Auth) -> RequestIdentity:
    """FastAPI dependency requiring evaluator or admin access.

    Allows both admin and evaluator identities to run validation/optimization/improvement runs.
    Raises HTTPException 403 if neither.
    """
    if not isinstance(auth, (AdminIdentity, EvaluatorIdentity)):
        raise HTTPException(status_code=403, detail="Evaluator or admin access required")
    return auth


# =============================================================================
# Agent credential passthrough
# =============================================================================


def get_agent_db(request: Request, admin_db: AdminDb, auth: Auth) -> Iterator[Database]:
    """Get Database using agent credentials for RLS enforcement.

    For agent callers: Creates per-request Database with agent's Postgres
    credentials. RLS policies automatically enforce access control.
    For admin callers: Returns the shared admin Database instance.
    For anonymous: Raises 401.

    Yields the Database and disposes per-request agent connections on cleanup.
    """
    if isinstance(auth, AnonymousIdentity):
        raise HTTPException(status_code=401, detail="Authentication required")

    if isinstance(auth, AdminIdentity):
        yield admin_db
        return

    # Agent caller: extract credentials from request to create per-request database
    assert isinstance(auth, AgentIdentity)
    authorization = request.headers.get("authorization")
    if not authorization:
        raise HTTPException(status_code=500, detail="Agent auth missing credentials")

    parsed = parse_credentials(authorization)
    if not parsed:
        raise HTTPException(status_code=500, detail="Agent auth missing credentials")

    username, password = parsed
    agent_config = admin_db.config.with_user(username, password)
    agent_db = Database.per_request(agent_config)
    try:
        yield agent_db
    finally:
        agent_db.dispose()


# Type alias for agent database dependency (use in route signatures where RLS applies)
AgentDb = Annotated[Database, Depends(get_agent_db)]
