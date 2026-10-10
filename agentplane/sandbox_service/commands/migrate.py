"""Migrate the Sandbox Service's command admission database.

Run from the commands migration image as a Sandbox Service Deployment init container.
A migration failure prevents the Pod from serving even existing lifecycle RPCs.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict

from agentplane.sandbox_service.commands.database_migrate import RUNNER


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTPLANE_SANDBOX_SERVICE_")
    commands_database_url: str


def main() -> None:
    RUNNER.apply(Settings().commands_database_url)


if __name__ == "__main__":
    main()
