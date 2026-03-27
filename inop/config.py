"""Configuration management for the Claude instruction optimizer."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from openai_utils.types import ReasoningEffort


class RolloutConfig(BaseModel):
    """Configuration for coding agent rollouts."""

    max_parallel: int = Field(description="Maximum concurrent rollouts")
    max_turns: int = Field(description="Maximum conversation turns per rollout")
    bash_timeout_ms: int = Field(description="Timeout for bash commands")


class PromptEngineerConfig(BaseModel):
    """Configuration for prompt engineering."""

    model: str = Field(description="Model to use for prompt engineering")
    reasoning_effort: ReasoningEffort | None = Field(description="Reasoning effort level")
    feedback_mode: str = Field(
        default="full_rollouts", description="Feedback mode: full_rollouts, summary, or stats_only"
    )


class GraderConfig(BaseModel):
    """Configuration for code grading."""

    model: str = Field(description="Model to use for grading")
    reasoning_effort: ReasoningEffort | None = Field(description="Reasoning effort level")


class SummarizerConfig(BaseModel):
    """Configuration for summarization tasks."""

    model: str = Field(description="Model to use for summarization")
    max_tokens: int = Field(description="Max tokens for responses")


class TokenConfig(BaseModel):
    """Token management configuration."""

    max_response_tokens: int = Field(description="Tokens reserved for response generation")
    reasoning_buffer_tokens: int = Field(description="Tokens reserved for reasoning")
    max_context_tokens: int = Field(description="Maximum input tokens")
    max_files_tokens: int = Field(description="Maximum tokens for file content in API calls")


class TruncationConfig(BaseModel):
    """File content truncation configuration."""

    max_file_size_grading: int = Field(
        description="Max file size in bytes before truncation for grading (affects what grader sees)"
    )
    max_file_size_pattern_analysis: int = Field(
        description="Max file size in bytes before truncation for pattern analysis (affects prompt engineering)"
    )
    log_message_length: int = Field(description="Max length for truncating log messages")


class DebugConfig(BaseModel):
    """Debugging and development configuration."""

    enable_strace: bool = Field(default=False, description="Enable strace debugging of Claude CLI execution")


class DockerLayerConfig(BaseModel):
    """Configuration for a single Docker layer."""

    image_tag: str = Field(description="Docker image tag")
    depends_on: list[str] = Field(default_factory=list, description="Layer dependencies")
    capabilities: list[str] = Field(default_factory=list, description="New capabilities this layer adds")


class ExternalImageConfig(BaseModel):
    """Configuration for external/proprietary Docker images."""

    base_image: str = Field(description="Base image name/tag from external registry")
    source: str = Field(description="Source identifier (e.g., 'azure_cr', 'external_registry')")
    description: str = Field(description="Human-readable description of this image")
    add_claude: bool = Field(default=True, description="Whether to layer Claude Code on top of this image")
    platform: str | None = Field(default=None, description="Docker platform (e.g., 'linux/amd64', 'linux/arm64')")


class OptimizerConfig(BaseModel):
    """Central configuration for the optimizer."""

    # Pre-task setup script configuration
    pre_task_setup_script: str | None = Field(
        default=None, description="Path to global pre-task setup script (runs outside container with docker access)"
    )
    pre_task_always_script: str | None = Field(
        default=None, description="Path to pre-task script that runs before every task (for authentication, etc.)"
    )
    seeds_file: str = Field(default="seeds.yaml", description="Path to seeds YAML file containing tasks")
    graders_file: str = Field(
        default="graders_consolidated.yaml", description="Path to graders YAML file containing task graders"
    )

    # Component configurations
    rollouts: RolloutConfig = Field(description="Rollout execution configuration")
    prompt_engineer: PromptEngineerConfig = Field(description="Prompt engineering configuration")
    grader: GraderConfig = Field(description="Code grading configuration")
    summarizer: SummarizerConfig = Field(description="Summarization configuration")
    tokens: TokenConfig = Field(description="Token management configuration")
    truncation: TruncationConfig = Field(description="File and message truncation configuration")
    debug: DebugConfig = Field(default_factory=DebugConfig, description="Debugging configuration")

    # File filtering
    exclude_patterns: list[str] = Field(
        description="Glob patterns for files/directories to exclude from file gathering"
    )

    # Wrapper environment variables for containerized Claude wrapper
    wrapper_env: dict[str, str] = Field(
        default_factory=dict, description="Additional environment variables for the docker wrapper"
    )

    model_config = ConfigDict(validate_assignment=True, extra="forbid")

    @classmethod
    def from_file(cls, config_path: str | Path | None = None) -> OptimizerConfig:
        """Load configuration from YAML file.

        Args:
            config_path: Path to config file. If None, looks for config.yaml in current directory.

        Returns:
            OptimizerConfig instance loaded from file

        Raises:
            FileNotFoundError: If config file doesn't exist
        """
        config_path = Path.cwd() / "config.yaml" if config_path is None else Path(config_path)

        if not config_path.exists():
            raise FileNotFoundError(
                f"Configuration file not found: {config_path}. Create this file or use OptimizerConfig() for defaults."
            )

        with config_path.open() as f:
            config_data = yaml.safe_load(f)

        return cls(**config_data)
