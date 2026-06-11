"""Shared authentication utilities and FastAPI dependencies for backend APIs.

Authentication uses Bearer or Basic tokens with Postgres credentials validation:
- Admin users: Any valid Postgres user (non-agent_* username, not 'evaluator')
- Evaluator users: Username 'evaluator' — read-only DB access, can launch agents
- Agent users: Format agent_{uuid} with temp credentials

Tokens contain base64-encoded username:password. Both schemes are supported:
- Bearer: OpenAI SDK sends api_key as Bearer token; agent containers encode creds this way
- Basic: OCI/crane tooling doesn't support token auth (Bearer); crane/docker
  send Basic auth from Docker config

This module provides:
- Credential validation with access level determination
- Request identity resolution with DB lookup for agent types
- FastAPI dependency for per-request auth (get_request_identity)
- Dependency functions for ACL enforcement (require_admin_access, etc.)
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


# --- Role markers (the varying part of each identity) ---


@dataclass(frozen=True)
class AdminRole:
    """Full admin access."""


@dataclass(frozen=True)
class EvaluatorRole:
    """Read-only DB access (BYPASSRLS + SELECT-only), can launch any agent type."""


@dataclass(frozen=True)
class AgentRole:
    """Agent run with specific type."""

    agent_type: AgentType
    agent_run_id: UUID


Role = AdminRole | EvaluatorRole | AgentRole


# --- Request identity ---


@dataclass(frozen=True)
class AnonymousIdentity:
    """Unauthenticated request."""


@dataclass(frozen=True)
class AuthenticatedIdentity:
    """Authenticated request with Postgres credentials and a role."""

    username: str
    password: str
    role: Role


RequestIdentity = AnonymousIdentity | AuthenticatedIdentity


_CRITIC_DEV_TYPES = {AgentType.CRITIC_DEV_OPTIMIZE, AgentType.CRITIC_DEV_IMPROVE}


def is_admin_or_evaluator(identity: RequestIdentity) -> bool:
    """Admin or evaluator — full read access, can launch any agent type."""
    return isinstance(identity, AuthenticatedIdentity) and isinstance(identity.role, (AdminRole, EvaluatorRole))


def is_critic_dev_agent(identity: RequestIdentity) -> bool:
    """Critic_dev agent (optimize or improve) — can push/pull registry and run critics."""
    return (
        isinstance(identity, AuthenticatedIdentity)
        and isinstance(identity.role, AgentRole)
        and identity.role.agent_type in _CRITIC_DEV_TYPES
    )


def extract_agent_run_id_from_username(username: str) -> UUID | None:
    """Extract agent_run_id from username if it matches agent_{uuid} pattern."""
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

    Returns agent_run_id if agent (extracted from username), None if non-agent.
    Raises HTTPException 401 if credentials invalid.
    """
    agent_run_id = extract_agent_run_id_from_username(username)

    try:
        with psycopg.connect(
            host=db_config.host,
            port=db_config.port,
            dbname=db_config.database,
            user=username,
            password=password,
            connect_timeout=5,
        ):
            pass
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

    if agent_run_id is None:
        # Non-agent: admin or evaluator
        role: Role = EvaluatorRole() if username == "evaluator" else AdminRole()
        return AuthenticatedIdentity(username=username, password=password, role=role)

    # Agent: look up agent_type from database
    with admin_db.session() as session:
        agent_run = session.get(AgentRun, agent_run_id)
        if agent_run is None:
            raise HTTPException(status_code=401, detail="Invalid agent token")

        return AuthenticatedIdentity(
            username=username,
            password=password,
            role=AgentRole(agent_type=agent_run.type_config.agent_type, agent_run_id=agent_run_id),
        )


# Type alias for request identity dependency (use in FastAPI route signatures)
Auth = Annotated[RequestIdentity, Depends(get_request_identity)]


# =============================================================================
# Dependency functions for ACL enforcement
# =============================================================================


def require_critic_run_access(auth: Auth) -> RequestIdentity:
    """FastAPI dependency requiring critic run access. Raises HTTPException 403 if not allowed."""
    if not (is_admin_or_evaluator(auth) or is_critic_dev_agent(auth)):
        raise HTTPException(status_code=403, detail=f"{auth} not allowed to run critics")
    return auth


def require_admin_access(auth: Auth) -> None:
    """FastAPI dependency requiring admin access. Raises HTTPException 403 if not admin."""
    if not (isinstance(auth, AuthenticatedIdentity) and isinstance(auth.role, AdminRole)):
        raise HTTPException(status_code=403, detail="Admin access required")


def require_evaluator_or_admin_access(auth: Auth) -> None:
    """FastAPI dependency allowing admin or evaluator access."""
    if not (isinstance(auth, AuthenticatedIdentity) and isinstance(auth.role, (AdminRole, EvaluatorRole))):
        raise HTTPException(status_code=403, detail="Admin or evaluator access required")


# =============================================================================
# Credential passthrough for DB access
# =============================================================================


def get_caller_db(admin_db: AdminDb, auth: Auth) -> Iterator[Database]:
    """Get Database using caller's credentials for RLS/permission enforcement.

    Admin: returns the shared admin Database (full access, bypasses RLS).
    Evaluator: per-request Database with evaluator's Postgres role (BYPASSRLS + SELECT-only).
    Agent: per-request Database with agent's Postgres role (RLS-scoped access).
    Anonymous: raises 401.
    """
    if isinstance(auth, AnonymousIdentity):
        raise HTTPException(status_code=401, detail="Authentication required")

    assert isinstance(auth, AuthenticatedIdentity)

    if isinstance(auth.role, AdminRole):
        yield admin_db
        return

    # Evaluator or agent: per-request DB with their own credentials
    user_config = admin_db.config.with_user(auth.username, auth.password)
    user_db = Database.per_request(user_config)
    try:
        yield user_db
    finally:
        user_db.dispose()


# Type alias for caller-scoped database dependency (use in route signatures where RLS applies)
CallerDb = Annotated[Database, Depends(get_caller_db)]
