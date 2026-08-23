import tomllib
from pathlib import Path

from platformdirs import user_config_dir
from pydantic import BaseModel, Field, SecretStr

from aiquota.providers.claude import ClaudeSettings
from aiquota.providers.codex import CodexSettings
from aiquota.providers.zai import ZaiSettings

CONFIG_DIR = Path(user_config_dir("aiquota"))
DEFAULT_CONFIG_PATH = CONFIG_DIR / "config.toml"


class CLIProxyAPISettings(BaseModel):
    """Connection settings for the shared CLIProxyAPI integration."""

    url: str | None = None


class RemoteAPISettings(BaseModel):
    """Optional remote aiquota API used by local clients."""

    url: str | None = None
    bearer_token: SecretStr | None = None


class Config(BaseModel):
    """Top-level config — one named sub-model per provider.

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
    claude: ClaudeSettings = Field(default_factory=ClaudeSettings)
    codex: CodexSettings = Field(default_factory=CodexSettings)
    zai: ZaiSettings = Field(default_factory=ZaiSettings)


def load(path: Path) -> Config:
    return Config.model_validate(tomllib.loads(path.read_text()))
