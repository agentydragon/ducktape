"""Runtime configuration for Haku's Agent Framework runtime (env: HAKU_*)."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Haku AF runtime settings, sourced from HAKU_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="HAKU_", protected_namespaces=())

    model: str = Field(description="LiteLLM model id, e.g. 'anthropic/claude-opus-5'.")
    litellm_base_url: str = Field(description="In-cluster LiteLLM OpenAI-compatible base URL (ends in /v1).")
    litellm_api_key: str = Field(description="Haku's scoped LiteLLM virtual key.")
    console_token: str | None = Field(
        default=None, description="Bearer for haku-console's /mcp (Tana + other console-mediated tools); omit to skip."
    )
    # Repos cloned at startup as context: ducktape holds the manual, run procedure, sources and
    # code; haku-state is Haku's memory and write surface. A None `*_repo_url` skips the clone,
    # assuming the directory is already present.
    ducktape_repo_url: str | None = Field(default=None, description="ducktape git URL to clone for context.")
    ducktape_dir: Path = Field(default=Path("/workspace/ducktape"), description="ducktape checkout (manual + code).")
    ducktape_clone_depth: int = Field(default=1, description="Shallow-clone depth for ducktape; 0 = full history.")
    state_repo_url: str | None = Field(default=None, description="haku-state git URL to clone; None assumes present.")
    state_dir: Path = Field(default=Path("/workspace/haku-state"), description="haku-state checkout (Haku's memory).")
    git_host: str | None = Field(default=None, description="Host for the ~/.netrc git-creds entry (haku-state push).")
    git_username: str | None = Field(default=None, description="Git username for git_host.")
    git_password: str | None = Field(default=None, description="Git password/token for git_host.")
    session_id: str = Field(default="haku-main", description="Stable session id; the same id resumes the thread.")
    summarize_target_count: int = Field(
        default=20, description="Compaction: message groups to keep after LLM summarization."
    )
    summarize_threshold: int = Field(
        default=10, description="Compaction: summarize once group count exceeds target + this."
    )
    summarize_model: str | None = Field(
        default=None, description="LiteLLM model for SummarizationStrategy; None reuses HAKU_MODEL."
    )
    host: str = Field(default="0.0.0.0", description="Supervisor HTTP bind host.")
    port: int = Field(default=8080, description="Supervisor HTTP bind port.")
    wake_interval_seconds: int = Field(default=0, description="Scheduler wake interval in seconds; 0 disables it.")
    redis_url: str | None = Field(
        default=None, description="Valkey/Redis URL for durable session history; None = in-memory."
    )
    redis_max_messages: int = Field(
        default=500, description="Max messages retained per session in Redis (auto-trims oldest)."
    )
