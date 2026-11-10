"""The model catalog LLM ingress serves to Codex."""

import json
from collections.abc import Sequence
from typing import Any

from python.runfiles import Runfiles

from cluster.cdk8s.agentplane.model_selections import RUNNER_CONTEXT_OVERRIDES
from model_catalog.catalog import Provider, Route

_UPSTREAM_MODELS = "ducktape/cluster/cdk8s/agentplane/codex_upstream_models.json"
# Codex's default prompt (`models-manager/prompt.md`, identical in 0.157.0 and 0.163.0). Codex refuses a
# catalog entry that carries no instructions, and uses this text for a model it has no entry for.
_BASE_INSTRUCTIONS = "ducktape/cluster/cdk8s/agentplane/codex_base_instructions.txt"


def codex_catalog(routes: Sequence[Route]) -> str | None:
    """Entries for the routes that need one, or None when none does.

    A ChatGPT route serves an OpenAI model, so its entry is the upstream one from the pinned Codex. Any other
    route gets an entry only where `RUNNER_CONTEXT_OVERRIDES` sets its window. A route without an entry keeps
    the metadata Codex falls back to for an unknown slug.
    """
    root = Runfiles.CreateOrRaise().root()
    upstream = {entry["slug"]: entry for entry in json.loads((root / _UPSTREAM_MODELS).read_text())["models"]}
    entries = []
    for route in sorted(routes, key=lambda route: route.id):
        if route.upstream.provider is Provider.CHATGPT:
            if (entry := upstream.get(route.model.id)) is None:
                raise ValueError(f"The pinned Codex has no bundled entry for {route.model.id=}")
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
