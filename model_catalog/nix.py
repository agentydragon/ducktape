"""Model selections and JSON generation for the Nix Claude Code gateway wrappers."""

import json
from pathlib import Path

from model_catalog.catalog import (
    ANTIGRAVITY_FLASH_LITE,
    ANTIGRAVITY_PRO,
    GEMINI_FLASH,
    GEMINI_FLASH_LITE,
    GPT6_ASTRA_MESSAGES,
    GPT6_LUNA_MESSAGES,
    HAIKU_SUBSCRIPTION,
    SONNET_SUBSCRIPTION,
    TANA_HAIKU,
    TANA_SONNET,
    Route,
)
from model_catalog.policies import KEY_MODEL_LANES, ModelLaneRoutes
from util.bazel.workspace import get_build_workspace_directory

OUTPUT_PATH = "model_catalog/claude-wrappers.json"


def _wrapper(
    primary: Route,
    haiku: Route,
    lane: ModelLaneRoutes,
    *,
    max_context_tokens: int | None = None,
    max_output_tokens: int | None = None,
) -> dict[str, str | int]:
    if primary not in lane.allowed or haiku not in lane.allowed:
        raise ValueError(f"wrapper selects a route outside its key lane: {primary.id}, {haiku.id}")
    config: dict[str, str | int] = {"model": primary.id, "haikuModel": haiku.id}
    if max_context_tokens is not None:
        config["maxContextTokens"] = max_context_tokens
    if max_output_tokens is not None:
        config["maxOutputTokens"] = max_output_tokens
    return config


def claude_wrapper_models() -> dict[str, dict[str, str | int]]:
    # Preserve existing Claude settings, not provider capacity claims. Omission
    # leaves the client's default in place; these wrappers remain paused (#9121).
    return {
        "codex-claude": _wrapper(
            GPT6_ASTRA_MESSAGES,
            GPT6_LUNA_MESSAGES,
            KEY_MODEL_LANES["codex_client_models"],
            max_context_tokens=872_000,
            max_output_tokens=128_000,
        ),
        "litellm-claude": _wrapper(SONNET_SUBSCRIPTION, HAIKU_SUBSCRIPTION, KEY_MODEL_LANES["claude_client_models"]),
        "gemini-claude": _wrapper(
            GEMINI_FLASH,
            GEMINI_FLASH_LITE,
            KEY_MODEL_LANES["gemini_client_models"],
            max_context_tokens=1_048_576,
            max_output_tokens=65_536,
        ),
        "antigravity-claude": _wrapper(
            ANTIGRAVITY_PRO,
            ANTIGRAVITY_FLASH_LITE,
            KEY_MODEL_LANES["antigravity_client_models"],
            max_context_tokens=1_048_576,
            # Preserve this wrapper's 65,536, independently of account metadata.
            max_output_tokens=65_536,
        ),
        "tana-claude": _wrapper(TANA_SONNET, TANA_HAIKU, KEY_MODEL_LANES["tana_client_models"]),
    }


def write_config(root: Path) -> None:
    path = root / OUTPUT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(claude_wrapper_models(), indent=2, sort_keys=True) + "\n")


def main() -> None:
    write_config(get_build_workspace_directory())


if __name__ == "__main__":
    main()
