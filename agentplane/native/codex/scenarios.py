"""Codex app-server launch configuration and hook registration."""

from __future__ import annotations

import json
from typing import Any

# The events a hooks capture registers: the blocking events a single-agent turn without compaction
# reaches, plus the session brackets. Subagent and compaction events stay unregistered.
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionEnd")


def hooks_config(command: str) -> dict[str, Any]:
    """Per-thread config running `command` as a shell hook on every event in `HOOK_EVENTS`. Codex
    spawns it with the hook input on stdin and reads its stdout as the hook's output; the driver
    only sees `hook/started` and `hook/completed`. The config is the driver's own, so the trust
    check on unmanaged hooks is bypassed."""
    return {
        "hooks": {
            event: [{"hooks": [{"type": "command", "command": command, "timeout": 30}]}] for event in HOOK_EVENTS
        },
        "bypass_hook_trust": True,
    }


# This replaces Codex's broad default coding-agent policy while preserving the
# app-server's native tool behavior for each scenario.
BASE_INSTRUCTIONS = "You are a concise test assistant. Follow user requests using the available tools."
CLIENT_NAME = "agentplane-capture"
# Bounded upstream retries keep connection-loss scenarios short.
MAX_RETRIES = 2


def config(*, endpoint: str, max_retries: int, model_context_window: int | None = None) -> dict[str, Any]:
    """Native app-server configuration; a direct setup, not a Haku adapter."""
    result: dict[str, Any] = {
        # The environment supplies OPENAI_API_KEY; the provider keeps the Responses endpoint explicit.
        "model_provider": "agentplane",
        "model_providers": {
            "agentplane": {
                "name": "Agentplane LiteLLM",
                "base_url": endpoint,
                "env_key": "OPENAI_API_KEY",
                "wire_api": "responses",
                # Transport failures retry only through the stream loop, whose attempts are
                # visible as native `error` notices; a request-level retry budget would
                # multiply the upstream attempts silently.
                "request_max_retries": 0,
                "stream_max_retries": max_retries,
            }
        },
        # Run deterministic tool probes without an interactive approval round-trip.
        "approval_policy": "never",
        "sandbox_mode": "danger-full-access",
        # Remove automatic skill discovery and its <skills_instructions> prompt block.
        "skills": {"bundled": {"enabled": False}, "include_instructions": False},
        # A fixed approval policy and no app, collaboration, or environment-context features:
        # omit their prompt blocks rather than recording unrelated harness guidance.
        "include_permissions_instructions": False,
        "include_apps_instructions": False,
        "include_collaboration_mode_instructions": False,
        "include_environment_context": False,
        "web_search": "disabled",
        "features": {
            # The scenarios use only shell execution; these tools are prompt bulk otherwise.
            "multi_agent": False,
            "view_image": False,
            # Retries are bounded by the provider's *_max_retries above.
            "unbounded_connection_retries": False,
        },
    }
    if model_context_window is not None:
        if model_context_window <= 0:
            raise ValueError("model_context_window must be positive")
        result["model_context_window"] = model_context_window
        # Keep Codex's default auto-compaction threshold: it derives 90% from this resolved window.
    return result


def _toml_literal(value: object) -> str:
    # JSON literals for strings, booleans, integers, and arrays of those are valid TOML values.
    return json.dumps(value)


def _overrides(config: dict[str, Any], prefix: str = "") -> list[str]:
    """`-c dotted.key=value` pairs; app-server parses each value as TOML."""
    result: list[str] = []
    for key, value in config.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            result.extend(_overrides(value, f"{path}."))
        else:
            result.extend(["-c", f"{path}={_toml_literal(value)}"])
    return result


def command(binary: str, *, endpoint: str, max_retries: int, model_context_window: int | None = None) -> list[str]:
    return [
        binary,
        *_overrides(config(endpoint=endpoint, max_retries=max_retries, model_context_window=model_context_window)),
        "app-server",
        "--listen",
        "stdio://",
    ]


def environment(*, endpoint: str, token: str, codex_home: str) -> dict[str, str]:
    return {"CODEX_HOME": codex_home, "OPENAI_API_KEY": token, "OPENAI_BASE_URL": endpoint}
