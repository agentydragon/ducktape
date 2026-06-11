"""Clean configuration models with no dict manipulation or dynamic fields."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

import tomli
import tomli_w
from pydantic import BaseModel, Field

from llm.claude_linter_v2.config.models import (
    AccessControlRule,
    AutofixCategory,
    LLMAnalysisConfig,
    NotificationHookConfig,
    PatternBasedRule,
    PostToolHookConfig,
    PredicateRule,
    PreToolHookConfig,
    StopHookConfig,
    SubagentStopHookConfig,
    TaskProfile,
)
from llm.claude_linter_v2.rule_registry import RuleRegistry


class LogLevel(StrEnum):
    """Logging level for configuration."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


# Type alias for hook config union
HookConfig = PreToolHookConfig | PostToolHookConfig | StopHookConfig | NotificationHookConfig | SubagentStopHookConfig


def _default_hooks() -> dict[str, HookConfig]:
    """Default hook configurations."""
    return {
        "pre": PreToolHookConfig(enabled=True),
        "post": PostToolHookConfig(
            enabled=True, auto_fix=True, autofix_categories=[AutofixCategory.FORMATTING], inject_permissions=True
        ),
        "stop": StopHookConfig(enabled=True, quality_gate=True, max_files_to_show=5, max_violations_per_file=3),
        "notification": NotificationHookConfig(enabled=True, send_to_dbus=True, urgency="normal"),
        "subagent_stop": SubagentStopHookConfig(enabled=True),
    }


class RuleConfig(BaseModel):
    """Configuration for a single rule."""

    enabled: bool = Field(True, description="Whether this rule is enabled")
    blocks_pre_hook: bool | None = Field(None, description="Override default pre-hook blocking (None = use default)")
    blocks_stop_hook: bool | None = Field(None, description="Override default stop-hook blocking (None = use default)")
    message: str | None = Field(None, description="Override default error message")


class ModularConfig(BaseModel):
    """Clean modular configuration with strict validation."""

    model_config = {"extra": "forbid"}  # No dynamic fields allowed

    # Core settings
    version: Literal["2.0"] = Field("2.0", description="Config version")
    max_errors_to_show: int = Field(3, description="Maximum errors to display in hook responses")

    # Rules configuration - single dict for all rules
    rules: dict[str, RuleConfig] = Field(
        default_factory=dict,
        description="Rule configurations keyed by canonical name (e.g., 'python.bare_except', 'ruff.E722')",
    )

    # Access control
    access_control: list[AccessControlRule] = Field(default_factory=list, description="Path-based access control rules")

    # Repo-wide predicate rules
    repo_rules: list[PredicateRule] = Field(default_factory=list, description="Repository-wide predicate rules")

    # Python tools
    python_tools: list[str] = Field(default_factory=lambda: ["ruff", "mypy"], description="Python linting tools to use")

    # Hook configurations
    hooks: dict[str, HookConfig] = Field(default_factory=_default_hooks, description="Hook-specific configurations")

    # Pattern-based file rules
    pattern_rules: list[PatternBasedRule] = Field(
        default_factory=lambda: [
            PatternBasedRule(
                name="test_files",
                patterns=["**/test_*.py", "**/*_test.py", "**/tests/**"],
                relaxed_checks=["python.bare_except", "ruff.E722"],
                custom_message="Test files have relaxed rules for error handling",
                enabled=True,
            )
        ],
        description="Pattern-based rules for file handling",
    )

    # LLM analysis
    llm_analysis: LLMAnalysisConfig = Field(
        default_factory=lambda: LLMAnalysisConfig(
            enabled=False, model="gpt-4o-mini", daily_cost_limit=5.0, cache_results=True
        ),
        description="LLM analysis configuration",
    )

    # Task profiles
    profiles: list[TaskProfile] = Field(default_factory=list, description="Pre-defined permission profiles")

    # Logging
    log_level: LogLevel = Field(LogLevel.INFO, description="Logging level")
    log_file: Path | None = Field(None, description="Log file path")

    @classmethod
    def from_toml(cls, path: Path) -> ModularConfig:
        """Load configuration from TOML file - let Pydantic handle parsing."""
        with path.open("rb") as f:
            data = tomli.load(f)

        # Transform flat dotted keys to nested structure for rules
        if "rules" not in data:
            data["rules"] = {}

        # Handle dotted keys like [rules."python.bare_except"]
        keys_to_remove = []
        for key, value in data.items():
            if key.startswith("rules."):
                rule_key = key[6:]  # Remove "rules." prefix
                # Remove quotes if present
                if rule_key.startswith('"') and rule_key.endswith('"'):
                    rule_key = rule_key[1:-1]
                data["rules"][rule_key] = value
                keys_to_remove.append(key)

        # Remove the flat keys we've processed
        for key in keys_to_remove:
            del data[key]

        # Let Pydantic validate and create the model
        return cls(**data)

    def save_to_file(self, path: Path) -> None:
        """Save configuration to TOML file."""
        # Use Pydantic's serialization mode to handle enums and Paths automatically
        data = self.model_dump(exclude_none=True, mode="python")

        with path.open("wb") as f:
            tomli_w.dump(data, f)

    def get_rule_config(self, rule_key: str) -> RuleConfig | None:
        """Get configuration for a specific rule."""
        # First check explicit config
        if rule_key in self.rules:
            return self.rules[rule_key]

        rule_def = RuleRegistry.get_by_key(rule_key)
        if rule_def:
            # Create a RuleConfig from registry defaults
            return RuleConfig(
                enabled=True,  # Rules in registry are enabled by default
                blocks_pre_hook=rule_def.default_blocks_pre,
                blocks_stop_hook=rule_def.default_blocks_stop,
                message=rule_def.default_message,
            )

        return None

    def get_ruff_codes_to_select(self) -> list[str]:
        """Get list of ruff codes to force enable."""
        codes = []

        # First, get all ruff rules from the registry that are enabled by default
        for rule_def in RuleRegistry.get_all_rules():
            if rule_def.category == "ruff":
                key = f"ruff.{rule_def.code}"
                # Check if there's an explicit config for this rule
                if key in self.rules:
                    # Use explicit config
                    if self.rules[key].enabled:
                        codes.append(rule_def.code)
                # Use default from registry
                elif rule_def.default_blocks_pre or rule_def.default_blocks_stop:
                    codes.append(rule_def.code)

        return codes
