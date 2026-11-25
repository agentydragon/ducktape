"""The model catalog LLM ingress serves to Codex."""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from python.runfiles import Runfiles

from cluster.cdk8s.agentplane.model_selections import RUNNER_CONTEXT_OVERRIDES
from model_catalog.catalog import Provider, Route

_UPSTREAM_MODELS = "ducktape/cluster/cdk8s/agentplane/codex_deployment_models.json"
# Codex's default prompt (`models-manager/prompt.md`, identical in 0.157.0 and 0.163.0). Codex refuses a
# catalog entry that carries no instructions, and uses this text for a model it has no entry for.
_BASE_INSTRUCTIONS = "ducktape/cluster/cdk8s/agentplane/codex_base_instructions.txt"


def _upstream_models(root: Path) -> dict[str, dict[str, Any]]:
    return {entry["slug"]: entry for entry in json.loads((root / _UPSTREAM_MODELS).read_text())["models"]}


def codex_auto_compact_tokens(routes: Sequence[Route]) -> dict[Route, int]:
    """Resolve ModelInfo::auto_compact_token_limit from the pinned Codex deployment catalog."""
    upstream = _upstream_models(Runfiles.CreateOrRaise().root())
    thresholds = {}
    for route in routes:
        entry = upstream[route.model.id]
        window = entry.get("context_window")
        if window is None:
            window = entry.get("max_context_window")
        configured = entry.get("auto_compact_token_limit")
        if window is not None:
            default = window * 9 // 10
            thresholds[route] = default if configured is None else min(configured, default)
        elif configured is not None:
            thresholds[route] = configured
        else:
            raise ValueError(f"The pinned Codex catalog has no compaction limit or context window for {route.model.id}")
    return thresholds


def codex_catalog(routes: Sequence[Route]) -> str | None:
    """Entries for the routes that need one, or None when none does.

    A ChatGPT route uses the separately pinned upstream model catalog. Any other route gets an entry only where `RUNNER_CONTEXT_OVERRIDES` sets its window. A route without an entry keeps
    the metadata Codex falls back to for an unknown slug.
    """
    root = Runfiles.CreateOrRaise().root()
    upstream = _upstream_models(root)
    entries = []
    for route in sorted(routes, key=lambda route: route.id):
        if route.upstream.provider is Provider.CHATGPT:
            if (entry := upstream.get(route.model.id)) is None:
                raise ValueError(f"The pinned deployment catalog has no entry for {route.model.id=}")
            entries.append({**entry, "slug": route.id})
        elif (window := RUNNER_CONTEXT_OVERRIDES.get(route)) is not None:
            instructions = (root / _BASE_INSTRUCTIONS).read_text()
            entries.append(_context_window_entry(route, window, instructions))
    return json.dumps({"models": entries}, indent=2, sort_keys=True) if entries else None


def _context_window_entry(route: Route, window: int, instructions: str) -> dict[str, Any]:
    """What Codex uses for a slug it has no entry for (`model_info_from_slug`), with `window`."""
    return {
        "slug": route.id,
        "display_name": route.id,
        "model_messages": {"instructions_template": instructions},
        "supported_reasoning_levels": [],
        "shell_type": "unified_exec",
        "visibility": "none",
        "supported_in_api": True,
        "priority": 99,
        "include_apps_usage_instructions": False,
        "support_verbosity": False,
        "truncation_policy": {"mode": "bytes", "limit": 10_000},
        "context_window": window,
        "max_context_window": window,
        "experimental_supported_tools": [],
    }
