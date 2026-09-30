"""The sync's and the web UI's deployment contract: each field is an environment variable
`SESSION_SYNC_<FIELD>` (`util/settings_contract.py`)."""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class SyncSettings(BaseSettings):
    """The connection string is a secret, so it comes from the environment rather than the command line."""

    model_config = SettingsConfigDict(env_prefix="SESSION_SYNC_")

    database_url: str


class ServeSettings(SyncSettings):
    """`serve`: the sync plus the page that pairs it, behind Authentik login for one owner."""

    credentials_file: Path = Field(description="Where the OAuth credential lives; this process owns it.")
    public_base_url: str = Field(description="The page's origin, as the browser and Authentik's redirect URI see it.")
    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: SecretStr
    oidc_session_secret: SecretStr
    oidc_allowed_subject: str = Field(description="The one Authentik `sub` allowed in, whatever else passes login.")
    oidc_session_seconds: int = Field(default=28_800, gt=0)
    host: str = "0.0.0.0"
    port: int = 8080
    interval_seconds: float = Field(default=300, gt=0, description="Between sync cycles.")
    workers: int = Field(default=3, gt=0, description="Sessions read concurrently.")
