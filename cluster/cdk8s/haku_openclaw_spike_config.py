"""Generates haku-openclaw-spike/app/openclaw.json's content -- see model_rosters.py for ANTHROPIC_MODELS."""

from __future__ import annotations

from cluster.cdk8s.model_rosters import ANTHROPIC_MODELS
from cluster.cdk8s.openclaw_gateway import (
    disabled_commands,
    haku_console_mcp,
    session_memory_hook,
    trusted_proxy_gateway,
)

# OpenClaw's own native `anthropic/<model>` id, not the `{provider}/{shape}/{model}`
# LiteLLM scheme -- this agent's `anthropic` plugin calls the Anthropic API directly,
# not through LiteLLM.
_MODEL_ALIASES = {
    "claude-opus-5": "Opus",
    "claude-sonnet-5": "Sonnet",
    "claude-fable-5": "Fable",
    "claude-haiku-4-5-20251001": "Haiku",
}


def _model_id(model: str) -> str:
    return f"anthropic/{model}"


def config() -> dict:
    return {
        "meta": {"migrations": {"modelPolicyAllowlist": True}},
        "agents": {
            "defaults": {
                "userTimezone": "America/Los_Angeles",
                "model": {"primary": _model_id(ANTHROPIC_MODELS[0])},
                "models": {
                    _model_id(model): {"agentRuntime": {"id": "claude-cli"}, "alias": _MODEL_ALIASES[model]}
                    for model in ANTHROPIC_MODELS
                },
                "modelPolicy": {"allow": [_model_id(model) for model in ANTHROPIC_MODELS]},
                "sandbox": {"mode": "off"},
                "skills": [],
                "verboseDefault": "full",
            },
            "entries": {"haku": {"default": True, "name": "Haku"}},
        },
        "memory": {
            "search": {
                # TODO: Enable semantic memory search after wiring an embedding provider.
                "enabled": False
            }
        },
        "commands": disabled_commands(),
        "cron": {"enabled": False},
        "gateway": trusted_proxy_gateway(
            allowed_origin="https://haku-openclaw-spike.allegedly.works", device_approve_scopes=["operator.admin"]
        ),
        "hooks": session_memory_hook(),
        "mcp": haku_console_mcp(request_timeout_ms=60000),
        "plugins": {"entries": {"anthropic": {"enabled": True}}},
        "tools": {
            "allow": [
                "exec",
                "process",
                "read",
                "write",
                "edit",
                "apply_patch",
                "memory_get",
                "memory_search",
                "session_status",
                "bundle-mcp",
            ],
            "elevated": {"enabled": False},
            "exec": {"mode": "full"},
            "fs": {"workspaceOnly": True},
            "sessions": {"visibility": "agent"},
        },
    }


def claude_config() -> dict:
    """claude.json -- OpenClaw's bundled Claude CLI runtime's own onboarding state, seeded
    so the CLI never prompts interactively inside the pod.
    """
    return {
        "hasCompletedOnboarding": True,
        "theme": "dark",
        "projects": {
            "/home/openclaw/.openclaw/workspace": {
                "hasTrustDialogAccepted": True,
                "hasCompletedProjectOnboarding": True,
            }
        },
    }
