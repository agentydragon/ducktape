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


class Settings(YamlFileSettings):
    model_config = SettingsConfigDict(env_prefix="HOME_ASSISTANT_ONBOARDING_", env_nested_delimiter="__")
    config_file_env: ClassVar[str] = "HOME_ASSISTANT_ONBOARDING_CONFIG_FILE"

    endpoint: HomeAssistantEndpoint
    owner_username: str = Field(description="The local owner onboarding creates, and logs in as once it exists.")
    owner_display_name: str
    owner_password: SecretStr
    http_config: HttpConfig
