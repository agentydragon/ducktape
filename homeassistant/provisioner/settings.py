"""Declarative configuration for the Home Assistant provisioner."""

from __future__ import annotations

import os
from typing import ClassVar

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

# YamlConfigSettingsSource loads yaml lazily inside pydantic-settings; Gazelle
# cannot see the dependency.
# gazelle:include_dep @pypi//pyyaml


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


class ComponentConfig(BaseModel):
    """Checksum-pinned release archive configuration."""

    version: str
    url: str
    sha256: str
    archive_path: str
    install_dir: str
    manifest_domain: str | None = None
    config_files: tuple[str, ...] = ()


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


class ProvisionerSettings(BaseSettings):
    """Settings loaded from env, then an optional YAML config file.

    Environment variables use the ``HOME_ASSISTANT_PROVISIONER_`` prefix and
    override YAML values. Nested values use ``__`` as the delimiter.
    """

    model_config = SettingsConfigDict(env_prefix="HOME_ASSISTANT_PROVISIONER_", env_nested_delimiter="__")

    home_assistant_url: str
    client_id: str
    redirect_uri: str
    username: str
    display_name: str
    local_admin_password: SecretStr | None = None
    http_config: HttpConfig
    components: tuple[ComponentConfig, ...]
    onboarding_enabled: bool
    tokens: tuple[TokenConfig, ...] | None = Field(
        default=None,
        description="Secrets the `tokens` command keeps holding valid tokens; None where only `setup` runs.",
    )

    # Used by callers that need to describe the source without duplicating the
    # environment-variable name in their own argument parsers.
    config_file_env: ClassVar[str] = "HOME_ASSISTANT_PROVISIONER_CONFIG_FILE"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Insert an optional YAML source below environment variables."""
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings, dotenv_settings]
        if config_file := os.environ.get(cls.config_file_env):
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=config_file))
        sources.append(file_secret_settings)
        return tuple(sources)


def load_settings() -> ProvisionerSettings:
    """Load one provisioner settings object for dependency injection."""
    return ProvisionerSettings()
