"""Generates haku-openclaw-spike/app/openclaw.json's content -- see model_rosters.py for ANTHROPIC_MODELS."""

from __future__ import annotations

from cluster.cdk8s.model_rosters import ANTHROPIC_MODELS

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
        "commands": {"config": False, "mcp": False, "restart": False},
        "cron": {"enabled": False},
        "gateway": {
            "auth": {
                "mode": "trusted-proxy",
                "trustedProxy": {
                    "allowLoopback": True,
                    "allowUsers": ["agentydragon"],
                    "requiredHeaders": ["x-authentik-email", "x-forwarded-host", "x-forwarded-proto"],
                    "userHeader": "x-authentik-username",
                    "deviceAutoApprove": {"enabled": True, "scopes": ["operator.admin"]},
                },
            },
            "bind": "lan",
            "controlUi": {"allowedOrigins": ["https://haku-openclaw-spike.allegedly.works"]},
            "mode": "local",
            "trustedProxies": ["10.0.0.0/8"],
        },
        "hooks": {"internal": {"entries": {"session-memory": {"enabled": True, "llmSlug": False, "messages": 15}}}},
        "mcp": {
            "servers": {
                "haku-console": {
                    "headers": {"Authorization": "Bearer ${HAKU_CONSOLE_TOKEN}"},
                    "requestTimeoutMs": 60000,
                    "supportsParallelToolCalls": True,
                    "transport": "streamable-http",
                    "url": "https://haku.allegedly.works/mcp",
                }
            }
        },
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
