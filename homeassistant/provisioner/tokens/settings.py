"""Configuration of the Home Assistant token provisioner."""

from __future__ import annotations

import os

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

from homeassistant.provisioner.endpoint import HomeAssistantEndpoint

# YamlConfigSettingsSource loads yaml lazily inside pydantic-settings; Gazelle
# cannot see the dependency.
# gazelle:include_dep @pypi//pyyaml

CONFIG_FILE_ENV = "HOME_ASSISTANT_TOKEN_PROVISIONER_CONFIG_FILE"


class TokenConfig(BaseModel):
    """A Secret the provisioner keeps holding a long-lived token that Home Assistant accepts,
    under the key `token`."""

    client_name: str = Field(
        description="The token's name in Home Assistant. Minting one revokes its user's other tokens of this name."
    )
    read_only_user: str | None = Field(
        default=None,
        description=(
            "A local-only member of Home Assistant's read-only group alone, which the provisioner creates, "
            "whose token this is; None for the owner's."
        ),
    )
    secret_name: str
    secret_namespace: str
    description: str = Field(description="The Secret's `description` annotation.")


class Settings(BaseSettings):
    """Environment variables (`HOME_ASSISTANT_TOKEN_PROVISIONER_*`) override the YAML file
    `CONFIG_FILE_ENV` names."""

    model_config = SettingsConfigDict(env_prefix="HOME_ASSISTANT_TOKEN_PROVISIONER_", env_nested_delimiter="__")

    endpoint: HomeAssistantEndpoint
    owner_username: str = Field(
        description="Home Assistant's local owner, who mints its own tokens and resets the read-only users."
    )
    owner_password: SecretStr
    tokens: tuple[TokenConfig, ...]

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings, dotenv_settings]
        if config_file := os.environ.get(CONFIG_FILE_ENV):
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=config_file))
        sources.append(file_secret_settings)
        return tuple(sources)
