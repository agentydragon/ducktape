"""Configuration management for Gatelet server."""

import logging
import os
import re
import tomllib
from datetime import timedelta
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)


class LogLevel(StrEnum):
    """Logging level for server configuration."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class NoAuth(BaseModel):
    """No authentication configuration."""

    type: Literal["none"] = "none"


class BearerAuth(BaseModel):
    """Bearer token authentication configuration."""

    type: Literal["bearer"] = "bearer"
    token: str

    @field_validator("token")
    @classmethod
    def token_not_empty(cls, v):
        if not v:
            raise ValueError("Token must not be empty")
        return v


WebhookAuthConfig = NoAuth | BearerAuth


def _default_dsn() -> str:
    """Get default database DSN, respecting DATABASE_URL env var."""
    return os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/gatelet")


class DatabaseSettings(BaseModel):
    # Use a simple string so we can support both Postgres and SQLite for tests.
    # The DATABASE_URL environment variable overrides the value from the
    # configuration file. This makes it easy to point tests at a temporary
    # database.
    dsn: str = Field(default_factory=_default_dsn)


class ServerSettings(BaseModel):
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8000)
    log_level: LogLevel = Field(default=LogLevel.INFO)
    log_file: str = Field(default="gatelet.log")


class KeyInUrlAuthSettings(BaseModel):
    enabled: bool = Field(default=False)
    key_valid_days: int = Field(default=365)

    @property
    def key_validity(self) -> timedelta:
        """Get key validity period as timedelta."""
        return timedelta(days=self.key_valid_days)


class ChallengeResponseAuthSettings(BaseModel):
    enabled: bool = Field(default=False)
    num_options: int = Field(default=16)
    session_extension_seconds: int = Field(default=300)  # 5 minutes
    session_max_duration_seconds: int = Field(default=3600)  # 1 hour
    nonce_validity_seconds: int = Field(default=300)  # 5 minutes

    @property
    def session_extension(self) -> timedelta:
        """Get session extension period as timedelta."""
        return timedelta(seconds=self.session_extension_seconds)

    @property
    def session_max_duration(self) -> timedelta:
        """Get maximum session duration as timedelta."""
        return timedelta(seconds=self.session_max_duration_seconds)

    @property
    def nonce_validity(self) -> timedelta:
        """Get nonce validity period as timedelta."""
        return timedelta(seconds=self.nonce_validity_seconds)


class AuthSettings(BaseModel):
    key_in_url: KeyInUrlAuthSettings = Field(default=KeyInUrlAuthSettings())
    challenge_response: ChallengeResponseAuthSettings = Field(default=ChallengeResponseAuthSettings())


class HomeAssistantSettings(BaseModel):
    api_url: str = Field(...)  # Required field
    api_token: str = Field(...)  # Required field
    entities: list[str] = Field(default_factory=list)


class ActivityWatchSettings(BaseModel):
    """Configuration for ActivityWatch integration."""

    enabled: bool = Field(default=False)
    server_url: str = Field(default="http://127.0.0.1:5600")


class WebhookIntegrationSettings(BaseModel):
    auth_config: WebhookAuthConfig = Field()
    enabled: bool = Field(default=False)


MAX_INTEGRATION_NAME_LEN = 50
MAX_PAGE_SIZE = 100


class WebhookSettings(BaseModel):
    integrations: dict[str, WebhookIntegrationSettings] = Field(default_factory=dict)
    default_page_size: int = Field(default=10)

    @field_validator("integrations")
    @classmethod
    def validate_integration_names(cls, v):
        for name in v:
            # Check for URL-safe names
            if not re.match(r"^[a-zA-Z0-9_-]+$", name):
                raise ValueError(f"Integration name '{name}' not only letters, numbers, underscores, and hyphens.")
            if len(name) > MAX_INTEGRATION_NAME_LEN:
                raise ValueError(f"Integration name '{name}' too long (max {MAX_INTEGRATION_NAME_LEN} characters)")
        return v

    @field_validator("default_page_size")
    @classmethod
    def validate_page_size(cls, v):
        if v < 1 or v > MAX_PAGE_SIZE:
            raise ValueError(f"default_page_size must be between 1 and {MAX_PAGE_SIZE}")
        return v


class AdminSettings(BaseModel):
    """Configuration for the human admin account."""

    password_hash: str = Field(...)


class SecuritySettings(BaseModel):
    """Misc security configuration."""

    csrf_secret: str = Field(...)


class Settings(BaseModel):
    database: DatabaseSettings
    server: ServerSettings
    auth: AuthSettings
    home_assistant: HomeAssistantSettings
    activitywatch: ActivityWatchSettings = Field(default=ActivityWatchSettings())
    webhook: WebhookSettings
    admin: AdminSettings
    security: SecuritySettings

    @classmethod
    def from_file(cls, path: Path):
        """Load settings from file at path."""
        logger.info(f"Loading settings from {path.absolute()}")
        with path.open("rb") as f:
            config_dict = tomllib.load(f)
        return cls.model_validate(config_dict)


# Path to the active configuration file
CONFIG_PATH = Path(os.getenv("GATELET_CONFIG", "gatelet.toml"))


@lru_cache
def get_settings() -> Settings:
    """Get application settings (cached).

    This is the proper dependency injection pattern for FastAPI.
    Use as: settings: Settings = Depends(get_settings)

    The @lru_cache decorator ensures settings are loaded once and reused.
    For tests, call get_settings.cache_clear() to reset.
    """
    return Settings.from_file(CONFIG_PATH)
