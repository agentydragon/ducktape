"""Database configuration for production and test environments.

Database connection parameters are set by devenv.nix and must be present in the environment.
Tests construct their own DatabaseConfig with per-test database names.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class DbConnectionConfig:
    """Single-user database connection configuration.

    Contains all fields needed for a PostgreSQL connection. Use this directly
    when you need to pass connection details to a specific context (e.g., Docker container).
    """

    host: str
    port: int
    user: str
    password: str
    database: str

    def url(self) -> str:
        """Construct PostgreSQL connection URL."""
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.database}"

    def to_env_dict(self) -> dict[str, str]:
        """Convert connection config to PostgreSQL environment variables."""
        return {
            "PGHOST": self.host,
            "PGPORT": str(self.port),
            "PGDATABASE": self.database,
            "PGUSER": self.user,
            "PGPASSWORD": self.password,
        }

    def with_database(self, database: str) -> DbConnectionConfig:
        """Return a copy with different database name."""
        return DbConnectionConfig(
            host=self.host, port=self.port, user=self.user, password=self.password, database=database
        )

    def with_user(self, username: str, password: str) -> DbConnectionConfig:
        """Return a copy with different user credentials.

        For host-side access with temporary credentials:
            async with TempUserManager(config.admin, run_id) as creds:
                user_config = config.admin.with_user(creds.username, creds.password)

        For container access, use DatabaseConfig.for_container_user() instead.
        """
        return DbConnectionConfig(
            host=self.host, port=self.port, user=username, password=password, database=self.database
        )


@dataclass(frozen=True)
class DatabaseConfig:
    """Database configuration for both host-side and container contexts.

    Host-side (orchestrator spawning containers):
        - Needs container_name/container_port for Docker network routing
        - Gets config from get_production_config() (PG* env vars)

    Agent inside container:
        - Already in Docker network, connects directly via host:port
        - container_name/container_port should be None
        - Gets config from agent helpers (PROPS_DB_* env vars)
    """

    # Connection parameters
    host: str
    port: int
    database: str
    container_name: str | None  # Required for host-side container routing, None for agents
    container_port: int | None  # Required for host-side container routing, None for agents

    # Credentials (may be admin or scoped user depending on context)
    user: str
    password: str

    @property
    def admin(self) -> DbConnectionConfig:
        """Connection config with credentials from this config (host-side access)."""
        return DbConnectionConfig(
            host=self.host, port=self.port, user=self.user, password=self.password, database=self.database
        )

    def admin_url(self) -> str:
        """Construct connection URL (host-side access)."""
        return self.admin.url()

    def for_container_user(self, username: str, password: str) -> DbConnectionConfig:
        """Create container-accessible config with temporary user credentials.

        Combines container_name and container_port (for Docker network) with temporary user credentials.
        Use this for scoped agents (prompt optimizer, improvement, etc.).

        Example:
            async with TempUserManager(config.admin, run_id) as creds:
                container_config = config.for_container_user(creds.username, creds.password)
                env.update(container_config.to_env_dict())

        Raises:
            ValueError: If container_name or container_port is None
        """
        if self.container_name is None or self.container_port is None:
            raise ValueError(
                "container_name and container_port required for container routing. "
                "This config is for agent contexts (already in container)."
            )
        return DbConnectionConfig(
            host=self.container_name, port=self.container_port, user=username, password=password, database=self.database
        )

    def with_database(self, database: str) -> DatabaseConfig:
        """Create a new config with a different database name."""
        return DatabaseConfig(
            host=self.host,
            port=self.port,
            database=database,
            container_name=self.container_name,
            container_port=self.container_port,
            user=self.user,
            password=self.password,
        )


def _get_required_env(name: str) -> str:
    """Get required environment variable or raise."""
    value = os.environ.get(name)
    if not value:
        raise ValueError(
            f"{name} environment variable not set. Are you running from a devenv shell? Try: direnv allow && cd ."
        )
    return value


def _get_optional_env(name: str) -> str | None:
    """Get optional environment variable."""
    return os.environ.get(name) or None


def get_database_config() -> DatabaseConfig:
    """Get database configuration from environment variables.

    Environment variables (set by devenv.nix or passed to containers):
        Standard PostgreSQL client vars (required):
            PGHOST: Database host
            PGPORT: Database port
            PGUSER: Username (admin or scoped user)
            PGPASSWORD: Password
            PGDATABASE: Database name

        Project-specific (optional - for container routing):
            PROPS_DB_CONTAINER_NAME: Container name for Docker network access
            PROPS_DB_CONTAINER_PORT: Port inside container (for Docker network communication)

    Two usage contexts:
    1. Host-side (orchestrators spawning containers):
       - All vars set, including PROPS_DB_*
       - Used to create container configs with routing

    2. Agent inside container:
       - Only PG* vars set (PROPS_DB_* are None)
       - Direct connection, no routing needed

    Raises:
        ValueError: If required PG* env vars not set (run from devenv shell)
    """
    container_name = _get_optional_env("PROPS_DB_CONTAINER_NAME")
    container_port_str = _get_optional_env("PROPS_DB_CONTAINER_PORT")
    container_port = int(container_port_str) if container_port_str else None

    return DatabaseConfig(
        host=_get_required_env("PGHOST"),
        port=int(_get_required_env("PGPORT")),
        database=_get_required_env("PGDATABASE"),
        container_name=container_name,
        container_port=container_port,
        user=_get_required_env("PGUSER"),
        password=_get_required_env("PGPASSWORD"),
    )
