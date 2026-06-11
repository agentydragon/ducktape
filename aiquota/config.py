import tomllib
from pathlib import Path

from platformdirs import user_config_dir
from pydantic import BaseModel, Field

from aiquota.providers.claude import ClaudeSettings
from aiquota.providers.codex import CodexSettings
from aiquota.providers.zai import ZaiSettings

CONFIG_DIR = Path(user_config_dir("aiquota"))
DEFAULT_CONFIG_PATH = CONFIG_DIR / "config.toml"


class Config(BaseModel):
    """Top-level config — one named sub-model per provider.

    Each provider section in the TOML maps to its typed settings class:

        [claude]
        credentials_path = "/some/path/.credentials.json"

        [codex]
        enabled = false

        [zai]
        api_key_path = "~/.config/zai/api_key"
    """

    claude: ClaudeSettings = Field(default_factory=ClaudeSettings)
    codex: CodexSettings = Field(default_factory=CodexSettings)
    zai: ZaiSettings = Field(default_factory=ZaiSettings)


def load(path: Path) -> Config:
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return Config()
    return Config.model_validate(tomllib.loads(raw))
