import tomllib
from pathlib import Path

from platformdirs import user_config_dir
from pydantic import BaseModel, Field, SecretStr

CONFIG_DIR = Path(user_config_dir("aiquota"))
DEFAULT_CONFIG_PATH = CONFIG_DIR / "config.toml"


class AnthropicPlatformSettings(BaseModel):
    enabled: bool = False
    url: str | None = None
    token_path: Path | None = None


class ClaudeSettings(BaseModel):
    enabled: bool = True
    credentials_path: Path = Path.home() / ".claude" / ".credentials.json"
    # An explicit token selects an externally owned, static credential source.
    # A trusted credential-substitution proxy can replace a non-secret sentinel
    # on the exact upstream Authorization header. This source is never
    # refreshed or written by aiquota.
    access_token: SecretStr | None = None


class CodexSettings(BaseModel):
    enabled: bool = True
    auth_path: Path = Path.home() / ".codex" / "auth.json"


class OpenAIPlatformSettings(BaseModel):
    enabled: bool = False
    url: str | None = None
    token_path: Path | None = None


class ZaiSettings(BaseModel):
    enabled: bool = True
    # No sensible default — user must point this at a file containing the key.
    api_key_path: Path | None = None


class CLIProxyAPISettings(BaseModel):
    """Connection settings for the shared CLIProxyAPI integration."""

    url: str | None = None


class RemoteAPISettings(BaseModel):
    """Optional remote aiquota API used by local clients."""

    url: str | None = None
    bearer_token: SecretStr | None = None


class PollingSettings(BaseModel):
    """Server polling policy. Unset intervals use the deployment settings."""

    poll_interval_seconds: int | None = Field(default=None, gt=0)
    history_interval_seconds: int | None = Field(default=None, gt=0)
    cache_ttl_seconds: int | None = Field(default=None, ge=0)
    refresh_on_read: bool = True


class Config(BaseModel):
    """AIQuota client, server polling, and provider configuration.

    Each provider section in the TOML maps to its typed settings class:

        [cli_proxy_api]
        url = "http://cli-proxy-api:8317/v0/management"

        [remote_api]
        url = "https://aiquota.allegedly.works"
        bearer_token = "<materialized by Home Manager>"

        [claude]
        credentials_path = "/some/path/.credentials.json"

        [codex]
        enabled = false

        [zai]
        api_key_path = "~/.config/zai/api_key"
    """

    cli_proxy_api: CLIProxyAPISettings = Field(default_factory=CLIProxyAPISettings)
    remote_api: RemoteAPISettings = Field(default_factory=RemoteAPISettings)
    polling: PollingSettings = Field(default_factory=PollingSettings)
    claude: ClaudeSettings = Field(default_factory=ClaudeSettings)
    codex: CodexSettings = Field(default_factory=CodexSettings)
    openai_platform: OpenAIPlatformSettings = Field(default_factory=OpenAIPlatformSettings)
    anthropic_platform: AnthropicPlatformSettings = Field(default_factory=AnthropicPlatformSettings)
    zai: ZaiSettings = Field(default_factory=ZaiSettings)


def load(path: Path) -> Config:
    return Config.model_validate(tomllib.loads(path.read_text()))
