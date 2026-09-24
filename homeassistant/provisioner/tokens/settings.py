"""Configuration of the Home Assistant token provisioner."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import SettingsConfigDict

from homeassistant.provisioner.endpoint import HomeAssistantEndpoint
from homeassistant.provisioner.yaml_settings import YamlFileSettings


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


class Settings(YamlFileSettings):
    model_config = SettingsConfigDict(env_prefix="HOME_ASSISTANT_TOKEN_PROVISIONER_", env_nested_delimiter="__")
    config_file_env: ClassVar[str] = "HOME_ASSISTANT_TOKEN_PROVISIONER_CONFIG_FILE"

    endpoint: HomeAssistantEndpoint
    owner_username: str = Field(
        description="Home Assistant's local owner, who mints its own tokens and resets the read-only users."
    )
    owner_password: SecretStr
    tokens: tuple[TokenConfig, ...]
