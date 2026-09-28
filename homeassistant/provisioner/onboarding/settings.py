"""Configuration of the Home Assistant onboarding provisioner."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import SettingsConfigDict

from homeassistant.provisioner.endpoint import HomeAssistantEndpoint
from homeassistant.provisioner.yaml_settings import YamlFileSettings


class HttpConfig(BaseModel):
    """Home Assistant HTTP settings applied through its admin API."""

    server_host: list[str]
    server_port: int
    cors_allowed_origins: list[str]
    use_x_forwarded_for: bool
    trusted_proxies: list[str]
    login_attempts_threshold: int
    ip_ban_enabled: bool
    ssl_profile: str
    use_x_frame_options: bool


class HomeLocation(BaseModel):
    """The home zone's coordinates. They come from a Secret, never from this public repository."""

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class CoreConfig(BaseModel):
    """Home Assistant core settings applied through its admin API. Only these; the rest stay as set in
    the UI. A freshly onboarded Home Assistant otherwise comes up in UTC at 0,0."""

    time_zone: str = Field(min_length=1, description="An IANA time zone name.")
    location: HomeLocation | None = Field(
        default=None, description="Absent while its Secret does not exist; the location then stays as set in the UI."
    )


class Settings(YamlFileSettings):
    model_config = SettingsConfigDict(env_prefix="HOME_ASSISTANT_ONBOARDING_", env_nested_delimiter="__")
    config_file_env: ClassVar[str] = "HOME_ASSISTANT_ONBOARDING_CONFIG_FILE"

    endpoint: HomeAssistantEndpoint
    owner_username: str = Field(description="The local owner onboarding creates, and logs in as once it exists.")
    owner_display_name: str
    owner_password: SecretStr
    http_config: HttpConfig
    core_config: CoreConfig
