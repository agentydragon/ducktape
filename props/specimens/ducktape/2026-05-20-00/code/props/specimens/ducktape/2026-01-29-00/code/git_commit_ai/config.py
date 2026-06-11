"""Configuration for the git-commit-ai tool.

Configuration values are sourced from (in order of precedence):

1. Command-line options
2. Environment variables (``GIT_COMMIT_AI_*``)
3. YAML file at ``$XDG_CONFIG_HOME/ducktape/git_commit_ai.yml``
"""

from __future__ import annotations

from pathlib import Path

import platformdirs
import yaml
from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Typed configuration values."""

    model_config = {"env_prefix": "GIT_COMMIT_AI_"}

    model: str = Field(default="gpt-5.1-codex-mini", description="OpenAI model to use")
    base_url: str | None = Field(default=None, description="Custom OpenAI API base URL")
    agent_timeout_secs: int = Field(default=60, description="Max seconds for agent loop (not per-request); 0 disables")


def load_settings() -> Settings:
    """Load settings from YAML file (if exists) merged with environment variables."""
    cfg_path = Path(platformdirs.user_config_dir("ducktape")) / "git_commit_ai.yml"
    if not cfg_path.exists():
        return Settings()
    data = yaml.safe_load(cfg_path.read_text())
    return Settings(**(data or {}))
