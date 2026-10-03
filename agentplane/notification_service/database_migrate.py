"""Image-owned, advisory-locked Alembic migrations for the notification database."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from agentplane.notification_service.db import Base
from util.db_migrations import MigrationRunner

RUNNER = MigrationRunner(
    metadata=Base.metadata, migrations_dir=Path(__file__).parent / "migrations", lock_key=0x4E4F5446
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTPLANE_NOTIFICATIONS_")
    database_url: str


def main() -> None:
    RUNNER.apply(Settings().database_url)


if __name__ == "__main__":
    main()
