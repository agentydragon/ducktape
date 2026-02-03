"""Database configuration for production and test environments.

Database connection parameters are set by devenv.nix and must be present in the environment.
Tests construct their own DatabaseConfig with per-test database names.
"""

from __future__ import annotations

import asyncpg
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseConfig(BaseSettings):
    """Database connection configuration.

    Contains all fields needed for a PostgreSQL connection. Used by both
    host-side orchestrators and agents inside containers.

    Reads from PG* environment variables automatically via pydantic-settings.
    """

    model_config = SettingsConfigDict(env_prefix="PG", frozen=True, populate_by_name=True)

    host: str = Field(alias="PGHOST")
    port: int = Field(alias="PGPORT")
    database: str = Field(alias="PGDATABASE")
    user: str = Field(alias="PGUSER")
    password: str = Field(alias="PGPASSWORD")

    @property
    def url(self) -> str:
        """PostgreSQL connection URL."""
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

    def with_database(self, database: str) -> DatabaseConfig:
        """Return a copy with different database name."""
        return DatabaseConfig(host=self.host, port=self.port, user=self.user, password=self.password, database=database)

    def with_user(self, username: str, password: str) -> DatabaseConfig:
        """Return a copy with different user credentials."""
        return DatabaseConfig(host=self.host, port=self.port, user=username, password=password, database=self.database)

    async def asyncpg_connect(self) -> asyncpg.Connection[asyncpg.Record]:
        """Connect to database using asyncpg."""
        return await asyncpg.connect(self.url)
