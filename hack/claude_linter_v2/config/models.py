"""Configuration models for Claude Linter v2 using Pydantic."""

import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class Violation(BaseModel):
    """A code quality violation."""

    rule: str = Field(description="Rule identifier (e.g., 'bare_except', 'ruff:E722')")
    line: int = Field(description="Line number where violation occurs")
    column: int = Field(0, description="Column number (optional)")
    message: str = Field(description="Human-readable violation message")
    fixable: bool = Field(False, description="Whether this can be auto-fixed")
    file_path: str | None = Field(None, description="File path where violation occurs")


class HookType(str, Enum):
    """Types of Claude Code hooks."""

    PRE = "pre"
    POST = "post"
    STOP = "stop"


class RuleAction(str, Enum):
    """Actions that can be taken by rules."""

    ALLOW = "allow"
    DENY = "deny"
    WARN = "warn"


class AutofixCategory(str, Enum):
    """Categories of autofixes."""

    FORMATTING = "formatting"
    IMPORTS = "imports"
    TYPE_HINTS = "type_hints"
    SECURITY = "security"
    ALL = "all"


class AccessControlRule(BaseModel):
    """Path-based access control rule."""

    paths: list[str] = Field(description="Glob patterns for paths")
    tools: list[str] = Field(description="Tool names (Write, Edit, etc)")
    action: RuleAction = Field(description="Action to take")
    message: str | None = Field(None, description="Custom message to show")


class PredicateRule(BaseModel):
    """Python predicate-based rule."""

    predicate: str = Field(description="Python expression or function")
    action: RuleAction = Field(description="Action to take")
    message: str | None = Field(None, description="Custom message to show")
    priority: int = Field(0, description="Rule priority (higher = evaluated first)")


# PythonHardBlock and PythonConfig have been removed - use modular config instead


class HookConfigBase(BaseModel):
    """Base configuration for all hook types."""

    enabled: bool = Field(True, description="Whether this hook is enabled")


class PreToolHookConfig(HookConfigBase):
    """Configuration for PreToolUse hooks."""

    # No additional fields needed - PreTool just blocks or allows


class PostToolHookConfig(HookConfigBase):
    """Configuration for PostToolUse hooks."""

    auto_fix: bool = Field(True, description="Whether to auto-fix issues")
    autofix_categories: list[AutofixCategory] = Field(
        default_factory=list, description="Categories to autofix (empty = use defaults)"
    )
    inject_permissions: bool = Field(True, description="Whether to inject permission info in responses")

    @field_validator("autofix_categories", mode="before")
    @classmethod
    def expand_all_category(cls, v: list[Any]) -> list[AutofixCategory]:
        """Expand 'all' category to all categories."""
        if not v:
            return []

        categories = []
        for cat_input in v:
            cat = AutofixCategory(cat_input) if isinstance(cat_input, str) else cat_input
            if cat == AutofixCategory.ALL:
                return list(AutofixCategory)
            categories.append(cat)

        return categories


class StopHookConfig(HookConfigBase):
    """Configuration for Stop hooks."""

    quality_gate: bool = Field(True, description="Whether to enforce quality gate")
    max_files_to_show: int = Field(5, description="Maximum number of files to show in error message")
    max_violations_per_file: int = Field(3, description="Maximum violations to show per file")


class NotificationHookConfig(HookConfigBase):
    """Configuration for Notification hooks."""

    send_to_dbus: bool = Field(True, description="Whether to send notifications to D-Bus")
    urgency: Literal["low", "normal", "critical"] = Field("normal", description="Default notification urgency")


class SubagentStopHookConfig(HookConfigBase):
    """Configuration for SubagentStop hooks."""

    # No additional fields needed for now


class LLMPromptTemplates(BaseModel):
    """Configurable prompt templates for LLM analysis."""

    full_file_analysis: str = Field(
        default="""Analyze this Python code for potential issues.

File: {file_path}

```python
{content}
```

Check for:
1. Error hiding patterns (catching exceptions without proper handling)
2. Security issues (hardcoded secrets, SQL injection, command injection)
3. Resource leaks (files/connections not properly closed)
4. Race conditions
5. Type safety issues

Respond with JSON:
{{
    "ok": true/false,
    "message": "Brief explanation if not ok",
    "violations": [
        {{
            "line": <line_number>,
            "rule": "LLM:<check_type>",
            "message": "Description of issue"
        }}
    ]
}}

If the code looks fine, return {{"ok": true, "violations": []}}""",
        description="Template for analyzing full files",
    )

    edit_analysis: str = Field(
        default="""Analyze this code change for potential issues.

File: {file_path}

Original code:
```python
{old_string}
```

Changed to:
```python
{new_string}
```

Context:
```python
{context}
```

Check if this change introduces:
1. Error hiding (broad exception handling)
2. Security vulnerabilities
3. Resource leaks
4. Type safety issues
5. Logic errors

Respond with JSON:
{{
    "ok": true/false,
    "message": "Brief explanation if not ok",
    "violations": [
        {{
            "line": <line_number>,
            "rule": "LLM:<check_type>",
            "message": "Description of issue"
        }}
    ]
}}""",
        description="Template for analyzing single edits",
    )

    multi_edit_analysis: str = Field(
        default="""Analyze these code changes for potential issues.

File: {file_path}

Changes:
{edits_summary}

Check if these changes introduce:
1. Error hiding patterns
2. Security vulnerabilities
3. Inconsistent error handling
4. Resource leaks

Respond with JSON:
{{
    "ok": true/false,
    "message": "Brief explanation if not ok",
    "violations": []
}}""",
        description="Template for analyzing multiple edits",
    )


class LLMAnalysisConfig(BaseModel):
    """Configuration for LLM-based analysis."""

    enabled: bool = Field(False, description="Whether to use LLM analysis")
    model: str = Field("gpt-4o-mini", description="Model to use")
    check_types: list[str] = Field(
        default_factory=lambda: ["error_hiding", "security_issues"], description="Types of checks to perform"
    )
    daily_cost_limit: float = Field(5.0, description="Maximum daily cost in USD")
    cache_results: bool = Field(True, description="Whether to cache results")
    prompts: LLMPromptTemplates = Field(default_factory=LLMPromptTemplates, description="Prompt templates")


class PatternBasedRule(BaseModel):
    """Generic pattern-based rule for file handling."""

    name: str = Field(description="Rule name (e.g., 'test_files', 'migrations')")
    patterns: list[str] = Field(description="Glob patterns to match files")
    relaxed_checks: list[str] = Field(default_factory=list, description="Checks to relax for matching files")
    enforced_checks: list[str] = Field(default_factory=list, description="Checks to enforce for matching files")
    custom_message: str | None = Field(None, description="Custom message when rules apply")
    enabled: bool = Field(True, description="Whether this rule is active")


class TaskProfile(BaseModel):
    """Pre-approved permission profile for common tasks."""

    name: str = Field(description="Profile name")
    description: str | None = Field(None, description="Profile description")
    predicate: str = Field(description="Python predicate granting permissions")
    duration: str | None = Field(None, description="How long profile is active")

    @field_validator("duration")
    @classmethod
    def validate_duration(cls, v: str | None) -> str | None:
        """Validate duration format."""
        if v is None:
            return None

        # Simple validation - could be expanded
        if not re.match(r"^\d+[hmd]$", v):
            raise ValueError("Duration must be like '2h', '30m', or '1d'")

        return v


# ClaudeLinterConfig has been removed - use ModularClaudeLinterConfig instead
