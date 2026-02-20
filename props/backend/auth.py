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
- Request identity resolution with DB lookup for agent types
- FastAPI dependency for per-request auth (get_request_identity)
- Dependency functions for ACL enforcement (require_admin_access, require_critic_run_access)
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

import psycopg
from fastapi import Depends, HTTPException, Request

from props.backend.deps import AdminDb
from props.db.config import DatabaseConfig
from props.db.database import Database
from props.db.models import AgentRun, AgentType

logger = logging.getLogger(__name__)


# --- Request identity: discriminated union ---


@dataclass(frozen=True)
class AnonymousIdentity:
    """Unauthenticated request."""


@dataclass(frozen=True)
class AdminIdentity:
    """Admin user with full access."""

    username: str
    password: str


@dataclass(frozen=True)
class AgentIdentity:
    """Agent with specific type and run ID."""

    agent_type: AgentType
    agent_run_id: UUID
    username: str
    password: str


RequestIdentity = AnonymousIdentity | AdminIdentity | AgentIdentity


def can_run_agent_type(identity: RequestIdentity, allowed_types: set[AgentType]) -> bool:
    """Check if identity's agent type is in the allowed set. Admin always allowed."""
    if isinstance(identity, AdminIdentity):
        return True
    return isinstance(identity, AgentIdentity) and identity.agent_type in allowed_types


# ACL permission sets — agent types that can perform each operation (admin always allowed)
_CRITIC_DEV_TYPES = {AgentType.CRITIC_DEV_OPTIMIZE, AgentType.CRITIC_DEV_IMPROVE}
ACL_CAN_READ_REGISTRY: set[AgentType] = _CRITIC_DEV_TYPES
ACL_CAN_PUSH_REGISTRY: set[AgentType] = _CRITIC_DEV_TYPES
ACL_CAN_PUSH_TAGS: set[AgentType] = set()  # Admin only
ACL_CAN_RUN_CRITICS: set[AgentType] = _CRITIC_DEV_TYPES


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


def validate_postgres_credentials(username: str, password: str, db_config: DatabaseConfig) -> UUID | None:
    """Validate credentials against Postgres.

    Returns agent_run_id if agent (extracted from username), None if admin.
    Raises HTTPException 401 if credentials invalid.
    """
    # Extract agent run ID from username pattern (None means admin)
    agent_run_id = extract_agent_run_id_from_username(username)

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
        raise HTTPException(status_code=401, detail="Invalid credentials")

    return agent_run_id


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
    agent_run_id = validate_postgres_credentials(username, password, admin_db.config)

    # Admin: validate_postgres_credentials returned None
    if agent_run_id is None:
        return AdminIdentity(username=username, password=password)

    # Agent: look up agent_type from database
    with admin_db.session() as session:
        agent_run = session.get(AgentRun, agent_run_id)
        if agent_run is None:
            raise HTTPException(status_code=401, detail="Invalid agent token")

        return AgentIdentity(
            agent_type=agent_run.type_config.agent_type, agent_run_id=agent_run_id, username=username, password=password
        )


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


# =============================================================================
# Agent credential passthrough
# =============================================================================


def get_agent_db(admin_db: AdminDb, auth: Auth) -> Iterator[Database]:
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

    # Must be AgentIdentity
    assert isinstance(auth, AgentIdentity)
    agent_config = admin_db.config.with_user(auth.username, auth.password)
    agent_db = Database.per_request(agent_config)
    try:
        yield agent_db
    finally:
        agent_db.dispose()


# Type alias for agent database dependency (use in route signatures where RLS applies)
AgentDb = Annotated[Database, Depends(get_agent_db)]
