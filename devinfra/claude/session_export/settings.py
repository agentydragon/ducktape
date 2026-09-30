"""The sync's and the web UI's deployment contract: each field is an environment variable
`SESSION_SYNC_<FIELD>` (`util/settings_contract.py`)."""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# The defaults of `serve`'s settings and of the `sync` flags.
DEFAULT_SYNC_INTERVAL_SECONDS = 300
DEFAULT_SYNC_WORKERS = 3


class SyncSettings(BaseSettings):
    """The connection string is a secret, so it comes from the environment rather than the command line."""

    model_config = SettingsConfigDict(env_prefix="SESSION_SYNC_")

    database_url: str


class OIDCSettings(SyncSettings):
    """Settings shared by the public web replicas and local all-in-one page."""

    public_base_url: str = Field(description="The page's origin, as the browser and Authentik's redirect URI see it.")
    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: SecretStr
    oidc_session_secret: SecretStr
    oidc_allowed_subject: str = Field(description="The one Authentik `sub` allowed in, whatever else passes login.")
    oidc_session_seconds: int = Field(default=28_800, gt=0)
    host: str = "0.0.0.0"
    port: int = 8080


class WebSettings(OIDCSettings):
    """Public web/API replicas; control operations go to the single credential-owning pod."""

    control_base_url: str = Field(description="The private in-namespace URL of the credential-owning pod.")


class ControlSettings(SyncSettings):
    """The single private process that owns the rotating OAuth credential and sync loop."""

    credentials_file: Path = Field(description="Where the OAuth credential lives; this process owns it.")
    host: str = "0.0.0.0"
    port: int = 8080
    interval_seconds: float = Field(default=DEFAULT_SYNC_INTERVAL_SECONDS, gt=0, description="Between sync cycles.")
    workers: int = Field(default=DEFAULT_SYNC_WORKERS, gt=0, description="Sessions read concurrently.")
    live_streams: int = Field(
        default=20, ge=0, description="Sessions followed by event stream at once; 0 leaves the sync to its cycles."
    )
    live_window_seconds: float = Field(
        default=1800, gt=0, description="A session is followed live while its last event is this recent."
    )


class ServeSettings(OIDCSettings):
    """`serve`: local all-in-one mode; the cluster uses `web` replicas and one `control` process."""

    credentials_file: Path = Field(description="Where the OAuth credential lives; this process owns it.")
    interval_seconds: float = Field(default=DEFAULT_SYNC_INTERVAL_SECONDS, gt=0, description="Between sync cycles.")
    workers: int = Field(default=DEFAULT_SYNC_WORKERS, gt=0, description="Sessions read concurrently.")
    live_streams: int = Field(
        default=20, ge=0, description="Sessions followed by event stream at once; 0 leaves it to polling."
    )
    live_window_seconds: float = Field(
        default=1800, gt=0, description="A session is followed live while its last event is this recent."
    )
