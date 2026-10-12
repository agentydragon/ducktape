"""Migrate the `sandbox_service` database, which stores Session Event history.

Run from the history migration image as a History Service Deployment init container.
A migration failure prevents the Pod from serving even existing lifecycle RPCs.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict

from agentplane.history_service.storage.database_migrate import RUNNER


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTPLANE_HISTORY_SERVICE_")
    database_url: str


def main() -> None:
    RUNNER.apply(Settings().database_url)


if __name__ == "__main__":
    main()
