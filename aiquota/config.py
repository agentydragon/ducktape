import tomllib
from contextlib import suppress
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
    raw: dict[str, object] = {}
    with suppress(FileNotFoundError):
        raw = tomllib.loads(path.read_text())

    # Home Manager can own this small companion file without overwriting a
    # user's provider configuration in config.toml. Its remote_api section is
    # deliberately authoritative whenever present, so enabling the managed
    # client cannot accidentally fall back to local provider credentials.
    remote_path = path.with_name("remote.toml")
    remote_raw: dict[str, object] = {}
    with suppress(FileNotFoundError):
        remote_raw = tomllib.loads(remote_path.read_text())
    if isinstance(remote_raw.get("remote_api"), dict):
        raw["remote_api"] = remote_raw["remote_api"]
    return Config.model_validate(raw)
