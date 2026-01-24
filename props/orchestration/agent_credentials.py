"""Agent PostgreSQL credentials.

Creates persistent database roles for agent runs with RLS-scoped access.
Passwords are deterministic (salt + agent_run_id) enabling reconnection.

All agents use the unified pattern:
- Username: agent_{agent_run_id}
- Role: agent_base (grants via migration 20251226000001)
- RLS: current_agent_run_id() extracts UUID, current_agent_type() determines access
- Password: deterministic from salt + agent_run_id

Roles are created on first use and never deleted. This avoids cleanup races
and allows agents to reconnect with the same credentials.

TODO: Consider adding a cleanup job to periodically remove stale agent roles
(e.g., roles for agent_runs older than 30 days).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from props.db.config import DbConnectionConfig

# Salt for deriving deterministic agent passwords.
# Set PROPS_AGENT_PASSWORD_SALT in production; uses default for development.
AGENT_PASSWORD_SALT = os.environ.get("PROPS_AGENT_PASSWORD_SALT", "dev-salt-change-in-production")

logger = logging.getLogger(__name__)


def _quote_ident(identifier: str) -> str:
    """Quote a PostgreSQL identifier for safe use in SQL.

    Raises:
        ValueError: If identifier contains characters outside [a-zA-Z0-9_-]
    """
    if not re.match(r"^[a-zA-Z0-9_-]+$", identifier):
        raise ValueError(
            f"Identifier contains invalid characters: {identifier!r}. "
            f"Only alphanumeric, underscore, and hyphen allowed."
        )
    escaped = identifier.replace('"', '""')
    return f'"{escaped}"'


@dataclass(frozen=True)
class AgentCredentials:
    """Credentials for an agent database role."""

    username: str
    password: str


def derive_agent_password(agent_run_id: UUID, salt: str = AGENT_PASSWORD_SALT) -> str:
    """Derive a deterministic password for an agent from salt and run ID.

    Uses HMAC-SHA256 for secure key derivation, then base64-encodes the result.
    The same agent_run_id always produces the same password (given the same salt),
    enabling agents to reconnect with consistent credentials.
    """
    key = salt.encode("utf-8")
    msg = str(agent_run_id).encode("utf-8")
    digest = hmac.new(key, msg, hashlib.sha256).digest()
    return urlsafe_b64encode(digest).decode("ascii").rstrip("=")


async def ensure_agent_role(admin_config: DbConnectionConfig, agent_run_id: UUID) -> AgentCredentials:
    """Ensure PostgreSQL role exists for agent, return credentials.

    Creates the role idempotently with deterministic password derived from
    salt + agent_run_id. Grants agent_base role for RLS-scoped access.

    Args:
        admin_config: Admin database connection (must have CREATE ROLE permission)
        agent_run_id: Agent run ID (encoded in username, used for password derivation)

    Returns:
        AgentCredentials with username and password for database connection
    """
    username = f"agent_{agent_run_id}"
    password = derive_agent_password(agent_run_id)

    logger.info("Ensuring agent role exists: %s", username)

    admin_url = admin_config.url().replace("postgresql://", "postgresql+asyncpg://")
    engine = create_async_engine(admin_url, echo=False)

    try:
        async with engine.begin() as conn:
            # Check if role exists
            result = await conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :username"), {"username": username}
            )
            role_exists = result.scalar() is not None

            if not role_exists:
                # Create role with password
                escaped_password = password.replace("'", "''")
                quoted_username = _quote_ident(username)
                await conn.execute(text(f"CREATE ROLE {quoted_username} WITH LOGIN PASSWORD '{escaped_password}'"))
                await conn.execute(text(f"GRANT agent_base TO {quoted_username}"))
                logger.info("Created agent role: %s", username)
            else:
                logger.debug("Agent role exists: %s", username)
    finally:
        await engine.dispose()

    return AgentCredentials(username=username, password=password)
