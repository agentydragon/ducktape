"""Configuration models for Claude Code hooks."""

from pydantic import BaseModel, Field


class HookConfiguration(BaseModel):
    enabled: bool = Field(default=True, description="Whether the hook is enabled")
    timeout_seconds: int = Field(default=60, ge=1, le=600, description="Hook execution timeout")
    matcher: str | None = Field(default=None, description="Tool name pattern matcher")


class AutofixerConfig(HookConfiguration):
    dry_run: bool = Field(default=False, description="Run in dry-run mode without making changes")
    tools: list[str] = Field(
        default_factory=lambda: ["Edit", "MultiEdit", "Write"], description="List of tool names that trigger autofix"
    )
