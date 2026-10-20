"""Model catalog entries a test serves to Codex."""

from __future__ import annotations

from typing import Any


def usable_window(context_window: int) -> int:
    """The `modelContextWindow` Codex reports for an entry: its `effective_context_window_percent` (95) of it."""
    return context_window * 95 // 100


def entry(slug: str, *, context_window: int) -> dict[str, Any]:
    """The `ModelInfo` Codex uses for a slug it has no entry for, with `context_window`.

    Codex refuses a whole catalog when an entry lacks instruction text, and a bundled model's entry switches
    it to code mode; this keeps the classic function-call tools the scripted models answer.
    """
    return {
        "slug": slug,
        "display_name": slug,
        "base_instructions": "You are a test assistant.",
        "supported_reasoning_levels": [],
        "shell_type": "unified_exec",
        "visibility": "none",
        "supported_in_api": True,
        "priority": 99,
        "include_apps_usage_instructions": False,
        "support_verbosity": False,
        "truncation_policy": {"mode": "bytes", "limit": 10_000},
        "context_window": context_window,
        "max_context_window": context_window,
        "experimental_supported_tools": [],
    }
