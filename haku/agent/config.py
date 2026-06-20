"""Runtime configuration for Haku's Agent Framework runtime (env: HAKU_*)."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Haku AF runtime settings, sourced from HAKU_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="HAKU_", protected_namespaces=())

    model: str = Field(description="LiteLLM model id, e.g. 'anthropic/claude-opus-4-8' or 'zai/glm-4.6'.")
    litellm_base_url: str = Field(description="In-cluster LiteLLM OpenAI-compatible base URL (ends in /v1).")
    litellm_api_key: str = Field(description="Haku's scoped LiteLLM virtual key.")
    tana_ro_token: str | None = Field(default=None, description="Bearer for tana-mcp-ro; omit to run without Tana.")
    base_dir: Path = Field(default=Path("/opt/haku"), description="Baked manual + run procedure root (base/, run.md).")
    state_dir: Path = Field(default=Path("/workspace/haku-state"), description="haku-state checkout (Haku's memory).")
    session_id: str = Field(default="haku-main", description="Stable session id; the same id resumes the thread.")
    keep_last_groups: int = Field(default=50, description="SlidingWindow compaction: recent message groups to keep.")
