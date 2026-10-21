"""Model catalog entries a test serves to Codex."""

from __future__ import annotations

import json
from typing import Any

from more_itertools import one

from util.bazel.runfiles import get_required_path

# The pinned Codex's own catalog (`codex debug models --bundled`), which LLM ingress serves ChatGPT routes from.
_BUNDLED = "_main/cluster/cdk8s/agentplane/codex_upstream_models.json"


def usable_window(context_window: int) -> int:
    """The `modelContextWindow` Codex reports for an entry: its `effective_context_window_percent` (95) of it."""
    return context_window * 95 // 100


def entry(slug: str, *, context_window: int) -> dict[str, Any]:
    """The `ModelInfo` Codex uses for a slug it has no entry for, with `context_window`.

    Codex refuses a whole catalog when an entry lacks instruction text, and a bundled model's entry switches
    it to code mode; this keeps the classic function-call tools.
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


def bundled(model: str, *, slug: str) -> dict[str, Any]:
    """The pinned Codex's bundled entry for `model`, served as `slug` the way LLM ingress serves a ChatGPT route."""
    models = json.loads(get_required_path(_BUNDLED).read_text())["models"]
    return {**one(entry for entry in models if entry["slug"] == model), "slug": slug}
