"""Migrate the Runner Gateway's command admission database.

Run from the commands migration image as a Runner Gateway Deployment init container.
A migration failure prevents the Pod from serving even existing lifecycle RPCs.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict

from agentplane.runner_gateway.commands.database_migrate import RUNNER


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTPLANE_SANDBOX_SERVICE_")
    commands_database_url: str


def main() -> None:
    RUNNER.apply(Settings().commands_database_url)


if __name__ == "__main__":
    main()
