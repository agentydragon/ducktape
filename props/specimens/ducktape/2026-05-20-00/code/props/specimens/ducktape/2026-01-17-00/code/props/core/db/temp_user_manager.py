"""Temporary PostgreSQL user management with async context manager pattern.

Provides lifecycle management (create, yield credentials, cleanup) for ephemeral database users
used by all agent types (critic, grader, prompt optimizer, etc.).

All agents use the unified pattern:
- Username: agent_{agent_run_id}
- Role: agent_base (grants via migration 20251226000001)
- RLS: current_agent_run_id() extracts UUID, current_agent_type() determines access

Type-specific access is controlled entirely by RLS policies based on agent_runs.type_config,
not by different roles or username patterns.
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from props.core.db.config import DbConnectionConfig

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


class TempUserManager:
    """Async context manager for temporary PostgreSQL agent users.

    Creates ephemeral database users for agent runs with RLS-scoped access.
    All agent types use the same pattern - type-specific access is controlled
    by RLS policies based on agent_runs.type_config.

    Lifecycle:
    1. Generate username from agent_run_id (agent_{uuid} pattern)
    2. Create PostgreSQL role with secure password
    3. Grant agent_base role (provides common permissions)
    4. Yield credentials (username, password)
    5. Revoke permissions and terminate connections
    6. Drop role

    Usage:
        async with TempUserManager(admin_config, agent_run_id) as creds:
            # Combine credentials with your connection parameters
            config = admin_config.with_user(creds.username, creds.password)
            engine = create_engine(config.url())
            # Agent has RLS-scoped access based on agent_run_id and type_config
        # User automatically cleaned up on exit
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

    async def revoke_permissions(self, username: str) -> None:
        """No-op: DROP ROLE automatically removes role memberships and inherited privileges."""

    async def __aenter__(self) -> TempUserCredentials:
        """Create user and grant permissions, return credentials."""
        self._username = self.generate_username()
        self._password = secrets.token_urlsafe(32)

        logger.info(f"Creating temporary user: {self._username}")

        # Create admin engine
        admin_url = self.admin_config.url().replace("postgresql://", "postgresql+asyncpg://")
        self.admin_engine = create_async_engine(admin_url, echo=False)

        # Create user
        await self._create_user(self._username, self._password)

        # Grant permissions (subclass-specific)
        await self.grant_permissions(self._username)

        logger.info(f"Temporary user {self._username} ready")

        # Return credentials only (caller combines with their connection parameters)
        return TempUserCredentials(username=self._username, password=self._password)

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Revoke permissions, terminate connections, drop user."""
        if self.admin_engine is None or self._username is None:
            return

        try:
            await self.revoke_permissions(self._username)
            await self._terminate_connections(self._username)
            await self._drop_user(self._username)
            logger.info(f"Temporary user {self._username} cleaned up")
        except Exception as e:
            logger.error(f"Failed to cleanup user {self._username}: {e}", exc_info=True)
        finally:
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

    async def _terminate_connections(self, username: str) -> None:
        """Terminate all active connections for the user.

        Required before dropping the role.

        Args:
            username: Role name to terminate connections for
        """
        assert self.admin_engine is not None, "admin_engine not initialized"
        async with self.admin_engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    SELECT pg_terminate_backend(pid)
                    FROM pg_stat_activity
                    WHERE usename = :username
                      AND pid != pg_backend_pid()
                """
                ),
                {"username": username},
            )

        logger.debug(f"Terminated connections for {username}")

    async def _drop_user(self, username: str) -> None:
        """Drop PostgreSQL role.

        Args:
            username: Role name to drop
        """
        assert self.admin_engine is not None, "admin_engine not initialized"
        async with self.admin_engine.begin() as conn:
            quoted_username = quote_ident(username)
            await conn.execute(text(f"DROP ROLE IF EXISTS {quoted_username}"))

        logger.debug(f"Dropped user: {username}")
