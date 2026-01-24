"""Agent PostgreSQL role management.

Creates persistent database roles for agent runs with RLS-scoped access.
All agent types use the same pattern - type-specific access is controlled
by RLS policies based on agent_runs.type_config.

All agents use the unified pattern:
- Username: agent_{agent_run_id}
- Role: agent_base (grants via migration 20251226000001)
- RLS: current_agent_run_id() extracts UUID, current_agent_type() determines access
- Password: deterministic from salt + agent_run_id (enables reconnection)

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
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from props.db.config import DbConnectionConfig

# Salt for deriving deterministic agent passwords.
# Set PROPS_AGENT_PASSWORD_SALT in production; uses default for development.
AGENT_PASSWORD_SALT = os.environ.get("PROPS_AGENT_PASSWORD_SALT", "dev-salt-change-in-production")

logger = logging.getLogger(__name__)


def quote_ident(identifier: str) -> str:
    """Quote a PostgreSQL identifier for safe use in SQL.

    Args:
        identifier: The identifier to quote (username, table name, etc.)

    Returns:
        Quoted identifier safe for SQL injection

    Raises:
        ValueError: If identifier contains characters outside [a-zA-Z0-9_-]
    """
    if not re.match(r"^[a-zA-Z0-9_-]+$", identifier):
        raise ValueError(
            f"Identifier contains invalid characters: {identifier!r}. "
            f"Only alphanumeric, underscore, and hyphen allowed."
        )
    # Escape any existing double quotes by doubling them
    escaped = identifier.replace('"', '""')
    return f'"{escaped}"'


@dataclass(frozen=True)
class TempUserCredentials:
    """Credentials for a temporary database user.

    Contains only the credentials (username, password) created by the manager.
    Callers combine these with their own connection parameters (host, port, database)
    based on their context (e.g., Docker containers use different host than admin).
    """

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


class TempUserManager:
    """Async context manager for PostgreSQL agent roles.

    Creates persistent database roles for agent runs with RLS-scoped access.
    All agent types use the same pattern - type-specific access is controlled
    by RLS policies based on agent_runs.type_config.

    Lifecycle:
    1. Generate username from agent_run_id (agent_{uuid} pattern)
    2. Derive deterministic password from salt + agent_run_id
    3. Create PostgreSQL role if it doesn't exist (idempotent)
    4. Grant agent_base role (provides common permissions)
    5. Yield credentials (username, password)
    6. On exit: no cleanup (roles persist for reconnection)

    Roles are never deleted - this allows agents to reconnect and avoids
    cleanup races. Stale roles can be cleaned up by a separate job.

    Usage:
        async with TempUserManager(admin_config, agent_run_id) as creds:
            # Combine credentials with your connection parameters
            config = admin_config.with_user(creds.username, creds.password)
            engine = create_engine(config.url())
            # Agent has RLS-scoped access based on agent_run_id and type_config
        # Role persists after exit (no cleanup)
    """

    def __init__(self, admin_config: DbConnectionConfig, agent_run_id: UUID):
        """Initialize with admin database config and agent run ID.

        Args:
            admin_config: Admin database connection (must have CREATE ROLE permission)
            agent_run_id: Agent run ID to scope access to (encoded in username)
        """
        self.admin_config = admin_config
        self.agent_run_id = agent_run_id
        self.admin_engine: AsyncEngine | None = None
        self._username: str | None = None
        self._password: str | None = None

    def generate_username(self) -> str:
        """Generate username encoding the agent run ID.

        Uses the unified agent_{uuid} pattern recognized by current_agent_run_id().

        Returns:
            Username for the temporary role (e.g., "agent_12345678-1234-...")
        """
        return f"agent_{self.agent_run_id}"

    async def grant_permissions(self, username: str) -> None:
        """Grant agent_base role which provides RLS-scoped access to agent tables."""
        assert self.admin_engine is not None, "admin_engine not initialized"
        async with self.admin_engine.begin() as conn:
            quoted_username = quote_ident(username)
            await conn.execute(text(f"GRANT agent_base TO {quoted_username}"))

        logger.debug(f"Granted agent_base to {username}")

    async def __aenter__(self) -> TempUserCredentials:
        """Create role (if needed) and grant permissions, return credentials."""
        self._username = self.generate_username()
        self._password = derive_agent_password(self.agent_run_id)

        logger.info(f"Ensuring agent role exists: {self._username}")

        # Create admin engine
        admin_url = self.admin_config.url().replace("postgresql://", "postgresql+asyncpg://")
        self.admin_engine = create_async_engine(admin_url, echo=False)

        # Create user
        await self._create_user(self._username, self._password)

        # Grant permissions (subclass-specific)
        await self.grant_permissions(self._username)

        logger.info(f"Agent role {self._username} ready")

        # Return credentials only (caller combines with their connection parameters)
        return TempUserCredentials(username=self._username, password=self._password)

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Dispose admin engine. Roles persist for agent reconnection."""
        if self.admin_engine is not None:
            await self.admin_engine.dispose()

    async def _create_user(self, username: str, password: str) -> None:
        """Create PostgreSQL role with LOGIN privilege (idempotent).

        Args:
            username: Role name to create
            password: Secure password for the role
        """
        assert self.admin_engine is not None, "admin_engine not initialized"
        async with self.admin_engine.begin() as conn:
            # Check if role exists first
            result = await conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :username"), {"username": username}
            )
            role_exists = result.scalar() is not None

            if not role_exists:
                # Create role with password (escape single quotes)
                escaped_password = password.replace("'", "''")
                quoted_username = quote_ident(username)
                await conn.execute(text(f"CREATE ROLE {quoted_username} WITH LOGIN PASSWORD '{escaped_password}'"))
                logger.debug(f"Created role: {username}")
            else:
                logger.debug(f"Role {username} already exists")
